import hashlib
import json
import logging
import os
import random
import re
import secrets
import sys
import threading
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd
from fastapi import Depends, FastAPI, Header, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

ROOT = Path(__file__).resolve().parent.parent
WEB_DIR = Path(__file__).resolve().parent
DATA_DIR = ROOT / "data"
OUTPUT_DIR = ROOT / "output"
SECURITY_FILE = ROOT / "security.json"

DATA_DIR.mkdir(parents=True, exist_ok=True)
OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.helpers import _clean, _num, _truthy  # noqa: E402
from src.slots import DAYS, SLOT_TIMES  # noqa: E402
from src.loaders import list_semesters  # noqa: E402

# Semesters are discovered from data/semesters/<name>/courses.xlsx, so a new
# semester (e.g. "sem3") becomes available without any code change.
SEMESTERS = {
    s: (f"Semester {s[3:]}" if s.startswith("sem") else s)
    for s in list_semesters(DATA_DIR / "semesters")
} or {"sem1": "Semester 1"}

COURSE_COLUMNS = [
    "course_code", "course_name", "programme", "level", "cohort",
    "lecturer", "lecture_hours", "practical_hours", "credits",
    "online", "field_work", "hours_per_session", "sessions_per_week", "min_room_size",
    "sections", "split", "size", "group_id", "group_size",
]
ROOM_COLUMNS = ["name", "capacity", "kind"]
COHORT_COLUMNS = ["programme", "level", "section", "size"]
LECTURER_COLUMNS = ["name"]

LECTURERS_FILE = DATA_DIR / "lecturers.xlsx"

SOLVER_CONFIG = "soft_lecturer=False;compact=2;online_in_person=1;physical_never_online=1"

app = FastAPI(title="UMaT-SRID Timetable")

log = logging.getLogger("umat.web")
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(name)s] %(message)s", datefmt="%H:%M:%S")

MAX_BODY_BYTES = 2 * 1024 * 1024  # 2 MB

@app.middleware("http")
async def limit_body_size(request: Request, call_next):
    cl = request.headers.get("content-length")
    if cl and int(cl) > MAX_BODY_BYTES:
        raise HTTPException(413, "Request body too large")
    return await call_next(request)

JOBS = {}

_SOLVE_LOCK = threading.Lock()

# ---- admin gate -----------------------------------------------------------

ADMIN_PASSWORD = os.environ.get("ADMIN_PASSWORD", "").strip()
_hashed_mode = False  # True when security.json stores a salt+hash instead of plaintext


def _hash_password(password, salt=None):
    if salt is None:
        salt = secrets.token_hex(16)
    h = hashlib.pbkdf2_hmac("sha256", password.encode(), salt.encode(), 100_000).hex()
    return salt, h


def _verify_hash(password, salt, stored_hash):
    return secrets.compare_digest(
        hashlib.pbkdf2_hmac("sha256", password.encode(), salt.encode(), 100_000).hex(),
        stored_hash,
    )


if not ADMIN_PASSWORD:
    if SECURITY_FILE.exists():
        try:
            data = json.loads(SECURITY_FILE.read_text(encoding="utf-8"))
            stored_salt = data.get("salt")
            stored_hash = data.get("password_hash") or data.get("password")
            if stored_salt and stored_hash:
                _hashed_mode = True
            elif stored_hash:
                ADMIN_PASSWORD = str(stored_hash)
        except Exception:
            ADMIN_PASSWORD = ""
    if not ADMIN_PASSWORD and not _hashed_mode:
        ADMIN_PASSWORD = secrets.token_urlsafe(18)
        salt, h = _hash_password(ADMIN_PASSWORD)
        SECURITY_FILE.write_text(json.dumps({"salt": salt, "password_hash": h}), encoding="utf-8")
        _cred_file = ROOT / ".admin_credentials"
        _cred_file.write_text(f"Admin password: {ADMIN_PASSWORD}\n", encoding="utf-8")
        log.info("Generated admin password written to %s", _cred_file)

_SESSIONS = {}

# ---- rate-limiting ---------------------------------------------------------

_LOGIN_ATTEMPTS: dict[str, list[float]] = {}
_LOGIN_LOCKOUT = 15 * 60   # seconds
_LOGIN_MAX = 5             # max failures before lockout


