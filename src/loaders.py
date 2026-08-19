import json
import math
import re
from pathlib import Path

import pandas as pd

from .models import Cohort, Course, Room, Session
from .solver import SolverWeights

ROOMS_FILE = "rooms.xlsx"
COHORTS_FILE = "cohorts.xlsx"
COURSES_FILE = "courses.xlsx"
LECTURERS_FILE = "lecturers.xlsx"
SETTINGS_FILE = "settings.json"

ONLINE_TRUE = {"yes", "y", "1", "true", "online"}

# A course taught to both sections A and B of the same programme+level is kept
# as ONE combined class while the combined size is at or below this many
# students; above it the loader splits it into two separate classes (one per
# section), each carrying the full teaching hours.
SPLIT_COMBINED_ABOVE = 90


def _truthy(value):
    return str(value).strip().lower() in ONLINE_TRUE


def _blank(value):
    return value is None or (isinstance(value, float) and math.isnan(value))


def _clean_text(value):
    if _blank(value):
        return ""
    return str(value).strip()


# Variants of the same lecturer's name (initial/spacing/case typos across the
# source files) mapped to the canonical spelling used everywhere downstream.
LECTURER_ALIASES = {
    "N.  Adzakor": "N. K. Adzakor",
    "N. Adzakor": "N. K. Adzakor",
    "N.A. Adzakor": "N. K. Adzakor",
    "Dr A. E. Nti": "Dr E. A. Nti",
    "A. E. Nti": "Dr E. A. Nti",
    "Dr A.Y. Anafo": "Dr A. Y. Anafo",
    "Dr Y. A. Anafo": "Dr A. Y. Anafo",
    "S. Afful": "S. L. Afful",
    "I.K. Prah": "I. K. Prah",
    "Dr D. A. Arhinful": "Dr D. Arhinful",
    "D. Arhinful": "Dr D. Arhinful",
    "D.Arhinful": "Dr D. Arhinful",
    "REV S. ODOOM": "Rev S. Odoom",
    "Rev. S. Odoom": "Rev S. Odoom",
    "Dr. S. Sebbeh-Newton": "Dr S. Sebbeh-Newton",
    "S. Sebbeh-Newton": "Dr S. Sebbeh-Newton",
    "E.Adu Kyeremeh": "E. Adu Kyeremeh",
    "E. A. Kyeremeh": "E. Adu Kyeremeh",
    "F. Mummuni": "F. Mumuni",
    "S. Yormesor": "S. K. Yormesor",
    "S K Yormesor": "S. K. Yormesor",
    "S.K. Yormesor": "S. K. Yormesor",
    "Dr. D. K. Tortor": "Dr D. K. Tortor",
    "Dr D. K Tortor": "Dr D. K. Tortor",
    "Dr E .M. Martey": "Dr E. M. Martey",
    "Dr E.M. Martey": "Dr E. M. Martey",
    "DR E. MARTEY": "Dr E. M. Martey",
    "E. M. Martey": "Dr E. M. Martey",
    "MARTEY": "Dr E. M. Martey",
    "DR MARTEY": "Dr E. M. Martey",
    "DR AMPONSAH": "Dr A. A. Amponsah",
    "DR ATTATSI": "Dr I. K. Attatsi",
    "Dr I. Attatsi": "Dr I. K. Attatsi",
    "DR KWANTWI": "Dr T. Kwantwi",
    "DR AMPOMAH": "Dr E. K. Ampomah",
    "E. ASANTE": "E. Asante",
    "OBU": "E. Obu",
    "E. OBU": "E. Obu",
    "Dr F. Tabase": "F. Tabase",
    "DR KWANSAH ANSAH": "Dr A. K. Kwansah Ansah",
    "Prof. W. Adjardjah": "Prof W. Adjardjah",
    "Dr C.A. Komolafe": "Dr C. A. Komolafe",
    "M. O. Alhassam": "M. O. Alhassan",
    "M. O Alhassan": "M. O. Alhassan",
    "R. Kuffour": "R. K. Kuffour",
    "Prof. S. Nunoo": "Prof S. Nunoo",
    "Prof  I. Yakubu": "Prof I. Yakubu",
    # ---- sem1 / sem2 dedup: same person, variant spelling ----
    "A, AMPONSAH": "Dr A. A. Amponsah",
    "A. AMPONSAH": "Dr A. A. Amponsah",
    "AMPONSAH": "Dr A. A. Amponsah",
    "ACQUAH": "Assoc Prof J. Acquah",
    "J. ACQUAH": "Assoc Prof J. Acquah",
    "ASSOC PROF ACQUAH": "Assoc Prof J. Acquah",
    "ADU KYEREMEH": "E. Adu Kyeremeh",
    "KYEREMEH": "E. Adu Kyeremeh",
    "AMOAH": "R. K. Amoah",
    "R. K. AMOAH": "R. K. Amoah",
    "ANNAN-BOAH": "E. Annan-Boah",
    "E. ANNAN BOAH": "E. Annan-Boah",
    "E. ANNAN-BOAH": "E. Annan-Boah",
    "E. BOAH": "E. Annan-Boah",
    "ASANTE": "E. Asante",
    "VLE ASANTE": "E. Asante",
    "ASARE": "E. N. Asare",
    "N. ASARE": "E. N. Asare",
    "E. N. ASARE": "E. N. Asare",
    "BENYARKU": "E. Benyarku",
    "E. BENYARKU": "E. Benyarku",
    "DANKWAH": "S. K. Dankwah",
    "DR D. ARHINFUL": "Dr D. Arhinful",
    "DR I. OFORI": "Dr I. Ofori",
    "OFORI": "Dr I. Ofori",
    "DR J. SEIDU": "Dr J. Seidu",
    "DR NKETSIAH": "Dr R. N. Nketsiah",
    "DR T. KWANTWI": "Dr T. Kwantwi",
    "KWANTWI": "Dr T. Kwantwi",
    "DR TABASE": "F. Tabase",
    "E. A. Nti": "Dr E. A. Nti",
    "ELLIS": "J. Ellis",
    "J. ELLIS": "J. Ellis",
    "EWUSI ESSOUN": "Ewusi-Essoun",
    "EWUSI-ESSOUN": "Ewusi-Essoun",
    "I. KWOFIE": "I. Kwofie",
    "KWOFIE": "I. Kwofie",
    "I. PRAH": "I. K. Prah",
    "I K. PRAH": "I. K. Prah",
    "I.K. PRAH": "I. K. Prah",
    "I. K. PRAH": "I. K. Prah",
    "PRAH": "I. K. Prah",
    "M. B POKU": "M. B. Poku",
    "M. B. POKU": "M. B. Poku",
    "POKU": "M. B. Poku",
    "MOHAMMED": "A. O. Mohammed",
    "O. MOHAMMED": "A. O. Mohammed",
    "A. O. MOHAMMED": "A. O. Mohammed",
    "O. BEMPAH": "Owusu-Bempah",
    "OWUSU BEMPAH": "Owusu-Bempah",
    "OWUSU-BEMPAH": "Owusu-Bempah",
    "SEBEH-NEWTON": "Dr S. Sebbeh-Newton",
    "SEBBEH-NEWTON": "Dr S. Sebbeh-Newton",
    "N. A. Adzakor": "N. K. Adzakor",
    # ---- collapse-time dedup: typo variants of the same person ----
    "O. MOHAMMD": "A. O. Mohammed",
    "D. AHINFUL": "Dr D. Arhinful",
    "YORMESOR": "S. K. Yormesor",
    "YORMESSOR": "S. K. Yormesor",
    "G. OWUSU": "Dr G. Owusu",
    "G.OWUSU": "Dr G. Owusu",
}

