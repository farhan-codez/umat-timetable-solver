"""Split joint (cross-programme) course rows into independent per-programme rows.

Each original row whose sections span more than one programme (e.g. a class
taught to CE100-B and TM100-A together) is replaced by one row per programme.
All the children of a source row share the same "group_id", so the loader
(src/loaders.py load_courses) re-combines them into the single combined class
that was taught originally — unless a child is edited (different lecturer,
modality or hours), in which case it becomes its own independent class.

The transform is lossless: a merged group reproduces the source row exactly
(same sections union, same combined size, same session ids / hints).
"""

import re
from pathlib import Path

import pandas as pd

SECTION_RE = re.compile(r"^([A-Z]+)(\d+)-([A-Z]+)$")


def _programme_of(section_id):
    m = SECTION_RE.match(section_id)
    return m.group(1) if m else None


def _num(v):
    if v is None or (isinstance(v, float) and v != v):
        return None
    try:
        return int(float(v))
    except (TypeError, ValueError):
        return None


def _size_for(section_ids, cohorts):
    return sum(int(cohorts[sid].get("size") or 0) for sid in section_ids)


def split_file(path, backup=True):
    path = Path(path)
    df = pd.read_excel(path)
    cohorts = {}
    cohorts_path = path.with_name("cohorts.xlsx")
    if cohorts_path.exists():
        cdf = pd.read_excel(cohorts_path)
        for _, r in cdf.iterrows():
            key = f"{str(r['programme']).strip()}{int(r['level'])}-{str(r['section']).strip()}"
            cohorts[key] = {"size": r.get("size")}

    out = []
    for i, row in enumerate(df.to_dict("records")):
        sections = [s.strip().upper() for s in str(row.get("sections") or "").split(",") if s.strip()]
        progs = [p for p in {_programme_of(s) for s in sections} if p]
        if len(progs) < 2:
            out.append(row)
            continue
        parent_prog = str(row.get("programme") or "").strip().upper()
        grouped = {}
        for s in sections:
            grouped.setdefault(_programme_of(s), []).append(s)
        ordered = sorted(grouped.keys(), key=lambda p: (p != parent_prog, p))
        gid = f"joint-{i + 1}"
        explicit = row.get("size")
        explicit = "" if explicit is None or (isinstance(explicit, float) and explicit != explicit) else int(explicit)
        for prog in ordered:
            child = dict(row)
            child["programme"] = prog
            child["sections"] = ",".join(sorted(grouped[prog]))
            child["size"] = _size_for(grouped[prog], cohorts)
            child["group_size"] = explicit
            child["split"] = "no"
            child["group_id"] = gid
            out.append(child)

    columns = list(df.columns)
    for c in ("group_id", "group_size"):
        if c not in columns:
            columns.append(c)
    out_df = pd.DataFrame(out, columns=columns)
    if backup and not path.with_suffix(".presplit.xlsx").exists():
        path.with_suffix(".presplit.xlsx").write_bytes(path.read_bytes())
    out_df.to_excel(path, index=False)
    print(f"{path.name}: {len(df)} rows -> {len(out_df)} rows "
          f"({sum(len([x for x in df['sections'].astype(str) if p in x]) for p in [])})")


if __name__ == "__main__":
    for sem in ("sem1", "sem2"):
        split_file(f"data/semesters/{sem}/courses.xlsx")