def _client_ip(request):
    forwarded = request.headers.get("x-forwarded-for")
    if forwarded and TRUSTED_PROXIES:
        return forwarded.split(",")[0].strip()
    return request.client.host if request.client else "unknown"


def _check_rate(ip):
    cutoff = time.time() - _LOGIN_LOCKOUT
    _LOGIN_ATTEMPTS.setdefault(ip, [])
    _LOGIN_ATTEMPTS[ip] = [t for t in _LOGIN_ATTEMPTS[ip] if t > cutoff]
    return len(_LOGIN_ATTEMPTS[ip]) < _LOGIN_MAX


def _record_failure(ip):
    _LOGIN_ATTEMPTS.setdefault(ip, []).append(time.time())

# Optional: push published timetables to the student app (e.g. on Vercel).
# Set STUDENT_APP_URL (e.g. https://umat-student-app.vercel.app) and
# STUDENT_APP_PUBLISH_SECRET (must match the app's TIMETABLE_PUBLISH_SECRET).
STUDENT_APP_URL = os.environ.get("STUDENT_APP_URL", "").strip().rstrip("/")
STUDENT_APP_PUBLISH_SECRET = os.environ.get("STUDENT_APP_PUBLISH_SECRET", "").strip()

# Comma-separated list of proxy IPs to trust for X-Forwarded-For.
# Leave empty (default) to ignore the header and use request.client.host directly.
TRUSTED_PROXIES = {
    ip.strip() for ip in os.environ.get("TRUSTED_PROXIES", "").split(",") if ip.strip()
}


def require_admin(authorization: str = Header(None)):
    if not authorization:
        raise HTTPException(401, "Admin login required")
    token = authorization.removeprefix("Bearer ").strip()
    exp = _SESSIONS.get(token)
    if exp is None or exp < time.time():
        raise HTTPException(401, "Admin session expired or invalid")
    return True


def _pub_snapshot(sem):
    out = _sem_out(sem) / "published.json"
    if not out.exists():
        raise HTTPException(404, "No timetable published yet")
    return json.loads(out.read_text(encoding="utf-8"))


# ---- CORS: allow the student app to read the public endpoint ---------------

_DEFAULT_CORS = "https://umat-student-app.vercel.app"
_CORS_ORIGINS = [o.strip() for o in os.environ.get("PUBLIC_ALLOW_ORIGINS", _DEFAULT_CORS).split(",") if o.strip()]
app.add_middleware(
    CORSMiddleware,
    allow_origins=_CORS_ORIGINS,
    allow_methods=["GET", "POST", "PUT"],
    allow_headers=["*"],
)


def _sem_path(sem):
    if sem not in SEMESTERS:
        raise HTTPException(404, f"Unknown semester: {sem!r}. Choose from {sorted(SEMESTERS)}.")
    return DATA_DIR / "semesters" / sem


def _sem_out(sem):
    _sem_path(sem)
    d = OUTPUT_DIR / sem
    d.mkdir(parents=True, exist_ok=True)
    return d


def _cache_jobs():
    done = [j for j, s in JOBS.items() if s.get("status") == "done"]
    while len(JOBS) > 20 and done:
        JOBS.pop(done.pop(0), None)


def _course_key(r):
    return (str(r.get("course_code") or "").strip(),
            str(r.get("programme") or "").strip(),
            str(r.get("level") or "").strip())


def _row_sections(r):
    s = str(r.get("sections") or "").strip()
    if s:
        return {x.strip().upper() for x in s.split(",") if x.strip()}
    c = str(r.get("cohort") or "").strip().upper()
    if c in ("A", "B", "AB"):
        tgt = f"{r.get('programme')}{_num(r.get('level'))}-"
        return {tgt + sec for sec in (["A", "B"] if c == "AB" else [c])}
    return set()


def _derivable_sections(prog, lv, cohorts):
    return {f"{prog}{lv}-{str(c.get('section') or '').strip().upper()}"
            for c in cohorts
            if str(c.get("programme") or "").strip() == prog and _num(c.get("level")) == lv
            and str(c.get("section") or "").strip()}