_SPACE_RE = re.compile(r"\s+")


def _canonical_lecturer(value):
    if _blank(value):
        return ""
    name = _SPACE_RE.sub(" ", str(value)).strip()
    return LECTURER_ALIASES.get(name, name)


def load_rooms(path):
    if not Path(path).exists():
        raise FileNotFoundError(f"Missing {path}")
    df = pd.read_excel(path)
    rooms = []
    for _, row in df.iterrows():
        rooms.append(
            Room(
                name=_clean_text(row["name"]),
                capacity=int(row["capacity"]),
                kind=_clean_text(row.get("kind", "lecture")) or "lecture",
            )
        )
    return rooms


def load_cohorts(path):
    if not Path(path).exists():
        raise FileNotFoundError(f"Missing {path}")
    df = pd.read_excel(path)
    cohorts = {}
    for _, row in df.iterrows():
        programme = _clean_text(row["programme"])
        if not programme:
            continue
        c = Cohort(
            programme=programme,
            level=int(row["level"]),
            section=_clean_text(row["section"]),
            size=int(row["size"]),
        )
        if c.id in cohorts:
            raise ValueError(f"Duplicate cohort {c.id}")
        cohorts[c.id] = c
    return cohorts


def load_lecturers(path):
    """Load global lecturers list. Returns list of dicts with 'name' key."""
    if not Path(path).exists():
        return []
    df = pd.read_excel(path)
    lecturers = []
    for _, row in df.iterrows():
        name = _clean_text(row.get("name", ""))
        if name:
            lecturers.append({"name": name})
    return lecturers


