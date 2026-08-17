"""Build the SEM 2 semester dataset from the six department course-distribution files.

Writes data/semesters/sem2/{courses.xlsx, rooms.xlsx, cohorts.xlsx}.
Course rows use the solver's courses.xlsx schema.  Weekly hours come from the
department files' FINAL TEACHING LOAD; the online flag is borrowed from the
assembled SEM 2 teaching timetable (matched by course code + cohort).

Run:  .venv\\Scripts\\python.exe tools\\build_sem2.py
"""

import re
import shutil
import sys
from collections import Counter, defaultdict
from pathlib import Path

import openpyxl
import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
INP = ROOT / "data" / "input"
BASE = ROOT / "data"
OUT = ROOT / "data" / "semesters" / "sem2"

DEPT_FILES = [
    "2nd Sem. 2025-26 Draft 2-Electrical and Electronic Course Distribution (1).xlsx",
    "2nd sem.2025-26 Draft 3-Mechanical Engineering Course Distribution-Sem2-2026.xlsx",
    "Final Course Distribution - GMCV - 2nd Semester 2026  -19 May 2026 (2).xlsx",
    "Final Course_Distribution_Semester_2_GLES_2025_2026 (4).xlsx",
    "Final-Computer Science and Engineering Course Distribution-Sem2-2026 (3).xlsx",
    "Final-Mathematics Course Distribution-Sem2-2026 (2).xlsx",
]
SEM2_TT = "FINAL TEACHING TIMETABLE_SRID (SEM 2_2025_2026) .xlsx"

COURSE_HEADERS = [
    "course_code", "course_name", "programme", "level", "cohort",
    "lecturer", "lecture_hours", "practical_hours", "credits",
    "online", "field_work", "hours_per_session", "sessions_per_week", "min_room_size",
]


def fnum(v):
    try:
        f = float(str(v).strip())
        return f if f == f else 0.0
    except Exception:
        return 0.0


def psum(p):
    return sum(int(x) for x in re.findall(r"\d+", str(p)))


def clean_code(raw):
    s = re.sub(r"[#*]", "", str(raw).strip())
    m = re.match(r"([A-Z]{1,3})\s*(\d{2,3})", s)
    if not m:
        return None, None, None
    suffix = re.search(r"(\d+)\s*([AB])\s*$", s)
    return m.group(1), int(m.group(2)), (suffix.group(2) if suffix else None)


def parse_sheet(xl, sheet):
    df = xl.parse(sheet, header=None)
    records = []
    for i in range(len(df)):
        a = [v if v is not None else "" for v in df.iloc[i].tolist()[:12]]
        if not re.match(r"^\d+\s*$", str(a[0]).strip()):
            continue
        if not re.match(r"[A-Z]{1,3}\s*\d", str(a[1]).strip()):
            continue
        pref, num, sec = clean_code(a[1])
        if pref is None:
            continue
        load = fnum(a[9]) if len(a) > 9 and fnum(a[9]) else fnum(a[6]) if len(a) > 6 else 0.0
        records.append({
            "code_raw": str(a[1]).strip(),
            "prefix": pref, "num": num, "section": sec,
            "name": str(a[2]).strip(),
            "T": fnum(a[3]), "P_raw": str(a[4]),
            "C": fnum(a[5]), "load": load,
            "students": fnum(a[8]) if len(a) > 8 else 0,
            "exam1": str(a[10]).strip() if len(a) > 10 else "",
            "exam2": str(a[11]).strip() if len(a) > 11 else "",
        })
    return records


def load_sections(coh_path):
    df = pd.read_excel(coh_path)
    sections = set()
    for _, r in df.iterrows():
        sections.add((str(r["programme"]).strip().upper(), int(r["level"]), str(r["section"]).strip().upper()))
    return sections


def borrow_online_flags():
    """Map (course_code, cohort) -> online from the SEM 2 teaching timetable."""
    sys.path.insert(0, str(ROOT))
    from src.extract import extract_timetable

    tmp = ROOT / "output" / "_sem2_tt"
    tmp.mkdir(parents=True, exist_ok=True)
    shutil.copy(BASE / "cohorts.xlsx", tmp / "cohorts.xlsx")
    extract_timetable(INP / SEM2_TT, tmp, None)
    df = pd.read_excel(tmp / "courses.xlsx")
    flags = {}
    for _, r in df.iterrows():
        code = str(r["course_code"]).strip()
        coh = str(r["cohort"]).strip().upper()
        if str(r["online"]).strip().lower() in ("yes", "y", "1", "true"):
            flags[(code, coh)] = True
    return flags