def _code_for_prog(merged_name, prog, fallback):
    """For a merged name like 'CE 158/PM 138', return the code matching *prog*."""
    if not merged_name or "/" not in str(merged_name):
        return fallback
    import re
    for part in str(merged_name).split("/"):
        m = re.search(r"([A-Z]{1,3})\s*(\d+)", part.strip())
        if m and m.group(1).upper() == prog.upper():
            return f"{m.group(1)} {m.group(2)}"
    return fallback


def _collapse_courses(rows, cohorts):
    """Group the expanded per-delivery rows into one row per course+programme+level.

    The page never makes staff enter per-section rows: a course is shown once.
    Unchanged courses round-trip losslessly because the original rows are kept
    verbatim on save. The "special" flag marks courses whose class composition
    cannot be re-derived from the Cohorts tab — attached / cross-programme classes
    such as "CE 158/PM 138" (taught to CE100-A + CE100-B + PM100-A). The loader
    derives sizes from the sections + cohorts tables, so size is never carried in
    the collapsed row."""
    from collections import OrderedDict

    groups = OrderedDict()
    for r in rows:
        key = _course_key(r)
        if not key[0] or not key[1]:
            continue
        groups.setdefault(key, []).append(r)

    out = []
    for (code, prog, lv), rs in groups.items():
        lv_i = _num(lv)

        def first(field):
            for r in rs:
                v = r.get(field)
                if v not in (None, ""):
                    return v
            return ""

        def maxnum(field):
            vals = [_num(r.get(field)) for r in rs if str(r.get(field) or "").strip()]
            return max(vals) if vals else ""

        def mode(field):
            vals = [str(r.get(field) or "").strip() for r in rs if str(r.get(field) or "").strip()]
            return max(set(vals), key=vals.count) if vals else ""

        union = set()
        for r in rs:
            union |= _row_sections(r)
        derivable = _derivable_sections(prog, lv_i, cohorts)
        sections = "" if (union and union == derivable) else ",".join(sorted(union))

        from src.loaders import _canonical_lecturer

        lecturer = ""
        for r in rs:
            raw = str(r.get("lecturer") or "").strip()
            if raw:
                for part in raw.split("/"):
                    canon = _canonical_lecturer(part)
                    if canon:
                        lecturer = canon
                        break
            if lecturer:
                break
        online = "yes" if any(_truthy(r.get("online")) for r in rs) else "no"
        field_work = "yes" if any(_truthy(r.get("field_work")) for r in rs) else "no"
        split_vals = {str(r.get("split") or "").strip().lower() for r in rs if str(r.get("split") or "").strip()}
        split = split_vals.pop() if len(split_vals) == 1 else ""

        merged_name = first("course_name")
        display_code = _code_for_prog(merged_name, prog, code)
        is_merged = bool(merged_name and "/" in str(merged_name))

        if is_merged:
            gids = {str(r.get("group_id") or "").strip() for r in rs if str(r.get("group_id") or "").strip()}
            gid = gids.pop() if len(gids) == 1 else ""
        else:
            gid = first("group_id")

        out.append({
            "course_code": display_code,
            "course_name": display_code if is_merged else merged_name, "programme": prog,
            "level": lv, "cohort": "", "lecturer": lecturer,
            "lecture_hours": maxnum("lecture_hours"), "practical_hours": maxnum("practical_hours"),
            "credits": maxnum("credits"), "online": online, "field_work": field_work,
            "hours_per_session": mode("hours_per_session") or 2, "sessions_per_week": "",
            "min_room_size": maxnum("min_room_size"), "sections": sections, "split": split,
            "size": "", "special": len({SECTION_RE.match(s).group(1) for s in union if SECTION_RE.match(s)}) > 1,
            "group_id": gid,
            "group_size": "" if is_merged else first("group_size"),
        })
    return out


SECTION_RE = re.compile(r"^([A-Z]+)(\d+)-([A-Z]+)$")


def _auto_group_id(row):
    """Return a group_id when the sections span multiple programmes and the
    row doesn't already carry one.  This lets the web-editor round-trip
    cross-programme courses without requiring split_joint.py."""
    if "group_id" in row:
        existing = str(row.get("group_id") or "").strip()
        return existing
    raw = str(row.get("sections") or "").strip()
    if not raw:
        return ""
    secs = [s.strip().upper() for s in raw.split(",") if s.strip()]
    progs = set()
    for s in secs:
        m = SECTION_RE.match(s)
        if m:
            progs.add(m.group(1))
    if len(progs) < 2:
        return ""
    import hashlib as _hl
    sig = "|".join(sorted(secs))
    short = _hl.md5(sig.encode()).hexdigest()[:8]
    return f"auto-{short}"


