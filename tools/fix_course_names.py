"""Data hygiene for the semester datasets.

Reads course names from the six department course-distribution files and
re-applies the real per-programme titles to every course row in
data/semesters/sem2/courses.xlsx (each row shows its own code + real title;
no merged "/" labels).  Rows with no dept title keep their code as the name.

Also makes every cross-programme class genuinely independent by dropping the
"group_id"/"group_size" columns (which made the loader re-combine the rows
into a single co-taught class), and removes the fabricated sem1 courses
DS 169 and MF 141 from data/semesters/sem1/courses.xlsx.

Run:  .venv\\Scripts\\python.exe tools\\fix_course_names.py
"""

import re
import shutil
from collections import Counter
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
INP = ROOT / "data" / "input"
SEM2 = ROOT / "data" / "semesters" / "sem2" / "courses.xlsx"
SEM1 = ROOT / "data" / "semesters" / "sem1" / "courses.xlsx"

DEPT_FILES = [
    "2nd Sem. 2025-26 Draft 2-Electrical and Electronic Course Distribution (1).xlsx",
    "2nd sem.2025-26 Draft 3-Mechanical Engineering Course Distribution-Sem2-2026.xlsx",
    "Final Course Distribution - GMCV - 2nd Semester 2026  -19 May 2026 (2).xlsx",
    "Final Course_Distribution_Semester_2_GLES_2025_2026 (4).xlsx",
    "Final-Computer Science and Engineering Course Distribution-Sem2-2026 (3).xlsx",
    "Final-Mathematics Course Distribution-Sem2-2026 (2).xlsx",
]

DROP_COLS = ("group_id", "group_size")


def _parse_code(raw):
    m = re.match(r"([A-Z]{1,3})\s*(\d{2,3})", re.sub(r"[#*]", "", str(raw).strip()))
    if not m:
        return None, None
    return m.group(1).upper(), f"{m.group(1)} {m.group(2)}"


def dept_title_map():
    """(programme, course_code) -> most-common real title across dept files."""
    titles = {}
    for fn in DEPT_FILES:
        xl = pd.ExcelFile(INP / fn)
        for sn in xl.sheet_names:
            df = xl.parse(sn, header=None)
            for i in range(len(df)):
                a = [str(v).strip() if v is not None else "" for v in df.iloc[i].tolist()[:12]]
                if len(a) < 3 or not re.match(r"^[A-Z]{1,3}\s*\d", a[1]):
                    continue
                pref, code = _parse_code(a[1])
                if pref is None or not a[2]:
                    continue
                titles.setdefault((pref, code), Counter())[a[2]] += 1
    return {k: _normalize_title(c.most_common(1)[0][0]) for k, c in titles.items()}


def _backup(path):
    bak = path.with_suffix(".pre-fix.xlsx")
    if not bak.exists():
        shutil.copy2(path, bak)
        print(f"backup -> {bak.name}")


def _normalize_title(title):
    return re.sub(r"\bCoporate\b", "Corporate", str(title))


def fix_sem2(titles):
    df = pd.read_excel(SEM2)
    before = len(df)
    df["programme"] = df["programme"].astype(str).str.strip().str.upper()
    df["course_code"] = df["course_code"].astype(str).str.strip()
    for c in DROP_COLS:
        if c in df.columns:
            df = df.drop(columns=[c])

    def name_of(row):
        pref, _ = _parse_code(row["course_code"])
        if pref is None:
            return row["course_name"]
        return titles.get((pref, row["course_code"]), row["course_code"])

    df["course_name"] = df.apply(name_of, axis=1)
    merged_left = df["course_name"].astype(str).str.contains("/", na=False).sum()
    _backup(SEM2)
    df.to_excel(SEM2, index=False)
    print(f"sem2: {before} rows -> {len(df)} rows; '/' labels remaining: {merged_left}")


def fix_sem1():
    df = pd.read_excel(SEM1)
    before = len(df)
    df["course_code"] = df["course_code"].astype(str).str.strip()
    mask = df["course_code"].str.match(r"^(DS\s*169|MF\s*141)$")
    removed = df[mask]
    if len(removed):
        print("sem1 fabricated rows removed:")
        for _, r in removed.iterrows():
            print("   ", " | ".join(str(r[c]) for c in ("course_code", "course_name", "programme", "lecturer")))
        df = df[~mask]
    df["course_name"] = df.apply(
        lambda r: _normalize_title(r["course_name"] if "/" not in str(r["course_name"]) else r["course_code"]), axis=1)
    _backup(SEM1)
    df.to_excel(SEM1, index=False)
    print(f"sem1: {before} rows -> {len(df)} rows")


if __name__ == "__main__":
    fix_sem2(dept_title_map())
    fix_sem1()