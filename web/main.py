import json
import os
import random
import secrets
import sys
import threading
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd
from fastapi import Depends, FastAPI, Header, HTTPException
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

JOBS = {}

_SOLVE_LOCK = threading.Lock()

# ---- admin gate -----------------------------------------------------------

ADMIN_PASSWORD = os.environ.get("ADMIN_PASSWORD", "").strip()
if not ADMIN_PASSWORD:
    if SECURITY_FILE.exists():
        try:
            ADMIN_PASSWORD = str(json.loads(SECURITY_FILE.read_text(encoding="utf-8")).get("password") or "")
        except Exception:
            ADMIN_PASSWORD = ""
    if not ADMIN_PASSWORD:
        ADMIN_PASSWORD = "admin"
        SECURITY_FILE.write_text(json.dumps({"password": ADMIN_PASSWORD}), encoding="utf-8")

_SESSIONS = {}

# Optional: push published timetables to the student app (e.g. on Vercel).
# Set STUDENT_APP_URL (e.g. https://umat-student-app.vercel.app) and
# STUDENT_APP_PUBLISH_SECRET (must match the app's TIMETABLE_PUBLISH_SECRET).
STUDENT_APP_URL = os.environ.get("STUDENT_APP_URL", "").strip().rstrip("/")
STUDENT_APP_PUBLISH_SECRET = os.environ.get("STUDENT_APP_PUBLISH_SECRET", "").strip()


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


# ---- CORS: allow the other platform to read the public endpoint ------------

_CORS_ORIGINS = [o.strip() for o in os.environ.get("PUBLIC_ALLOW_ORIGINS", "*").split(",") if o.strip()]
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


def _clean(value):
    if value is None:
        return ""
    if isinstance(value, float):
        if value != value:  # NaN
            return ""
        return int(value) if value.is_integer() else value
    if isinstance(value, bool):
        return "yes" if value else "no"
    return str(value)


def _num(value):
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return 0


def _truthy(value):
    if value is None:
        return False
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in ("yes", "y", "1", "true", "t")


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

        lecturers = sorted({str(r.get("lecturer") or "").strip() for r in rs if str(r.get("lecturer") or "").strip()})
        online = "yes" if any(_truthy(r.get("online")) for r in rs) else "no"
        field_work = "yes" if any(_truthy(r.get("field_work")) for r in rs) else "no"
        split_vals = {str(r.get("split") or "").strip().lower() for r in rs if str(r.get("split") or "").strip()}
        split = split_vals.pop() if len(split_vals) == 1 else ""

        out.append({
            "course_code": code, "course_name": first("course_name"), "programme": prog,
            "level": lv, "cohort": "", "lecturer": " / ".join(lecturers),
            "lecture_hours": maxnum("lecture_hours"), "practical_hours": maxnum("practical_hours"),
            "credits": maxnum("credits"), "online": online, "field_work": field_work,
            "hours_per_session": mode("hours_per_session") or 2, "sessions_per_week": "",
            "min_room_size": maxnum("min_room_size"), "sections": sections, "split": split,
            "size": "", "special": bool(sections),
            "group_id": first("group_id"), "group_size": first("group_size"),
        })
    return out


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

    base = {
        "course_code": code, "course_name": row.get("course_name"), "programme": prog,
        "level": lv, "cohort": "", "lecturer": row.get("lecturer"),
        "lecture_hours": 0, "practical_hours": 0, "credits": credits,
        "online": "no", "field_work": "no", "hours_per_session": hps,
        "sessions_per_week": row.get("sessions_per_week"), "min_room_size": row.get("min_room_size"),
        "sections": row.get("sections"), "split": row.get("split"), "size": "",
        "group_id": row.get("group_id"), "group_size": row.get("group_size"),
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
    df.to_excel(path, index=False)
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
def login(payload: LoginRequest):
    if payload.password != ADMIN_PASSWORD:
        raise HTTPException(401, "Wrong password")
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
    return _save(_sem_path(semester) / "courses.xlsx", rows, COURSE_COLUMNS, _validate_courses, "courses")


@app.get("/api/rooms")
def get_rooms(semester: str = "sem2"):
    return _read_table(_sem_path(semester) / "rooms.xlsx", ROOM_COLUMNS, key="name")


@app.put("/api/rooms")
def put_rooms(payload: list[dict], semester: str = "sem2", _: bool = Depends(require_admin)):
    return _save(_sem_path(semester) / "rooms.xlsx", payload, ROOM_COLUMNS, _validate_rooms, "rooms")


@app.get("/api/cohorts")
def get_cohorts(semester: str = "sem2"):
    return _read_table(_sem_path(semester) / "cohorts.xlsx", COHORT_COLUMNS, key="programme")


@app.put("/api/cohorts")
def put_cohorts(payload: list[dict], semester: str = "sem2", _: bool = Depends(require_admin)):
    return _save(_sem_path(semester) / "cohorts.xlsx", payload, COHORT_COLUMNS, _validate_cohorts, "cohorts")


@app.get("/api/lecturers")
def get_lecturers():
    return _read_table(LECTURERS_FILE, LECTURER_COLUMNS, key="name")


@app.put("/api/lecturers")
def put_lecturers(payload: list[dict], _: bool = Depends(require_admin)):
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
            # Feasibility-only solve with hard section/lecturer/room constraints:
            # any returned solution is already conflict-free. A second pass with
            # an optimization objective is intentionally skipped - on this data
            # CP-SAT cannot even finish presolve for that model within minutes.
            phase1 = solve(problem, time_limit=min(max(time_limit, 120), 300),
                           minimize_objective=False, feasibility_jump=True, seed=random.randint(1, 2**31 - 1), progress_cb=live_cb("phase1"))
            result = phase1
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
                "conflicts": {k: result.checks[k] for k in ("section", "room", "capacity")},
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

            out_path = export_all(problem, result, _sem_out(semester))
            (_sem_out(semester) / "solve_result.json").write_text(
                json.dumps(summary, indent=2), encoding="utf-8"
            )
            job.update(status="done", ok=True, summary=summary, note=note,
                       progress="", out=str(out_path))
        except Exception as e:
            job.update(status="done", ok=False, progress="", error=str(e))


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
    path = _sem_out(semester) / "timetable.xlsx"
    if not path.exists():
        return {"summary": summary, "rows": []}
    df = pd.read_excel(path, sheet_name=0)
    rows = []
    for _, r in df.iterrows():
        rows.append({
            "day": _clean(r.get("Day")),
            "time": _clean(r.get("Time")),
            "room": _clean(r.get("Room")),
            "code": _clean(r.get("Course Code")),
            "name": _clean(r.get("Course")),
            "programme": _clean(r.get("Programme")),
            "level": _clean(r.get("Level")),
            "cohort": _clean(r.get("Cohort")),
            "lecturer": _clean(r.get("Lecturer")),
            "type": _clean(r.get("Type")),
            "duration": int(r["Duration"]) if _clean(r.get("Duration")) else 1,
        })
    return {"summary": summary, "rows": rows}


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