def _expand_one(row):
    """Expand a single (minimal) course row into the delivery rows the loader
    consumes. Keeps the full teaching hours (TPC) and carries the sections
    verbatim (so attached classes like "CE 158/PM 138" stay combined); the
    loader applies the combined-size > 90 A/B split rule downstream and derives
    sizes from the sections + cohorts tables."""
    code = str(row.get("course_code") or "").strip()
    prog = str(row.get("programme") or "").strip()
    lv = _num(row.get("level"))
    lect_h = _num(row.get("lecture_hours"))
    prac_h = _num(row.get("practical_hours"))
    credits = _num(row.get("credits"))
    hps = row.get("hours_per_session")
    hps = 2 if hps in (None, "") else int(hps)
    online = _truthy(row.get("online"))
    field_work = _truthy(row.get("field_work"))
    gid = _auto_group_id(row)

    base = {
        "course_code": code, "course_name": row.get("course_name"), "programme": prog,
        "level": lv, "cohort": "", "lecturer": row.get("lecturer"),
        "lecture_hours": 0, "practical_hours": 0, "credits": credits,
        "online": "no", "field_work": "no", "hours_per_session": hps,
        "sessions_per_week": row.get("sessions_per_week"), "min_room_size": row.get("min_room_size"),
        "sections": row.get("sections"), "split": row.get("split"), "size": "",
        "group_id": gid, "group_size": row.get("group_size"),
    }

    if field_work:
        row_out = dict(base)
        row_out["field_work"] = "yes"
        row_out["practical_hours"] = prac_h
        return [row_out]

    out = []
    if lect_h > 0:
        r = dict(base)
        r["lecture_hours"] = lect_h
        r["online"] = "yes" if online else "no"
        out.append(r)
    if prac_h > 0:
        r = dict(base)
        r["practical_hours"] = prac_h
        r["online"] = "no"
        out.append(r)
    if not out:
        out.append(dict(base))
    return out


def _expand_courses(collapsed, existing, cohorts):
    """Rebuild expanded rows from the page's collapsed rows. Unchanged courses
    keep their original rows verbatim (so special structures survive); edited or
    new courses are expanded from the minimal fields."""
    from collections import OrderedDict

    existing_groups = OrderedDict()
    for r in existing:
        key = _course_key(r)
        if not key[0] or not key[1]:
            continue
        existing_groups.setdefault(key, []).append(r)

    def editable(r):
        return {k: _clean(r.get(k)) for k in (
            "course_name", "lecturer", "lecture_hours", "practical_hours", "credits",
            "online", "field_work", "hours_per_session", "sessions_per_week",
            "min_room_size", "sections", "split", "size",
            "group_id", "group_size",
        )}

    out = []
    for row in collapsed:
        key = _course_key(row)
        orig = existing_groups.get(key)
        if orig:
            collapsed_orig = next(
                (x for x in _collapse_courses(orig, cohorts) if _course_key(x) == key), None)
            if collapsed_orig is not None and editable(row) == editable(collapsed_orig):
                out.extend(orig)
                continue
        out.extend(_expand_one(row))
    return out


def _read_table(path, columns, key=None):
    if not path.exists():
        return []
    df = pd.read_excel(path)
    rows = []
    for _, row in df.iterrows():
        if key is not None:
            v = row.get(key)
            if v is None or (isinstance(v, float) and v != v):
                continue
            if str(v).strip() == "":
                continue
        rows.append({c: _clean(row.get(c)) for c in columns})
    return rows


def _write_table(path, rows, columns):
    import tempfile
    data = []
    for row in rows:
        out = {}
        for c in columns:
            v = row.get(c)
            if v is None or v == "":
                out[c] = float("nan")
            else:
                out[c] = v
        data.append(out)
    df = pd.DataFrame(data, columns=columns)
    fd, tmp = tempfile.mkstemp(dir=path.parent, suffix=".xlsx")
    try:
        os.close(fd)
        df.to_excel(tmp, index=False, engine="openpyxl")
        os.replace(tmp, path)
    except Exception:
        try: os.unlink(tmp)
        except OSError: pass
        raise
    return len(df)


