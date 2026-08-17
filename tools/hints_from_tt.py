"""Build a warm-start solution for the solver from the ORIGINAL timetable.

The school's real timetable is itself a valid packing of the same demand, so
its (day, time, room) placements are a perfect hint set for CP-SAT.  This maps
every solver session back to the source cell it came from and writes the result
in solve()'s solution-JSON format, then verifies it against the hard
constraints (section / lecturer / room / capacity conflicts).
"""
import sys
from collections import Counter, defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import src.extract as ex
from src.loaders import load_problem
from src.solver import Assignment, _verify
from src.slots import DAYS, SLOT_TIMES, SLOTS_PER_DAY

ROOT = Path(__file__).resolve().parent.parent
SOLVER_ROOMS = {"ONLINE", "FIELD WORK"}


def resolve_cells(fn, data_dir):
    """Replicate the extraction's per-cell resolution (incl. co-taught merge)."""
    records = ex.collect_records(fn)
    sections = ex.load_sections(data_dir)
    cells = []
    for rec in records:
        offers, _ = ex.parse_cell(rec)
        if not offers:
            continue
        vk = ex.venue_kind(rec["room"])
        no_room = vk in ("online", "field")
        is_vle = __import__("re").search(r"\(VLE\)", rec["text"], __import__("re").IGNORECASE) is not None
        field_work = vk == "field" and not is_vle
        if is_vle:
            no_room = True
        practical = vk in ("lab", "field") or any(o["p_marker"] for o in offers)

        resolved = []
        for o in offers:
            if o["prefix"] == "RT":
                continue
            secs = sections.get((o["prog"], o["level"]))
            if secs is None:
                continue
            cohort = ex.resolve_cohort(secs, o["section"])
            section_ids = {f"{o['prog']}{o['level']}-{sec}" for sec in secs if cohort == "AB" or sec == cohort}
            resolved.append((o, section_ids))

        if not resolved:
            continue
        if len(resolved) > 1:
            counts = Counter(o["lecturer"] for o, _ in resolved)
            offer = dict(resolved[0][0])
            offer["lecturer"] = counts.most_common(1)[0][0]
            section_ids = set().union(*(sid for _, sid in resolved))
        else:
            offer, section_ids = resolved[0]

        cells.append({
            "code": offer["code"], "prog": offer["prog"], "level": offer["level"],
            "section_ids": frozenset(section_ids), "span": rec["span"],
            "day": rec["day"], "time": rec["time"], "room": ex.re.sub(r"\s*\(\d+\)", "", rec["room"]).strip(),
            "online": no_room, "field_work": field_work,
            "practical": practical,
        })
    return cells


def time_to_slot(time_str):
    for i, st in enumerate(SLOT_TIMES):
        if st.startswith(time_str):
            return i
    return None


def build_hints(sem, sub, fn, data_dir):
    cells = resolve_cells(fn, data_dir)
    problem = load_problem(data_dir)
    sessions = problem["sessions"]
    room_names = {r.name for r in problem["rooms"]}

    # index cells by (code, prog, level, span, online, field_work, practical)
    by_key = defaultdict(list)
    for c in cells:
        by_key[(c["code"], c["prog"], c["level"], c["span"], c["online"], c["field_work"], c["practical"])].append(c)

    # sessions bucketed by (code, prog, level, duration, online, field_work,
    # practical, sections-set): cells and sessions sharing a bucket are matched
    # by (day,time) order, so duplicate rows of the same course each get their
    # own original cell.
    buckets = defaultdict(list)
    for s in sessions:
        buckets[(s.course.code, s.course.programme, s.course.level, s.duration,
                 s.course.online, s.course.field_work, s.course.practical_hours > 0,
                 frozenset(s.sections))].append(s)

    hinted = 0
    assignments = []
    room_names = {r.name for r in problem["rooms"]}
    for key, sess in buckets.items():
        code, prog, level, dur, online, fw, practical, secset = key
        row_cells = by_key.get((code, prog, level, dur, online, fw, practical), [])
        cands = [c for c in row_cells if c["section_ids"] == secset]
        if not cands:
            continue
        cands = sorted(cands, key=lambda c: (DAYS.index(c["day"].title()), time_to_slot(c["time"]) or 0))
        sess = sorted(sess, key=lambda s: (s.course.seq, s.index))
        for s, c in zip(sess, cands):
            t = time_to_slot(c["time"])
            if t is None:
                continue
            slot = DAYS.index(c["day"].title()) * SLOTS_PER_DAY + t
            if c["online"]:
                room = "ONLINE"
            elif c["field_work"]:
                room = "FIELD WORK"
            else:
                room = c["room"]
                if room not in room_names:
                    continue
            assignments.append(Assignment(s, slot, room))
            hinted += 1

    checks = _verify(assignments, problem)
    print(f"{sem}: sessions={len(sessions)} hinted={hinted} "
          f"section={checks['section']} lecturer={checks['lecturer']} "
          f"room={checks['room']} capacity={checks['capacity']}")

    # The school's real timetable contains its own section/lecturer conflicts.
    # A hinted session is pinned to its exact (slot, room) later (fix_hinted),
    # so any conflicted hint would force a conflict into the solution. Drop
    # every hinted session involved in a section OR lecturer conflict; the
    # solver will place those sessions itself.
    sec_seen = defaultdict(list)
    lec_seen = defaultdict(list)
    conflicted = set()
    for a in assignments:
        for u in range(a.slot, a.slot + a.session.duration):
            for sec in a.session.sections:
                if sec_seen[(sec, u)]:
                    conflicted.update(x.session.id for x in sec_seen[(sec, u)])
                    conflicted.add(a.session.id)
                sec_seen[(sec, u)].append(a)
            lec = a.session.course.lecturer
            if lec:
                if lec_seen[(lec, u)]:
                    conflicted.update(x.session.id for x in lec_seen[(lec, u)])
                    conflicted.add(a.session.id)
                lec_seen[(lec, u)].append(a)
    if conflicted:
        before = len(assignments)
        assignments = [a for a in assignments if a.session.id not in conflicted]
        print(f"{sem}: dropped {before - len(assignments)} hinted sessions involved in "
              f"section/lecturer conflicts ({len(conflicted)} sessions)")

    checks = _verify(assignments, problem)
    print(f"{sem}: clean hint set: hinted={len(assignments)} "
          f"section={checks['section']} lecturer={checks['lecturer']} "
          f"room={checks['room']} capacity={checks['capacity']}")

    out_path = ROOT / f"output/{sub}/initial_solution.json"
    data = {"status": "FEASIBLE", "objective": 0,
            "sessions": {a.session.id: [a.slot, a.room] for a in assignments}}
    import json
    with open(out_path, "w", encoding="utf-8") as fh:
        json.dump(data, fh)
    print(f"{sem}: wrote {out_path.name}")
    return checks


if __name__ == "__main__":
    jobs = [
        ("sem1", "_sem1_tt", r"data/input/DRAFT_FINAL TEACHING TIME TABLE_SRID (SEM 1_2025_2026).xlsx"),
        ("sem2", "_sem2_tt", r"data/input/FINAL TEACHING TIMETABLE_SRID (SEM 2_2025_2026) .xlsx"),
    ]
    for sem, sub, fn in jobs:
        build_hints(sem, sub, fn, ROOT / f"data/semesters/{sem}")