def room_capacity_map():
    caps = {}
    for path in (OUT / "rooms.xlsx", BASE / "rooms.xlsx", ROOT / "output" / "_sem2_tt" / "rooms.xlsx"):
        if Path(path).exists():
            df = pd.read_excel(path)
            for _, r in df.iterrows():
                caps[str(r["name"]).strip()] = int(r["capacity"])
            if caps:
                break
    return caps


def real_sizes():
    """Course sizes calibrated to the rooms actually used in the SEM 2
    teaching timetable: the school's real attendance per class.  Combined AB
    classes the school books into 80-cap rooms are sized 80, not the sum of
    the two intake streams."""
    sys.path.insert(0, str(ROOT))
    import src.extract as ex

    secs = ex.load_sections(BASE)
    caps = room_capacity_map()
    m = defaultdict(set)
    for rec in ex.collect_records(INP / SEM2_TT):
        offers, _ = ex.parse_cell(rec)
        if ex.venue_kind(rec["room"]) in ("online", "field"):
            continue
        room = re.sub(r"\s*\(\d+\)", "", rec["room"]).strip()
        cap = caps.get(room, 80)
        for o in offers:
            if o["prefix"] == "RT":
                continue
            pool = secs.get((o["prog"], o["level"]))
            if pool is None:
                continue
            cohort = ex.resolve_cohort(pool, o["section"])
            m[(o["code"], cohort)].add(cap)
    # a class is sized by the room it USES MOST, not its occasional biggest
    # venue (e.g. a class that normally meets in SR 7B but once used the
    # auditorium is still an 80-seat class)
    return {k: Counter(v).most_common(1)[0][0] for k, v in m.items()}


# Field-work course names that are field-based by nature (even when absent from
# the assembled teaching timetable).  Everything else in the field-work set is
# detected from the FIELD WORK venue cells in the SEM 2 timetable itself.
FIELD_NAME = re.compile(r"FIELD\s*TRIP|ENGINEERING\s+PRACTICE", re.I)


def field_work_codes():
    """Course codes whose SEM 2 timetable cells sit in a FIELD WORK venue."""
    sys.path.insert(0, str(ROOT))
    import src.extract as ex

    codes = set()
    for rec in ex.collect_records(INP / SEM2_TT):
        if ex.venue_kind(rec["room"]) != "field":
            continue
        offers, _ = ex.parse_cell(rec)
        for o in offers:
            codes.add(o["code"])
    return codes


def is_field_work(code, name, field_codes):
    """A course is field work if it is booked into a FIELD WORK venue or its
    dept name is field-based (Fieldtrip / Engineering Practice).  Language
    courses the timetable happens to park in a FIELD WORK row stay online."""
    if re.search(r"\bFRENCH\b", name, re.I):
        return False
    if code in field_codes:
        return True
    return bool(FIELD_NAME.search(name))