def save_lecturers(path, lecturers):
    """Save global lecturers list."""
    df = pd.DataFrame(lecturers, columns=["name"])
    df.to_excel(path, index=False)


def load_courses(path, cohorts, max_capacity=120, split_combined_above=SPLIT_COMBINED_ABOVE):
    if not Path(path).exists():
        raise FileNotFoundError(f"Missing {path}")
    df = pd.read_excel(path)
    raw_rows = [row.to_dict() for _, row in df.iterrows()]

    # Joint (cross-programme) classes are stored as independent per-programme
    # rows that share a "group_id" (see split_joint.py). Rows of the same group
    # are merged back into ONE combined offering so the class is taught as a
    # single combined session exactly as the original joint row was. Rows only
    # merge while they are genuinely the same course delivered by the same
    # lecturer (same hours, modality and duration): if a programme edits its
    # copy (different lecturer, split="yes", ...) the family separates into
    # independent classes. Merged rows keep the first row's programme, so
    # session ids / hints are unchanged.
    groups = {}
    for i, r in enumerate(raw_rows):
        g = _clean_text(r.get("group_id", ""))
        if g:
            groups.setdefault(g, []).append(i)

    def row_sig(r):
        return (
            _clean_text(r.get("course_code")),
            _clean_text(r.get("course_name")),
            str(r.get("level")),
            _clean_text(r.get("cohort")),
            _canonical_lecturer(r.get("lecturer")),
            str(r.get("lecture_hours") or 0),
            str(r.get("practical_hours") or 0),
            str(r.get("credits") or 0),
            _truthy(r.get("online")),
            _truthy(r.get("field_work")),
            str(r.get("hours_per_session") or ""),
            str(r.get("sessions_per_week") or ""),
            str(r.get("min_room_size") or ""),
        )

    logical = []
    emitted = set()
    for i, r in enumerate(raw_rows):
        g = _clean_text(r.get("group_id", ""))
        if g and g in groups:
            if g in emitted:
                continue
            emitted.add(g)
            rs = [raw_rows[j] for j in groups[g]]
            if len({row_sig(x) for x in rs}) == 1 and not any(
                _clean_text(x.get("split", "")).lower() == "yes" for x in rs
            ):
                merged = dict(rs[0])
                section_ids = []
                for x in rs:
                    s = _clean_text(x.get("sections", ""))
                    section_ids.extend(y.strip().upper() for y in s.split(",") if y.strip())
                merged["sections"] = ",".join(dict.fromkeys(section_ids))
                merged_size = next(
                    (x.get("group_size") for x in rs if not _blank(x.get("group_size"))), None)
                if merged_size is not None:
                    merged["size"] = int(merged_size)
                else:
                    sizes = [x.get("size") for x in rs]
                    if all(not _blank(s) for s in sizes):
                        merged["size"] = sum(int(s) for s in sizes)
                    else:
                        merged["size"] = ""
                merged["group_id"] = ""
                logical.append(merged)
            else:
                logical.extend(rs)
        else:
            logical.append(r)

    sessions = []
    seq = 0
    for row in logical:
        code = _clean_text(row["course_code"])
        name = _clean_text(row["course_name"])
        programme = _clean_text(row["programme"])
        level = int(row["level"])
        cohort = _clean_text(row["cohort"]).upper()
        lecturer = _canonical_lecturer(row["lecturer"])
        if not lecturer:
            # No name in the source data. Give the class a unique placeholder
            # so it is its own lecturer resource (two unnamed classes must not
            # collide on the shared "" name) and the exports stay readable.
            lecturer = f"TBA {code}"
        lecture_hours = float(row.get("lecture_hours", 0) or 0)
        practical_hours = float(row.get("practical_hours", 0) or 0)
        credits = float(row.get("credits", 0) or 0)
        online = _truthy(row.get("online", "no"))
        field_work = _truthy(row.get("field_work", "no"))
        duration = row.get("hours_per_session")
        duration = 2 if _blank(duration) else int(duration)
        if not 1 <= duration <= 12:
            raise ValueError(f"{code}: hours_per_session must be 1..12, got {duration}")
        spw = row.get("sessions_per_week")
        if _blank(spw):
            spw = int(math.ceil((lecture_hours + practical_hours) / duration))
        else:
            spw = int(spw)
        min_capacity = row.get("min_room_size")
        min_capacity = 0 if _blank(min_capacity) else int(min_capacity)

        if cohort not in ("A", "B", "AB", ""):
            raise ValueError(f"{code}: cohort must be A, B or AB, got {cohort!r}")

        sections_col = _clean_text(row.get("sections", ""))
        if sections_col:
            section_ids = [s.strip().upper() for s in sections_col.split(",") if s.strip()]
            for sid in section_ids:
                if sid not in cohorts:
                    raise ValueError(f"{code}: unknown section {sid!r} in sections column")
            sections = set(section_ids)
            size = sum(cohorts[sid].size for sid in section_ids)
        elif cohort:
            target = f"{programme}{level}-"
            needed = ["A", "B"] if cohort == "AB" else [cohort]
            for sec in needed:
                if target + sec not in cohorts:
                    raise ValueError(f"{code}: no cohort row for {target + sec}")
            sections = {target + sec for sec in needed}
            size = sum(cohorts[target + sec].size for sec in needed)
        else:
            # Auto-derive: a course serves every section of its programme+level
            # that exists in the cohorts table (e.g. CE 100 -> CE100-A, CE100-B).
            target = f"{programme}{level}-"
            section_ids = sorted(sid for sid in cohorts if sid.startswith(target))
            if not section_ids:
                raise ValueError(f"{code}: no cohort rows for {programme} level {level}")
            sections = set(section_ids)
            size = sum(cohorts[sid].size for sid in section_ids)
        size_override = row.get("size")
        if not _blank(size_override):
            size = int(size_override)
        if size > max_capacity and not online and not field_work:
            print(f"WARNING: {code} combined size {size} exceeds the biggest room ({max_capacity} seats); capping at {max_capacity}")
            size = max_capacity

        if spw < 1:
            raise ValueError(f"{code}: sessions_per_week must be >= 1")

        # Auto-split: a physical course taught to both A and B of the same
        # programme+level is split into two separate classes (one per section)
        # when the combined class is too large to teach as one. Each split class
        # keeps the full teaching hours. Cross-programme combinations (e.g.
        # "CE100-B,TM100-A") and online / field-work courses are never split.
        # The optional "split" column overrides the size-based default:
        #   blank  -> auto (split only when combined size > SPLIT_COMBINED_ABOVE)
        #   "no"   -> always keep combined (e.g. an auditorium lecture)
        #   "yes"  -> always split into one class per section
        split_flag = _clean_text(row.get("split", ""))
        auto_split = split_flag not in ("no", "yes")
        force_split = split_flag == "yes"
        target = f"{programme}{level}-"
        groups = [(cohort, sections, size)]
        if (
            not online
            and not field_work
            and set(sections) == {target + "A", target + "B"}
            and (force_split or (auto_split and size > split_combined_above))
        ):
            groups = [
                ("A", {target + "A"}, cohorts[target + "A"].size),
                ("B", {target + "B"}, cohorts[target + "B"].size),
            ]

        for g_cohort, g_sections, g_size in groups:
            seq += 1
            course = Course(
                code=code,
                name=name,
                programme=programme,
                level=level,
                cohort=g_cohort,
                lecturer=lecturer,
                lecture_hours=lecture_hours,
                practical_hours=practical_hours,
                credits=credits,
                online=online,
                field_work=field_work,
                sessions_per_week=spw,
                min_capacity=min_capacity,
                seq=seq,
            )

            for i in range(1, spw + 1):
                sessions.append(Session(
                    course=course, index=i, size=g_size,
                    sections=g_sections,
                    duration=duration, online=online, field_work=field_work,
                ))

    return sessions