def _int_value(v, field):
    if v is None or v == "":
        raise ValueError(f"{field} is required")
    try:
        return int(float(v))
    except (TypeError, ValueError):
        raise ValueError(f"{field} must be a whole number, got {v!r}")


def _validate_courses(rows):
    for row in rows:
        code = row.get("course_code") or ""
        if not str(code).strip():
            raise ValueError("every course needs a course_code")
        if not str(row.get("programme") or "").strip():
            raise ValueError(f"{code}: programme is required")
        _int_value(row.get("level"), f"{code}: level")
        cohort = str(row.get("cohort") or "").strip().upper()
        if cohort and cohort not in ("A", "B", "AB"):
            raise ValueError(f"{code}: cohort must be A, B or AB")
        online = str(row.get("online") or "").strip().lower()
        if online and online not in ("yes", "no", "y", "n", "1", "0", "true", "false"):
            raise ValueError(f"{code}: online must be yes/no")
        field_work = str(row.get("field_work") or "").strip().lower()
        if field_work and field_work not in ("yes", "no", "y", "n", "1", "0", "true", "false"):
            raise ValueError(f"{code}: field_work must be yes/no")
        hps = row.get("hours_per_session")
        if hps not in (None, ""):
            hps_i = _int_value(hps, f"{code}: hours_per_session")
            if not 1 <= hps_i <= 12:
                raise ValueError(f"{code}: hours_per_session must be 1..12")


def _validate_cohort_existence(rows, cohorts):
    """Check that every course's sections have matching rows in cohorts.xlsx."""
    valid = set()
    for c in cohorts:
        p = str(c.get("programme") or "").strip()
        l = str(c.get("level") or "").strip()
        s = str(c.get("section") or "").strip().upper()
        if p and l and s:
            valid.add(f"{p}{l}-{s}")
    for row in rows:
        code = str(row.get("course_code") or "").strip()
        raw = str(row.get("sections") or "").strip()
        if not raw:
            continue
        for s in raw.split(","):
            s = s.strip().upper()
            if s and s not in valid:
                raise ValueError(
                    f"{code}: section {s!r} has no matching cohort. "
                    f"Add {s} to cohorts.xlsx first."
                )


def _validate_rooms(rows):
    for row in rows:
        if not str(row.get("name") or "").strip():
            raise ValueError("every room needs a name")
        _int_value(row.get("capacity"), f"room {row.get('name')}: capacity")


def _validate_cohorts(rows):
    for row in rows:
        if not str(row.get("programme") or "").strip():
            raise ValueError("every cohort needs a programme")
        _int_value(row.get("level"), f"{row.get('programme')}: level")
        section = str(row.get("section") or "").strip().upper()
        if section not in ("A", "B"):
            raise ValueError(f"{row.get('programme')}: section must be A or B")
        _int_value(row.get("size"), f"{row.get('programme')}{row.get('level')}: size")


def _validate_lecturers(rows):
    seen = set()
    for row in rows:
        name = str(row.get("name") or "").strip()
        if not name:
            raise ValueError("every lecturer needs a name")
        if name in seen:
            raise ValueError(f"duplicate lecturer: {name}")
        seen.add(name)


def _save(path, rows, columns, validator, label):
    try:
        validator(rows)
        _write_table(path, rows, columns)
    except PermissionError:
        raise HTTPException(409, f"{label} file is open in Excel. Close it and try again.")
    except ValueError as e:
        raise HTTPException(400, str(e))
    except Exception as e:
        raise HTTPException(500, f"Failed to save {label}: {e}")
    return {"ok": True, "rows": len(rows)}


@app.get("/api/health")
def health():
    return {"ok": True}


class LoginRequest(BaseModel):
    password: str = ""


@app.post("/api/auth/login")
def login(payload: LoginRequest, request: Request):
    ip = _client_ip(request)
    if not _check_rate(ip):
        raise HTTPException(429, "Too many failed attempts. Try again later.")

    password = payload.password
    if _hashed_mode:
        data = json.loads(SECURITY_FILE.read_text(encoding="utf-8"))
        ok = _verify_hash(password, data["salt"], data["password_hash"])
    else:
        ok = secrets.compare_digest(password, ADMIN_PASSWORD)

    if not ok:
        _record_failure(ip)
        raise HTTPException(401, "Wrong password")

    _LOGIN_ATTEMPTS.pop(ip, None)
    token = secrets.token_hex(16)
    _SESSIONS[token] = time.time() + 12 * 3600
    return {"token": token}