def main():
    sections = load_sections(BASE / "cohorts.xlsx")

    records = []
    for fn in DEPT_FILES:
        f = INP / fn
        print(f"Reading {f.name}")
        xl = pd.ExcelFile(f)
        for sn in xl.sheet_names:
            records.extend(parse_sheet(xl, sn))

    print("dept course rows parsed:", len(records))
    print("prefix counts:", dict(Counter(r["prefix"] for r in records)))

    # dept master keyed by (code, cohort) and by code.  The timetable teaches
    # many courses combined (AB) while the dept files list them per cohort, so
    # we must be able to fall back to a code-only match.
    master = {}
    by_code = {}
    for r in records:
        key = (r["prefix"], r["num"])
        has_ab = any(s[0] == r["prefix"] and s[1] == key[1] // 100 * 100 and s[2] == "B" for s in sections)
        cohort = r["section"] if r["section"] else ("AB" if has_ab else "A")
        code = f"{r['prefix']} {r['num']}"
        info = {
            "name": r["name"],
            "lecturer": r["exam1"] or r["exam2"],
            "T": r["T"], "P": psum(r["P_raw"]), "C": r["C"], "load": r["load"],
        }
        master.setdefault((code, cohort), info)
        by_code.setdefault(code, []).append(info)
    print("dept master courses:", len(master))

    # timetable delivery pattern: (code, cohort) -> (name, lecturer, hours/sessions, online)
    tmp = ROOT / "output" / "_sem2_tt"
    tt_path = tmp / "courses.xlsx"
    if not tt_path.exists():
        sys.path.insert(0, str(ROOT))
        from src.extract import extract_timetable

        tmp.mkdir(parents=True, exist_ok=True)
        shutil.copy(BASE / "cohorts.xlsx", tmp / "cohorts.xlsx")
        extract_timetable(INP / SEM2_TT, tmp, None)
    tt = pd.read_excel(tt_path)
    sizes = real_sizes()
    field_codes = field_work_codes()

    rows = []
    matched = 0
    matched_code = 0
    unmatched = 0
    for _, r in tt.iterrows():
        code = str(r["course_code"]).strip()
        cohort = str(r["cohort"]).strip().upper()
        dept = master.get((code, cohort))
        if dept is None:
            # combined class taught to AB; pick the first dept row for that code
            pool = by_code.get(code)
            dept = pool[0] if pool else None
            if dept:
                matched_code += 1
            else:
                unmatched += 1
        else:
            matched += 1
        if dept:
            name = dept["name"]
            lecturer = dept["lecturer"]
        else:
            name = str(r.get("course_name") or "").strip()
            lecturer = str(r.get("lecturer") or "").strip()
        field_work = is_field_work(code, name, field_codes)
        rows.append({
            "course_code": code,
            "course_name": name,
            "programme": str(r["programme"]).strip(),
            "level": int(r["level"]),
            "cohort": cohort,
            "lecturer": lecturer,
            "lecture_hours": float(r.get("lecture_hours", 0) or 0),
            "practical_hours": float(r.get("practical_hours", 0) or 0),
            "credits": float(r.get("credits", 0) or 0),
            "online": "yes" if str(r["online"]).strip().lower() in ("yes", "y", "1", "true") and not field_work else "no",
            "field_work": "yes" if field_work else "no",
            "hours_per_session": int(r["hours_per_session"]),
            "sessions_per_week": int(r["sessions_per_week"]),
            "min_room_size": str(r.get("min_room_size") or "").strip(),
            "sections": str(r.get("sections") or "").strip(),
            "size": sizes.get((code, cohort), 0) or "",
        })

    # Field-work courses listed in the department files but absent from the
    # assembled SEM 2 timetable (the school did not schedule them).  They are
    # still offered: give them the same 2x2h delivery as the other field-work
    # practicals so the solver can place them.
    missing_field = [("CE 356", "CE", 300), ("MC 356", "MC", 300), ("ES 252", "ES", 200)]
    for code, prog, level in missing_field:
        recs = [r for r in records if f"{r['prefix']} {r['num']}" == code]
        if not recs:
            continue
        for sec in ("A", "B"):
            r = next((x for x in recs if (x["section"] or "").upper() == sec), recs[0])
            rows.append({
                "course_code": code,
                "course_name": r["name"],
                "programme": prog,
                "level": level,
                "cohort": sec,
                "lecturer": r["exam1"] or r["exam2"],
                "lecture_hours": 0.0,
                "practical_hours": 4.0,
                "credits": 1.0,
                "online": "no",
                "field_work": "yes",
                "hours_per_session": 2,
                "sessions_per_week": 2,
                "min_room_size": "",
                "sections": f"{prog}{level}-{sec}",
                "size": "",
            })
        print(f"added missing field-work course: {code} ({recs[0]['name']})")

    # dept courses not present in the SEM 2 timetable (e.g. SEM 1 offerings)
    tt_codes = set(str(r["course_code"]).strip() for _, r in tt.iterrows())
    absent = sorted(k for k in master if k[0] not in tt_codes)
    print(f"timetable rows matched by (code,cohort): {matched} | by code only: {matched_code} | unmatched: {unmatched}")
    print(f"dept courses absent from SEM 2 timetable: {len(absent)} (not added)")
    for k in absent[:15]:
        print("   ", k, "|", master[k]["name"])

    OUT.mkdir(parents=True, exist_ok=True)
    df = pd.DataFrame(rows)
    df.to_excel(OUT / "courses.xlsx", index=False)
    shutil.copy(BASE / "rooms.xlsx", OUT / "rooms.xlsx")
    shutil.copy(BASE / "cohorts.xlsx", OUT / "cohorts.xlsx")
    print(f"Wrote {len(rows)} course rows to {OUT / 'courses.xlsx'}")
    print("total weekly hours:", round(df['lecture_hours'].sum() + df['practical_hours'].sum(), 1))
    print("sessions (spw sum):", int(df['sessions_per_week'].sum()))
    print("online count:", int((df['online'] == 'yes').sum()))
    print("field_work count:", int((df['field_work'] == 'yes').sum()))
    print("blank names:", int((df['course_name'].astype(str).str.strip() == '').sum()))
    print("blank lecturers:", int((df['lecturer'].astype(str).str.strip() == '').sum()))


if __name__ == "__main__":
    main()