def load_settings(data_dir):
    path = Path(data_dir) / SETTINGS_FILE
    weights = SolverWeights()
    if path.exists():
        with open(path, encoding="utf-8") as fh:
            raw = json.load(fh)
        for key in ("room_oversize", "evening", "cohort_gap", "lecturer_gap", "early_utilization", "lecturer_overlap", "section_overlap", "lab_room", "online"):
            if key in raw.get("weights", {}):
                setattr(weights, key, int(raw["weights"][key]))
    return weights


# Optional per-semester knobs (settings.json). Every key has a safe default so
# a settings file is never required: the system derives room-based numbers from
# rooms.xlsx and only uses these to override them.
SETTINGS_OVERRIDES = {
    "split_combined_above": 90,   # auto-split an AB class into per-section classes above this size
    "sr4_room": "",               # preferred small-class room name ("" = smallest real room)
    "small_class_cap": 0,         # classes at or below this many students are "small" (0 = smallest room capacity)
    "max_class_cap": 0,           # largest allowed combined/co-taught class (0 = biggest room capacity)
    "phase1_time_limit": 240,     # seconds for the phase-1 feasibility solve
    "regen_pack_budget": 0,       # seconds for the packing step (0 = pipeline defaults)
}


def load_extra_settings(data_dir):
    path = Path(data_dir) / SETTINGS_FILE
    out = dict(SETTINGS_OVERRIDES)
    if path.exists():
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except ValueError:
            raw = {}
        if not isinstance(raw, dict):
            raw = {}
        for key in out:
            if key in raw and raw[key] is not None and raw[key] != "":
                out[key] = raw[key]
    return out