@app.get("/api/auth/check")
def check_auth(_: bool = Depends(require_admin)):
    return {"ok": True}


@app.post("/api/publish")
def publish(semester: str = "sem2", _: bool = Depends(require_admin)):
    path = _sem_out(semester) / "timetable.xlsx"
    if not path.exists():
        raise HTTPException(404, "No timetable yet. Generate it first.")
    table = get_timetable(semester)
    token = secrets.token_hex(16)
    snapshot = {
        "semester": semester,
        "published_at": datetime.now(timezone.utc).isoformat(),
        "token": token,
        "summary": table["summary"],
        "rows": table["rows"],
    }
    (_sem_out(semester) / "published.json").write_text(json.dumps(snapshot), encoding="utf-8")
    out = {"ok": True, "semester": semester, "url": f"/api/public/timetable?semester={semester}&token={token}"}
    if STUDENT_APP_URL and STUDENT_APP_PUBLISH_SECRET:
        out["student_app"] = _push_to_student_app(semester, table["rows"])
    return out


def _push_to_student_app(semester, rows):
    """POST the published rows to the student app so phones see the new
    timetable. Returns {"ok": bool, "detail": str}."""
    import urllib.request
    import urllib.error
    payload = json.dumps({"semester": semester, "rows": rows}).encode("utf-8")
    req = urllib.request.Request(
        STUDENT_APP_URL + "/api/timetable/publish",
        data=payload,
        headers={
            "Content-Type": "application/json",
            "x-publish-secret": STUDENT_APP_PUBLISH_SECRET,
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            reply = json.loads(resp.read().decode("utf-8"))
        return {"ok": bool(reply.get("ok")), "detail": reply.get("count", "?")}
    except urllib.error.HTTPError as e:
        return {"ok": False, "detail": f"student app rejected (HTTP {e.code})"}
    except Exception as e:
        return {"ok": False, "detail": str(e)}


@app.get("/api/public/timetable")
def public_timetable(semester: str = "sem2", token: str = ""):
    snap = _pub_snapshot(semester)
    if not token or not secrets.compare_digest(str(snap.get("token") or ""), str(token)):
        raise HTTPException(403, "Invalid or missing publish token")
    return {k: v for k, v in snap.items() if k != "token"}


@app.get("/api/meta")
def meta():
    return {"days": DAYS, "slot_times": SLOT_TIMES,
            "semesters": SEMESTERS,
            "course_columns": COURSE_COLUMNS,
            "room_columns": ROOM_COLUMNS,
            "cohort_columns": COHORT_COLUMNS,
            "lecturer_columns": LECTURER_COLUMNS,
            "student_app_url": STUDENT_APP_URL}


@app.get("/api/courses")
def get_courses(semester: str = "sem2"):
    rows = _read_table(_sem_path(semester) / "courses.xlsx", COURSE_COLUMNS, key="course_code")
    cohorts = _read_table(_sem_path(semester) / "cohorts.xlsx", COHORT_COLUMNS, key="programme")
    return _collapse_courses(rows, cohorts)


@app.put("/api/courses")
def put_courses(payload: list[dict], semester: str = "sem2", _: bool = Depends(require_admin)):
    existing = _read_table(_sem_path(semester) / "courses.xlsx", COURSE_COLUMNS, key="course_code")
    cohorts = _read_table(_sem_path(semester) / "cohorts.xlsx", COHORT_COLUMNS, key="programme")
    rows = _expand_courses(payload, existing, cohorts)
    _validate_cohort_existence(rows, cohorts)
    with _SOLVE_LOCK:
        return _save(_sem_path(semester) / "courses.xlsx", rows, COURSE_COLUMNS, _validate_courses, "courses")


@app.post("/api/courses/split")
def split_cross_programme(payload: list[dict], semester: str = "sem2", _: bool = Depends(require_admin)):
    """Split cross-programme rows into per-programme rows with auto-assigned group_ids."""
    cohorts = _read_table(_sem_path(semester) / "cohorts.xlsx", COHORT_COLUMNS, key="programme")
    cohort_map = {}
    for c in cohorts:
        key = f"{c['programme']}{c['level']}-{c['section']}"
        cohort_map[key] = c

    out = []
    for row in payload:
        code = str(row.get("course_code") or "").strip()
        raw_secs = str(row.get("sections") or "").strip()
        secs = [s.strip().upper() for s in raw_secs.split(",") if s.strip()] if raw_secs else []
        progs = {}
        for s in secs:
            m = SECTION_RE.match(s)
            if m:
                progs.setdefault(m.group(1), []).append(s)
        if len(progs) < 2:
            out.append(row)
            continue
        gid = row.get("group_id") or _auto_group_id(row)
        for prog, prog_secs in sorted(progs.items()):
            child = dict(row)
            child["programme"] = prog
            child["sections"] = ",".join(sorted(prog_secs))
            size = sum(cohort_map.get(f"{prog}{row.get('level', 100)}-{s}", {}).get("size", 0) or 0 for s in prog_secs)
            child["size"] = size
            child["group_id"] = gid
            child["group_size"] = row.get("size") or ""
            out.append(child)
    return out


@app.get("/api/rooms")
def get_rooms(semester: str = "sem2"):
    return _read_table(_sem_path(semester) / "rooms.xlsx", ROOM_COLUMNS, key="name")


@app.put("/api/rooms")
def put_rooms(payload: list[dict], semester: str = "sem2", _: bool = Depends(require_admin)):
    with _SOLVE_LOCK:
        return _save(_sem_path(semester) / "rooms.xlsx", payload, ROOM_COLUMNS, _validate_rooms, "rooms")


@app.get("/api/cohorts")
def get_cohorts(semester: str = "sem2"):
    return _read_table(_sem_path(semester) / "cohorts.xlsx", COHORT_COLUMNS, key="programme")


@app.put("/api/cohorts")
def put_cohorts(payload: list[dict], semester: str = "sem2", _: bool = Depends(require_admin)):
    with _SOLVE_LOCK:
        return _save(_sem_path(semester) / "cohorts.xlsx", payload, COHORT_COLUMNS, _validate_cohorts, "cohorts")


@app.get("/api/lecturers")
def get_lecturers():
    from src.loaders import _canonical_lecturer

    rows = _read_table(LECTURERS_FILE, LECTURER_COLUMNS, key="name")
    seen = set()
    out = []
    for r in rows:
        canonical = _canonical_lecturer(r.get("name"))
        if canonical and canonical not in seen:
            seen.add(canonical)
            r["name"] = canonical
            out.append(r)
    return out


@app.put("/api/lecturers")
def put_lecturers(payload: list[dict], _: bool = Depends(require_admin)):
    with _SOLVE_LOCK:
        return _save(LECTURERS_FILE, payload, LECTURER_COLUMNS, _validate_lecturers, "lecturers")


def _build_problem(sem):
    from src.loaders import load_problem

    problem = load_problem(_sem_path(sem))
    problem["soft_lecturer"] = False
    return problem


def _run_solve(job_id, time_limit, semester):
    job = JOBS[job_id]
    with _SOLVE_LOCK:
        try:
            from src.export import export_all
            from src.solver import repair_assignments, solve, _verify

            job["progress"] = "Loading data..."
            problem = _build_problem(semester)

            note = ""

            def live_cb(phase):
                def cb(info):
                    info = dict(info)
                    info["phase"] = phase
                    job["live"] = info
                return cb

            job["progress"] = "Solving..."
            job["live"] = {"phase": "phase1", "elapsed": 0}
            log.info("solve start semester=%s time_limit=%s", semester, time_limit)
            # Fast greedy solver (much faster than CP-SAT for this data)
            from fast_solver import greedy_phase1
            assignments = greedy_phase1(problem, time_limit=min(max(time_limit, 60), 300), seed=random.randint(1, 2**31 - 1))
            
            # Convert to result-like object
            from src.solver import _verify
            class GreedyResult:
                def __init__(self, assignments, problem):
                    self.assignments = assignments
                    self.checks = _verify(assignments, problem)
                    self.status = "FEASIBLE" if assignments else "NO SOLUTION"
                    self.objective = 0
            
            result = GreedyResult(assignments, problem)
            if result.status in ("OPTIMAL", "FEASIBLE"):
                from regen import postprocess
                job["progress"] = "Packing sessions together (reducing idle gaps)..."
                packed = postprocess(problem, result.assignments, semester)
                checks = _verify(packed, problem)
                if any(checks[k] for k in ("section", "lecturer", "room")):
                    # Never ship a conflicted timetable: the solver's own output
                    # is hard-conflict-free, so fall back to it if packing could
                    # not be fully repaired.
                    job["note"] = "Packing left overlaps that could not be repaired; shipped the solver's clean solution."
                    packed = phase1.assignments if phase1.status in ("OPTIMAL", "FEASIBLE") else result.assignments
                    checks = _verify(packed, problem)
                result.assignments = packed
                result.checks = checks

            summary = {
                "status": result.status,
                "objective": round(result.objective) if result.objective != float("inf") else None,
                "conflicts": {k: result.checks.get(k, 0) for k in ("section", "room", "capacity")},
                "lecturer_overlaps": result.checks.get("lecturer", 0),
                "sessions": len(problem["sessions"]),
                "sections": len(problem["sections"]),
                "lecturers": len(problem["lecturers"]),
                "rooms": len(problem["rooms"]),
                "built_from": SEMESTERS[semester],
            }
            if result.status not in ("OPTIMAL", "FEASIBLE"):
                job.update(status="done", ok=False, summary=summary, note=note, progress="")
                return

            sem_label = f"{SEMESTERS.get(semester, 'Semester')} TIME TABLE".upper()
            out_path = export_all(problem, result, _sem_out(semester), semester_label=sem_label)
            (_sem_out(semester) / "solve_result.json").write_text(
                json.dumps(summary, indent=2), encoding="utf-8"
            )
            job.update(status="done", ok=True, summary=summary, note=note,
                       progress="", out=str(out_path))
            log.info("solve done semester=%s status=%s sessions=%s",
                     semester, summary.get("status"), summary.get("sessions"))
        except Exception as e:
            job.update(status="done", ok=False, progress="", error=str(e))
            log.exception("solve failed semester=%s", semester)


class SolveRequest(BaseModel):
    time_limit: float = 300
    semester: str = "sem2"


@app.post("/api/solve")
def start_solve(payload: SolveRequest = None, _: bool = Depends(require_admin)):
    if payload is None:
        payload = SolveRequest()
    _sem_path(payload.semester)
    time_limit = max(1.0, min(payload.time_limit, 3600))
    _cache_jobs()
    job_id = uuid.uuid4().hex[:12]
    JOBS[job_id] = {"status": "running", "progress": "Queued", "summary": None, "error": None, "note": ""}
    t = threading.Thread(target=_run_solve, args=(job_id, time_limit, payload.semester), daemon=True)
    t.start()
    return {"job_id": job_id}


@app.get("/api/solve/{job_id}")
def get_job(job_id: str):
    job = JOBS.get(job_id)
    if not job:
        raise HTTPException(404, "job not found")
    return job


@app.get("/api/timetable")
def get_timetable(semester: str = "sem2"):
    summary = {}
    if (_sem_out(semester) / "solve_result.json").exists():
        summary = json.loads((_sem_out(semester) / "solve_result.json").read_text(encoding="utf-8"))
    json_path = _sem_out(semester) / "timetable_rows.json"
    if json_path.exists():
        rows = json.loads(json_path.read_text(encoding="utf-8"))
        return {"summary": summary, "rows": rows}
    return {"summary": summary, "rows": [], "note": "No timetable generated yet. Run the solver first."}


@app.get("/api/timetable.xlsx")
def download_timetable(semester: str = "sem2"):
    path = _sem_out(semester) / "timetable.xlsx"
    if not path.exists():
        raise HTTPException(404, "No timetable yet. Run the solver first.")
    return FileResponse(path, filename=f"timetable_{semester}.xlsx")


@app.get("/")
def index():
    return FileResponse(WEB_DIR / "static" / "index.html", headers={"Cache-Control": "no-cache"})


app.mount("/static", StaticFiles(directory=WEB_DIR / "static"), name="static")
