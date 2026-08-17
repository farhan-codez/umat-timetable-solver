"""Build the SEM 1 semester dataset from the SEM 1 teaching-timetable draft.

Writes data/semesters/sem1/{courses.xlsx, rooms.xlsx, cohorts.xlsx}.
SEM 1 has no course-distribution files, so courses come from the SEM 1 DRAFT
timetable itself (the user's chosen source for semester one).

Run:  .venv\\Scripts\\python.exe tools\\build_sem1.py
"""

import re
import shutil
import sys
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
INP = ROOT / "data" / "input"
BASE = ROOT / "data"
OUT = ROOT / "data" / "semesters" / "sem1"

SEM1_TT = "DRAFT_FINAL TEACHING TIME TABLE_SRID (SEM 1_2025_2026).xlsx"


def room_capacity_map():
    caps = {}
    for path in (OUT / "rooms.xlsx", BASE / "rooms.xlsx", ROOT / "output" / "_sem1_tt" / "rooms.xlsx"):
        if Path(path).exists():
            df = pd.read_excel(path)
            for _, r in df.iterrows():
                caps[str(r["name"]).strip()] = int(r["capacity"])
            if caps:
                break
    return caps


def real_sizes():
    """Course sizes calibrated to the rooms actually used in the SEM 1
    teaching timetable (the school's real attendance per class)."""
    sys.path.insert(0, str(ROOT))
    from src.extract import collect_records, parse_cell, resolve_cohort, venue_kind
    from src.extract import load_sections as ex_load_sections

    secs = ex_load_sections(BASE)
    caps = room_capacity_map()
    m = {}
    for rec in collect_records(INP / SEM1_TT):
        offers, _ = parse_cell(rec)
        if venue_kind(rec["room"]) in ("online", "field"):
            continue
        room = re.sub(r"\s*\(\d+\)", "", rec["room"]).strip()
        cap = caps.get(room, 80)
        for o in offers:
            if o["prefix"] == "RT":
                continue
            pool = secs.get((o["prog"], o["level"]))
            if pool is None:
                continue
            cohort = resolve_cohort(pool, o["section"])
            key = (o["code"], cohort)
            m.setdefault(key, []).append(cap)
    # size by the room most used (not the occasional biggest venue)
    return {k: max(set(c), key=c.count) for k, c in m.items()}


def main():
    sys.path.insert(0, str(ROOT))
    from src.extract import extract_timetable

    tmp = ROOT / "output" / "_sem1_tt"
    tmp.mkdir(parents=True, exist_ok=True)
    shutil.copy(BASE / "cohorts.xlsx", tmp / "cohorts.xlsx")
    result = extract_timetable(INP / SEM1_TT, tmp, tmp)
    tt = pd.read_excel(tmp / "courses.xlsx")
    sizes = real_sizes()

    rows = []
    for _, r in tt.iterrows():
        code = str(r["course_code"]).strip()
        cohort = str(r["cohort"]).strip().upper()
        rows.append({
            "course_code": code,
            "course_name": str(r.get("course_name") or "").strip(),
            "programme": str(r["programme"]).strip(),
            "level": int(r["level"]),
            "cohort": cohort,
            "lecturer": str(r.get("lecturer") or "").strip(),
            "lecture_hours": float(r.get("lecture_hours", 0) or 0),
            "practical_hours": float(r.get("practical_hours", 0) or 0),
            "credits": float(r.get("credits", 0) or 0),
            "online": "yes" if str(r["online"]).strip().lower() in ("yes", "y", "1", "true") else "no",
            "field_work": "yes" if str(r.get("field_work") or "no").strip().lower() in ("yes", "y", "1", "true") else "no",
            "hours_per_session": int(r["hours_per_session"]),
            "sessions_per_week": int(r["sessions_per_week"]),
            "min_room_size": str(r.get("min_room_size") or "").strip(),
            "sections": str(r.get("sections") or "").strip(),
            "size": sizes.get((code, cohort), 0) or "",
        })

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
    return result


if __name__ == "__main__":
    main()