def rooms_config(rooms, overrides):
    """Room-derived numbers used by the postprocess passes. Everything is
    computed from rooms.xlsx so adding or removing classrooms keeps working:
    the 'small room' is the smallest real room, the small-class cap is that
    room's capacity, and the co-teach cap is the biggest room's capacity."""
    real = [r for r in rooms if r.name not in ("ONLINE", "FIELD WORK")]
    max_cap = max((r.capacity for r in real), default=0)
    small = min(real, key=lambda r: r.capacity) if real else None
    small_cap = small.capacity if small else 0
    sr4 = overrides.get("sr4_room") or ""
    if not sr4 or not any(r.name == sr4 for r in real):
        sr4 = small.name if small else ""
    return {
        "max_capacity": max_cap,
        "small_capacity": small_cap,
        "small_room": sr4,
        "max_class": int(overrides.get("max_class_cap") or max_cap),
    }


def list_semesters(base=None):
    """Discover semester data folders (data/semesters/<name>/courses.xlsx) so a
    new semester works without touching any code."""
    base = Path(base) if base else Path(__file__).resolve().parent.parent / "data" / "semesters"
    out = []
    if base.is_dir():
        for p in sorted(base.glob("*")):
            if p.is_dir() and (p / COURSES_FILE).exists():
                out.append(p.name)
    return out


def load_problem(data_dir):
    data_dir = Path(data_dir)
    rooms = load_rooms(data_dir / ROOMS_FILE)
    cohorts = load_cohorts(data_dir / COHORTS_FILE)
    overrides = load_extra_settings(data_dir)
    rc = rooms_config(rooms, overrides)
    sessions = load_courses(
        data_dir / COURSES_FILE, cohorts,
        max_capacity=rc["max_capacity"],
        split_combined_above=overrides["split_combined_above"],
    )
    weights = load_settings(data_dir)

    sections = sorted(cohorts.keys())
    lecturers = sorted({s.course.lecturer for s in sessions if s.course.lecturer})

    return {
        "rooms": rooms,
        "cohorts": cohorts,
        "sections": sections,
        "sessions": sessions,
        "lecturers": lecturers,
        "weights": weights,
        "overrides": overrides,
        "rooms_config": rc,
    }
