import sys, json, os, subprocess, time, random, tempfile
from copy import copy
from pathlib import Path
from types import SimpleNamespace
from src.paths import PROJECT_ROOT, DATA_DIR, OUTPUT_DIR
from src.loaders import load_problem, list_semesters
from src.solver import load_solution_json, repair_assignments, _verify, Assignment, ONLINE_ROOM, FIELD_WORK_ROOM, _allowed_starts, _allowed_rooms, _is_split_form, _is_combined_form, unit_base, fix_same_day_course
from src.slots import DAYS, SLOTS_PER_DAY, N_SLOTS, day_index_of, slot_in_day
from src.compact import compact, fill_online_rooms
from src.pack import pack, Packer
from src.solver import _verify
from src.export import export_all
from src.pack import pack, Packer

import logging
log = logging.getLogger("umat.regen")

PY = sys.executable
WORKER = str(Path(__file__).resolve().parent / "regen_worker.py")
WORK = Path(tempfile.gettempdir()) / "umat-tt"
os.makedirs(WORK, exist_ok=True)


def run_phase(sem, phase, time_limit, in_path, out_path, seed, mode="plain", grace=180):
    if os.path.exists(out_path):
        os.remove(out_path)
    cmd = [PY, "-u", WORKER, sem, phase, str(time_limit), in_path or "none", out_path, str(seed), str(mode)]
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, encoding="utf-8", errors="replace")
    deadline = time.time() + time_limit + grace
    line = None
    while time.time() < deadline:
        try:
            rc = proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            continue
        try:
            out, _ = proc.communicate(timeout=5)
        except subprocess.TimeoutExpired:
            out = ""
        for ln in (out or "").splitlines():
            print(f"  [{phase}] {ln}", flush=True)
        return rc, None
    proc.kill()
    try:
        out, _ = proc.communicate(timeout=10)
    except subprocess.TimeoutExpired:
        out = ""
    print(f"  [{phase}] KILLED after {time_limit + grace:.0f}s", flush=True)
    for ln in (out or "").splitlines():
        print(f"  [{phase}] {ln}", flush=True)
    return -1, True


def best_solution(path, sessions):
    status, objective, assignments = load_solution_json(path, sessions)
    return status, objective, assignments


def gap_slots(assignments, problem):
    from src.slots import SLOTS_PER_DAY, day_index_of, slot_in_day
    total = 0
    for sec in problem["sections"]:
        for d in range(5):
            occ = set()
            for a in assignments:
                if sec in a.session.sections and a.room not in ("ONLINE", "FIELD WORK") and day_index_of(a.slot) == d:
                    for k in range(a.session.duration):
                        occ.add(slot_in_day(a.slot) + k)
            occ = sorted(occ)
            if len(occ) < 2:
                continue
            first, last = occ[0], occ[-1]
            total += sum(1 for s in range(first + 1, last) if s not in occ)
    return total


def room_holes(assignments, problem):
    from src.slots import SLOTS_PER_DAY, day_index_of, slot_in_day
    total = 0
    for room in problem["rooms"]:
        for d in range(5):
            occ = set()
            for a in assignments:
                if a.room == room.name and day_index_of(a.slot) == d:
                    for k in range(a.session.duration):
                        occ.add(slot_in_day(a.slot) + k)
            occ = sorted(occ)
            if len(occ) < 2:
                continue
            first, last = occ[0], occ[-1]
            total += sum(1 for s in range(first + 1, last) if s not in occ)
    return total


def clone_assign(assignments):
    return [Assignment(a.session, a.slot, a.room) for a in assignments]


# Phase2 (global soft-optimization over the full ~324k-var model) is intractable:
# CP-SAT cannot even find a first solution within 300s (presolve alone exceeds it).
# Postprocess (repair/compact/pack/rebalance) already does local improvement, so
# phase2 is bypassed; phase1 + hints + postprocess is the production pipeline.
RUN_PHASE2 = False


def run(sem):
    t0 = time.time()
    log.info("regen start semester=%s", sem)
    problem = load_problem(str(DATA_DIR / "semesters" / sem))
    # Hard section/lecturer constraints: the pinned hints are conflict-free, so
    # phase1 (feasibility-only) already returns a conflict-free schedule and the
    # post-process only has to preserve it.
    problem["soft_lecturer"] = False
    problem["soft_sections"] = False
    problem["skip_gaps"] = True
    sessions = problem["sessions"]

    from src.feasibility import demand_report, print_demand_report
    report = demand_report(problem)
    print_demand_report(sem, report, detail=True)
    if report["shortfall_hours"] > 0:
        print(
            f"[{sem}] NOTE: naive demand is {report['shortfall_hours']}h/week over capacity; "
            "a small overrun may still pack, but if phase1 below finds nothing, "
            "this is why (add rooms, or co-teach/merge classes).",
            flush=True,
        )

    out1 = WORK / f"solve_{sem}_p1.json"
    hints_path = str(OUTPUT_DIR / f"_{sem}_tt" / "initial_solution.json")
    phase1_limit = int(problem["overrides"].get("phase1_time_limit") or 240)

    phase1_done = False
    for mode in ("tight", "plain"):
        label = "tight (no halls for small/mid classes)" if mode == "tight" else "plain"
        print(f"[{sem}] phase1 mode={label} (hints) ...", flush=True)
        for seed in (42, 7):
            print(f"[{sem}] phase1 seed={seed} ...", flush=True)
            rc, _ = run_phase(sem, "1", phase1_limit, hints_path, out1, seed, mode=mode)
            status, obj, assign1 = best_solution(out1, sessions)
            if assign1:
                phase1_done = True
                phase1_mode = mode
                print(f"[{sem}] phase1 mode={mode} seed={seed} recovered status={status} sessions={len(assign1)}", flush=True)
                break
            print(f"[{sem}] phase1 mode={mode} seed={seed} found nothing", flush=True)
        if phase1_done:
            break
        print(f"[{sem}] phase1 mode={mode} unsolvable - falling back to plain feasibility", flush=True)
    if not phase1_done:
        print(f"[{sem}] ABORT: no phase1 solution found", flush=True)
        print(
            f"[{sem}] Check the room-hour balance above, then run: "
            "python tools/healthcheck.py --probe",
            flush=True,
        )
        return

    if RUN_PHASE2:
        print(f"[{sem}] phase2 ...", flush=True)
        best = None
        best_gaps = None
        for seed in (42, 7):
            out2 = os.path.join(WORK, f"solve_{sem}_p2_seed{seed}.json")
            rc, _ = run_phase(sem, "2", 300, out1, out2, seed)
            status, obj, assign2 = best_solution(out2, sessions)
            if not assign2:
                print(f"[{sem}] phase2 seed={seed} found nothing", flush=True)
                continue
            gaps = gap_slots(assign2, problem)
            print(f"[{sem}] phase2 seed={seed} status={status} obj={round(obj)} gaps={gaps}", flush=True)
            if best is None or gaps < best_gaps:
                best, best_gaps, best_status, best_obj = assign2, gaps, status, obj
        if best is None:
            result_assign = assign1
            best_status, best_obj = None, None
            print(f"[{sem}] phase2 found nothing - using phase1", flush=True)
        else:
            result_assign, status, obj = best, best_status, best_obj
            print(f"[{sem}] keeping best phase2 (gaps={best_gaps})", flush=True)
    else:
        result_assign, best_status, best_obj = assign1, None, None
        print(f"[{sem}] phase2 skipped - using phase1 + postprocess", flush=True)

    result_assign = list(result_assign)
    result_assign = postprocess(problem, result_assign, sem)
    checks = _verify(result_assign, problem)
    print(f"[{sem}] final verify: " + "; ".join(f"{k}={v}" for k, v in checks.items()), flush=True)

    used_cells, total_cells = room_cell_usage(result_assign, problem["rooms"])
    util_pct = round(used_cells / total_cells * 100, 1)

    out_dir = OUTPUT_DIR / sem
    summary = {
        "status": "FEASIBLE" if status in (None, "NO SOLUTION") else status,
        "objective": round(obj) if obj not in (None, float("inf")) else None,
        "conflicts": {k: checks[k] for k in ("section", "room", "capacity")},
        "lecturer_overlaps": checks.get("lecturer", 0),
        "same_day_course": checks.get("same_day_course", 0),
        "paired_session": checks.get("paired_session", 0),
        "sessions": len(sessions),
        "sections": len(problem["sections"]),
        "lecturers": len(problem["lecturers"]),
        "rooms": len(problem["rooms"]),
        "room_idle_holes": room_holes(result_assign, problem),
        "room_utilisation_pct": util_pct,
        "room_cells_used": used_cells,
        "room_cells_total": total_cells,
        "built_from": f"Semester {sem[-1]}",
    }
    export_all(problem, SimpleNamespace(assignments=result_assign), out_dir,
               semester_label=f"{summary['built_from']} TIME TABLE")
    with open(f"{out_dir}/solve_result.json", "w", encoding="utf-8") as fh:
        json.dump(summary, fh, indent=2)
    print(f"[{sem}] {summary}", flush=True)
    elapsed = time.time() - t0
    print(f"[{sem}] done in {elapsed:.0f}s", flush=True)
    log.info("regen done semester=%s elapsed=%ss status=%s", sem, round(elapsed), summary.get("status"))


def ensure_conflict_free(problem, assignments, max_rounds=8, seed=7):
    """Guarantee the shipped timetable is hard-conflict-free. Section and
    lecturer overlaps are treated as hard here even if the solve ran in soft
    mode, and the greedy repair is retried with shuffled session orders so it
    converges as far as possible. Returns (assignments, remaining_conflicts)."""
    problem["soft_sections"] = False
    problem["soft_lecturer"] = False
    rng = random.Random(seed)
    remaining = _verify(assignments, problem)
    for _ in range(max_rounds):
        if not any(remaining[k] for k in ("section", "lecturer", "room")):
            break
        rng.shuffle(assignments)
        repair_assignments(problem, assignments)
        remaining = _verify(assignments, problem)
    return assignments, remaining


def _code_num(code):
    import re
    m = re.search(r"(\d+)", str(code))
    return m.group(1) if m else None


def _slot_free_for_sections(assignments, host, sections, merged_away):
    for a in assignments:
        if a.session.id == host.session.id or a.session.id in merged_away:
            continue
        if not (set(a.session.sections) & sections):
            continue
        hs = range(host.slot, host.slot + host.session.duration)
        os = range(a.slot, a.slot + a.session.duration)
        if set(hs) & set(os):
            return False
    return True


def co_teach_merge(problem, assignments, sr4=None, max_class=None):
    """Fallback used only when the small-class room is full: a small physical
    class that could not get it joins the same-course class of another
    programme (same lecturer, or both lecturers unknown) in one bigger room -
    mirroring the school's own combined classes (e.g. 'CE/CV 251'). The host
    session absorbs the small class's sections; the absorbed assignment is
    dropped. The small-class cap and the merged-class cap come from
    rooms.xlsx (or settings.json overrides). Returns the number of merges.
    Assumes the input is conflict-free (production input)."""
    rc = problem.get("rooms_config") or {}
    if sr4 is None:
        sr4 = rc.get("small_room") or "SR 4"
    if max_class is None:
        max_class = rc.get("max_class") or 120
    small_cap = rc.get("small_capacity") or 40
    room_cap = {r.name: r.capacity for r in problem["rooms"]}
    small = [a for a in assignments
             if not a.session.online and not a.session.field_work
             and a.session.size <= small_cap and a.room != sr4]
    merged_away = set()
    hosts = set()
    merges = 0
    for a in small:
        s = a.session
        if s.id in merged_away or s.id in hosts:
            continue
        code_num = _code_num(s.course.code)
        if code_num is None:
            continue
        lec = s.course.lecturer
        tba = lec.startswith("TBA ")
        best = None
        for b in assignments:
            if b.session.id == s.id or b.session.id in merged_away:
                continue
            if b.session.online or b.session.field_work:
                continue
            if _code_num(b.session.course.code) != code_num:
                continue
            if b.session.course.programme == s.course.programme:
                continue
            blec = b.session.course.lecturer
            if not (lec == blec or (tba and blec.startswith("TBA "))):
                continue
            combined = s.size + b.session.size
            if combined > max_class:
                continue
            cap = room_cap.get(b.room)
            if cap is None or cap < combined:
                continue
            if b.session.id in hosts and not s.sections.isdisjoint(b.session.sections):
                continue
            if not _slot_free_for_sections(assignments, b, set(s.sections), merged_away):
                continue
            score = (1 if b.room == sr4 else 0, cap)
            if best is None or score < best[0]:
                best = (score, b, combined)
        if best is None:
            continue
        _, b, combined = best
        merged_session = copy(b.session)
        merged_session.sections = set(b.session.sections) | set(s.sections)
        merged_session.size = combined
        b.session = merged_session
        hosts.add(merged_session.id)
        merged_away.add(s.id)
        assignments.remove(a)
        merges += 1
    return merges


def prefer_sr4(problem, assignments, sr4=None, small_cap=None, max_gap_cost=1.0):
    """Move small physical classes into the smallest classroom (e.g. 'SR 4')
    whenever a free slot is available and the move does not badly worsen the
    packing (gap-cost below max_gap_cost). The target room and the small-class
    size cap are derived from rooms.xlsx (or overridable via settings.json),
    so adding classrooms keeps this working. Runs after pack/rebalance so it
    has the final say on which room small classes use. Mutates Assignment
    objects in place; returns the number of classes relocated."""
    from src.slots import N_SLOTS

    rc = problem.get("rooms_config") or {}
    if sr4 is None:
        sr4 = rc.get("small_room") or "SR 4"
    if small_cap is None:
        small_cap = rc.get("small_capacity") or 40
    if not any(r.name == sr4 for r in problem["rooms"]):
        return 0
    p = Packer(problem, assignments)
    p.room_occ.setdefault(sr4, [None] * N_SLOTS)
    moved = 0
    for a in list(p.assign.values()):
        s = a.session
        if s.online or s.field_work:
            continue
        if s.size > small_cap or a.room == sr4:
            continue
        if sr4 not in p.allowed[s.id]:
            continue
        best_t, best_d = None, max_gap_cost
        for t in p.starts[s.id]:
            if not p._free(s, t, sr4):
                continue
            d = p._score_relocate(s, t, sr4)
            if d < best_d:
                best_d, best_t = d, t
        if best_t is not None:
            p._remove(s)
            p._place(s, best_t, sr4)
            moved += 1
    return moved


import random

from src.slots import N_SLOTS, SLOTS_PER_DAY, slot_in_day

def _fix_break_crossing(problem, assignments):
    """Ensure no session crosses the 12:30-13:00 lunch break (slot 5->6 boundary).
    If a session crosses, move it to the earliest valid slot on the same day."""
    from src.solver import _allowed_starts, _allowed_rooms, _is_no_room
    from src.compact import _delta
    
    fixed = 0
    for a in assignments:
        s = a.session
        dur = s.duration
        day = a.slot // SLOTS_PER_DAY
        slot_in_day_start = a.slot % SLOTS_PER_DAY
        
        # Check if session crosses the break (slot 5 is 11:30-12:30, slot 6 is 13:00-14:00)
        if slot_in_day_start <= 5 and slot_in_day_start + dur > 6:
            # Find a valid slot on the same day
            allowed_starts = _allowed_starts(s)
            day_starts = [t for t in allowed_starts if t // SLOTS_PER_DAY == day]
            if not day_starts:
                continue
            
            # Try to find a valid slot that doesn't cross break
            sec_occ = {}
            lec_occ = {}
            room_occ = {}
            for aa in assignments:
                if aa.session.id == s.id:
                    continue
                ss = aa.session
                slots = range(aa.slot, aa.slot + ss.duration)
                for sec in ss.sections:
                    arr = sec_occ.setdefault(sec, [None] * N_SLOTS)
                    for u in slots:
                        arr[u] = ss.id
                arr = lec_occ.setdefault(ss.course.lecturer, [None] * N_SLOTS)
                for u in slots:
                    arr[u] = ss.id
                if not _is_no_room(aa.room):
                    arr = room_occ.setdefault(aa.room, [None] * N_SLOTS)
                    for u in slots:
                        arr[u] = ss.id
            
            best_t = None
            for t in day_starts:
                new_slots = range(t, t + dur)
                ok = True
                for sec in s.sections:
                    arr = sec_occ.get(sec, [])
                    for u in new_slots:
                        if u < len(arr) and arr[u] is not None:
                            ok = False
                            break
                    if not ok:
                        break
                if not ok:
                    continue
                for u in new_slots:
                    arr = lec_occ.get(s.course.lecturer, [])
                    if u < len(arr) and arr[u] is not None:
                        ok = False
                        break
                if not ok:
                    continue
                if not _is_no_room(a.room):
                    arr = room_occ.get(a.room, [])
                    for u in new_slots:
                        if u < len(arr) and arr[u] is not None:
                            ok = False
                            break
                    if not ok:
                        continue
                best_t = t
                break
            
            if best_t is not None:
                a.slot = best_t
                fixed += 1
    return fixed


def _room_candidates(session, rooms):
    """Real rooms a session could physically attend, best-fit first. Mirrors
    _allowed_rooms but never returns ONLINE / FIELD WORK."""
    need = max(session.size, session.course.min_capacity)
    wanted = "lab" if session.course.practical_hours > 0 else "lecture"
    candidates = [r for r in rooms if r.kind == wanted and r.capacity >= need]
    if not candidates:
        candidates = [r for r in rooms if r.capacity >= need]
    elif wanted == "lab":
        candidates = candidates + [r for r in rooms if r.kind != "lab" and r.capacity >= need]
    candidates = [r for r in candidates if r.name not in (ONLINE_ROOM, FIELD_WORK_ROOM)]
    return sorted(candidates, key=lambda r: (r.capacity, r.name))


def _build_occupancy(assignments, n_slots):
    room_occ = {}
    sec_occ = {}
    lec_occ = {}
    for a in assignments:
        if a.room in (ONLINE_ROOM, FIELD_WORK_ROOM):
            continue
        for u in range(a.slot, a.slot + a.session.duration):
            room_occ.setdefault(a.room, [None] * n_slots)[u] = a.session.id
            for sec in a.session.sections:
                sec_occ.setdefault(sec, [None] * n_slots)[u] = a.session.id
            if a.session.course.lecturer:
                lec_occ.setdefault(a.session.course.lecturer, [None] * n_slots)[u] = a.session.id
    return room_occ, sec_occ, lec_occ


def _place_online_in_room(problem, assignments, a, room_occ, sec_occ, lec_occ):
    """Try to seat one ONLINE assignment into a real room. Occupancy-aware
    (room / section / lecturer free), honours _allowed_starts (Saturday is only
    for RT online) and TIER_STRICT (no 120-seat halls for small/mid classes
    unless the room fits). Mutates occupancy arrays on success. Returns bool."""
    from src.solver import _tier, TIER_STRICT
    s = a.session
    dur = s.duration
    if TIER_STRICT and _tier(max(s.size, s.course.min_capacity)) <= 2:
        candidates = [r for r in _room_candidates(s, problem["rooms"]) if _tier(r.capacity) <= 2]
    else:
        candidates = _room_candidates(s, problem["rooms"])
    for r in candidates:
        rn = r.name
        for t in _allowed_starts(s):
            cover = range(t, t + dur)
            if any(room_occ.get(rn, [None] * N_SLOTS)[u] is not None for u in cover):
                continue
            if any(sec_occ.get(sec) and sec_occ[sec][u] is not None
                   for sec in s.sections for u in cover):
                continue
            if s.course.lecturer:
                lec = lec_occ.get(s.course.lecturer)
                if lec is not None and any(lec[u] is not None for u in cover):
                    continue
            a.room = rn
            a.slot = t
            for u in cover:
                room_occ.setdefault(rn, [None] * N_SLOTS)[u] = s.id
                for sec in s.sections:
                    sec_occ.setdefault(sec, [None] * N_SLOTS)[u] = s.id
                if s.course.lecturer:
                    lec_occ.setdefault(s.course.lecturer, [None] * N_SLOTS)[u] = s.id
            return True
    return False


def _illegal_online(assignments):
    """ONLINE rows that the school's rules forbid: split-form (A/B half)
    sessions. Online is strictly the combined form, so an A-only / B-only row
    may never ship in the ONLINE venue - even when the rest of the unit is
    online. Combined-AB online rows alongside an in-person row are the school's
    dual campus/VLE design and are NOT violations."""
    return [a for a in assignments if a.room == ONLINE_ROOM and _is_split_form(a.session)]


def _seat_one_round(problem, assignments):
    """One occupancy-aware pass seating every ONLINE row that must be in
    person (split-form halves and ONLINE members of mixed units). Split forms
    are processed first so they win the free cells over legitimately-online
    rows. Returns the number seated. Never moves other sessions, never falls
    back to ONLINE."""
    room_occ, sec_occ, lec_occ = _build_occupancy(assignments, N_SLOTS)
    targets = _illegal_online(assignments)
    targets.sort(key=lambda a: 0 if _is_split_form(a.session) else 1)
    done = 0
    for a in targets:
        if _place_online_in_room(problem, assignments, a, room_occ, sec_occ, lec_occ):
            done += 1
    return done


def fix_mixed_courses(problem, assignments, max_rounds=8):
    """Final guard: seat every ONLINE row that must be in person until no more
    progress. Online is strictly the combined form, so the only ONLINE rows
    that survive are combined-AB courses and single-group course rows (VLE
    whole-cohort lectures). Residuals (split sessions that could not fit any
    room) are reported, never silently shipped as ONLINE. Returns a dict of
    counters."""
    seated = 0
    for _ in range(max_rounds):
        done = _seat_one_round(problem, assignments)
        seated += done
        if done == 0:
            break
    left = _illegal_online(assignments)
    left_names = sorted(a.session.id for a in left)
    return {
        "seated": seated,
        "left": len(left),
        "left_names": left_names[:40],
    }


def _same_course_day_count(assignments, session, day):
    """Physical sessions of this course already booked on `day` (the VLE ONLINE
    row itself is not a classroom, so it never counts against the daily cap)."""
    return sum(
        1 for a in assignments
        if a.session.course.code == session.course.code
        and day_index_of(a.slot) == day
        and a.room not in (ONLINE_ROOM, FIELD_WORK_ROOM)
    )


def _find_seat(problem, assignments, session, room_occ, sec_occ, lec_occ, daily_cap,
               strict_section_day=False, skip_day=None):
    """Non-mutating best-fit seat search feeding the utilisation sweep.
    Returns (room_name, slot) or None. Honours the size-tier rule (TIER_STRICT:
    small/mid must not take a 120-seat hall), _allowed_starts (lunch break,
    Saturday = RT online only, field-work window), the same-course daily cap,
    and the live room/section/lecturer occupancy. With strict_section_day, skips
    any day where a section of this course already has a physical class of the
    same course (so splits/halves never double-book a section in one day).
    skip_day excludes one day index (keeps A and B halves of a course apart)."""
    from src.solver import _tier, TIER_STRICT
    need = max(session.size, session.course.min_capacity)
    if TIER_STRICT and _tier(need) <= 2:
        rooms = [r for r in _room_candidates(session, problem["rooms"]) if _tier(r.capacity) <= 2]
    else:
        rooms = _room_candidates(session, problem["rooms"])
    dur = session.duration
    for t in _allowed_starts(session):
        d = day_index_of(t)
        if skip_day is not None and d == skip_day:
            continue
        if daily_cap and _same_course_day_count(assignments, session, d) >= daily_cap:
            continue
        if strict_section_day:
            blocked = False
            for a in assignments:
                if (a.room not in (ONLINE_ROOM, FIELD_WORK_ROOM)
                        and day_index_of(a.slot) == d
                        and a.session.course.code == session.course.code
                        and a.session.sections.intersection(session.sections)):
                    blocked = True
                    break
            if blocked:
                continue
        cover = range(t, t + dur)
        for r in rooms:
            rn = r.name
            if any(room_occ.get(rn, [None] * N_SLOTS)[u] is not None for u in cover):
                continue
            if any((sec_occ.get(sec) or [None] * N_SLOTS)[u] is not None
                   for sec in session.sections for u in cover):
                continue
            if session.course.lecturer:
                lec = lec_occ.get(session.course.lecturer) or [None] * N_SLOTS
                if any(lec[u] is not None for u in cover):
                    continue
            return rn, t
    return None


def _commit_seat(assignments, session, seat, room_occ, sec_occ, lec_occ):
    """Place a (probably synthetic) session and update the occupancy arrays."""
    rn, t = seat
    assignments.append(Assignment(session, t, rn))
    for u in range(t, t + session.duration):
        room_occ.setdefault(rn, [None] * N_SLOTS)[u] = session.id
        for sec in session.sections:
            sec_occ.setdefault(sec, [None] * N_SLOTS)[u] = session.id
        if session.course.lecturer:
            lec_occ.setdefault(session.course.lecturer, [None] * N_SLOTS)[u] = session.id


def max_utilisation_sweep(problem, assignments):
    """Raise % utilisation by seating the remaining combined-ONLINE rows in free
    room cells. Whole-seat a row when any compatible cell is free; big rows
    (no free 120-cap hall) are split into physical A/B half-sessions that fit
    the 80-cap rooms, keeping the VLE combined row ONLINE (parallel-stream
    teaching, the school's dual pattern). Unseatable rows stay ONLINE. Never
    creates conflicts. Returns dict counters."""
    overrides = problem.get("overrides") or {}
    daily_cap = int(overrides.get("daily_max_sessions") or 3)
    targets = sorted(
        (a for a in assignments if a.room == ONLINE_ROOM and not _is_split_form(a.session)),
        key=lambda a: (-a.session.size, a.session.id),
    )
    from src.models import Session as _Session
    seq = 900
    whole = split = left = 0
    for a in targets:
        s = a.session
        rebuild = _build_occupancy(assignments, N_SLOTS)
        seat = _find_seat(problem, assignments, s, *rebuild, daily_cap, strict_section_day=True)
        if seat is not None:
            a.room, a.slot = seat[0], seat[1]
            whole += 1
            continue
        if s.size > 80 and len(s.sections) == 2:
            secs = sorted(s.sections)
            code_ab = f"{s.course.code}-{s.course.programme}{s.course.level}-AB"
            ha = _Session(s.course, seq, (s.size + 1) // 2, {secs[0]}, s.duration,
                          False, s.field_work, code_ab)
            hb = _Session(s.course, seq + 1, s.size // 2, {secs[1]}, s.duration,
                          False, s.field_work, code_ab)
            seq += 2
            room_occ, sec_occ, lec_occ = rebuild
            seat_a = _find_seat(problem, assignments, ha, room_occ, sec_occ, lec_occ, daily_cap, strict_section_day=True)
            if seat_a is None:
                left += 1
                continue
            _commit_seat(assignments, ha, seat_a, room_occ, sec_occ, lec_occ)
            seat_b = _find_seat(problem, assignments, hb, room_occ, sec_occ, lec_occ, daily_cap, strict_section_day=True)
            if seat_b is not None:
                _commit_seat(assignments, hb, seat_b, room_occ, sec_occ, lec_occ)
                split += 1
            else:
                assignments[:] = [x for x in assignments if x.session.id != ha.id]
                left += 1
            continue
        left += 1
    return {"whole": whole, "split": split, "left": left}


def fill_free_cells(problem, assignments, skip_lab=None):
    """Final, fully-deterministic hole-filler. Runs after every other pass, so it
    only claims room cells that nothing else took.

    Two steps:
      1. Direct seat: any remaining ONLINE combined row is seated into a free
         lecture-room cell when its whole duration clears the hard rules
         (capacity, lunch break, Saturday, section/lecturer occupancy, daily cap,
         same-course-per-day). 120-seat halls are scanned first.
      2. Atomic A/B split: a leftover big combined-AB ONLINE row (size > 80) is
         split into physical A/B halves only when BOTH halves land in the two
         consecutive cells of a <=80 lecture room at the same day/time. A lone
         half never ships.

    COMPUTER LAB and other lab-kind rooms are excluded unless allow_lab_overflow
    is enabled. Returns counters; never creates conflicts."""
    from src.models import Session as _Session
    from src.solver import _allowed_starts, _is_split_form
    overrides = problem.get("overrides") or {}
    if skip_lab is None:
        skip_lab = not bool(overrides.get("allow_lab_overflow", False))
    daily_cap = int(overrides.get("daily_max_sessions") or 3)

    targets = [
        r for r in problem["rooms"]
        if r.name not in (ONLINE_ROOM, FIELD_WORK_ROOM) and not (skip_lab and r.kind == "lab")
    ]

    room_occ, sec_occ, lec_occ = _build_occupancy(assignments, N_SLOTS)

    def same_code_day_count(session, day):
        return sum(
            1 for a in assignments
            if a.room not in (ONLINE_ROOM, FIELD_WORK_ROOM)
            and a.session.course.code == session.course.code
            and day_index_of(a.slot) == day
        )

    def same_day_section_blocked(session, day):
        for a in assignments:
            if (a.room not in (ONLINE_ROOM, FIELD_WORK_ROOM)
                    and day_index_of(a.slot) == day
                    and a.session.course.code == session.course.code
                    and a.session.sections.intersection(session.sections)):
                return True
        return False

    def place_ok(session, rn, g, growth=1, why=None):
        need = max(session.size, session.course.min_capacity)
        cap = next(r.capacity for r in problem["rooms"] if r.name == rn)
        if need > cap:
            if why is not None: why.append(f"capacity need={need}>cap={cap}")
            return False
        if any(room_occ.get(rn, [None] * N_SLOTS)[u] for u in range(g, g + session.duration)):
            if why is not None: why.append("room")
            return False
        if any((sec_occ.get(sec) or [None] * N_SLOTS)[u]
               for sec in session.sections for u in range(g, g + session.duration)):
            if why is not None: why.append("section")
            return False
        lec = session.course.lecturer
        if lec and any((lec_occ.get(lec) or [None] * N_SLOTS)[u] for u in range(g, g + session.duration)):
            if why is not None: why.append("lecturer")
            return False
        if g not in _allowed_starts(session):
            if why is not None: why.append("allowed_start")
            return False
        d0 = day_index_of(g)
        if same_day_section_blocked(session, d0):
            if why is not None: why.append("same_day_sec")
            return False
        if daily_cap and same_code_day_count(session, d0) + growth > daily_cap:
            if why is not None: why.append(f"daily_cap cnt={same_code_day_count(session, d0)}")
            return False
        return True

    def commit_online(a, rn, g):
        a.room, a.slot = rn, g
        for u in range(g, g + a.session.duration):
            room_occ.setdefault(rn, [None] * N_SLOTS)[u] = a.session.id
            for sec in a.session.sections:
                sec_occ.setdefault(sec, [None] * N_SLOTS)[u] = a.session.id
            if a.session.course.lecturer:
                lec_occ.setdefault(a.session.course.lecturer, [None] * N_SLOTS)[u] = a.session.id

    counters = {"direct": 0, "split": 0, "rooms_left": 0, "rows_left": 0}
    hole = room_holes(assignments, problem)

    # ---- step 1: direct seat into any free lecture-room cell ----
    candidates = [
        a for a in assignments
        if a.room == ONLINE_ROOM and a.session.online and not a.session.field_work
        and not _is_split_form(a.session)
    ]
    candidates.sort(key=lambda a: (-a.session.size, a.session.id))

    cells = []
    for r in targets:
        for d0 in range(5):
            for t0 in range(SLOTS_PER_DAY):
                g = d0 * SLOTS_PER_DAY + t0
                cells.append((r.capacity, r.name, g))
    cells.sort(key=lambda c: (-c[0], c[1], c[2]))

    taken = set()
    for cap, rn, g in cells:
        if (rn, g) in taken:
            continue
        if any(room_occ.get(rn, [None] * N_SLOTS)[u] for u in range(g, g + 1)):
            continue
        for a in candidates:
            if a.session.id in taken:
                continue
            s = a.session
            if cap < max(s.size, s.course.min_capacity):
                continue
            if slot_in_day(g) + s.duration > SLOTS_PER_DAY:
                continue
            if any(room_occ.get(rn, [None] * N_SLOTS)[u] for u in range(g, g + s.duration)):
                continue
            if not place_ok(s, rn, g):
                continue
            room_before, slot_before = a.room, a.slot
            commit_online(a, rn, g)
            new_holes = room_holes(assignments, problem)
            if new_holes < hole:
                hole = new_holes
                taken.add(a.session.id)
                counters["direct"] += 1
            else:
                a.room, a.slot = room_before, slot_before
                room_occ, sec_occ, lec_occ = _build_occupancy(assignments, N_SLOTS)
            break

    # ---- step 2: atomic A/B split into two (possibly different) free seats ----
    big = [
        a for a in assignments
        if a.room == ONLINE_ROOM and a.session.online and not a.session.field_work
        and len(a.session.sections) == 2 and a.session.size > 80
    ]
    big.sort(key=lambda a: (-a.session.size, a.session.id))

    seq = 1100
    for a in big:
        if a.session.id in taken:
            continue
        s = a.session
        secs = sorted(s.sections)
        code_ab = f"{s.course.code}-{s.course.programme}{s.course.level}-AB"
        ha = _Session(s.course, seq, (s.size + 1) // 2, {secs[0]}, s.duration,
                      False, s.field_work, code_ab)
        hb = _Session(s.course, seq + 1, s.size // 2, {secs[1]}, s.duration,
                      False, s.field_work, code_ab)
        seq += 2
        seat_a = _find_seat(problem, assignments, ha, room_occ, sec_occ, lec_occ,
                            daily_cap, strict_section_day=True)
        if seat_a is None:
            continue
        _commit_seat(assignments, ha, seat_a, room_occ, sec_occ, lec_occ)
        seat_b = _find_seat(problem, assignments, hb, room_occ, sec_occ, lec_occ,
                            daily_cap, strict_section_day=True,
                            skip_day=day_index_of(seat_a[1]))
        if seat_b is None:
            assignments[:] = [x for x in assignments if x.session.id != ha.id]
            room_occ, sec_occ, lec_occ = _build_occupancy(assignments, N_SLOTS)
            continue
        _commit_seat(assignments, hb, seat_b, room_occ, sec_occ, lec_occ)
        assignments.remove(a)
        new_holes = room_holes(assignments, problem)
        if new_holes < hole:
            hole = new_holes
            counters["split"] += 1
            taken.add(a.session.id)
        else:
            assignments[:] = [x for x in assignments
                              if x.session.id not in (ha.id, hb.id)]
            assignments.append(a)
            room_occ, sec_occ, lec_occ = _build_occupancy(assignments, N_SLOTS)

    counters["rows_left"] = sum(
        1 for a in assignments
        if a.room == ONLINE_ROOM and a.session.online and not a.session.field_work
        and not _is_split_form(a.session)
    )
    counters["rooms_left"] = room_holes(assignments, problem)
    return counters


def room_cell_usage(assignments, rooms):
    """(used, total) room-slot cells for the % utilisation report. Physical
    classes only - ONLINE and FIELD WORK occupy no classroom."""
    used = set()
    for a in assignments:
        if a.room in (ONLINE_ROOM, FIELD_WORK_ROOM):
            continue
        d = day_index_of(a.slot)
        if d >= 5:
            continue
        t0 = slot_in_day(a.slot)
        for k in range(a.session.duration):
            t = t0 + k
            if t < SLOTS_PER_DAY:
                used.add((a.room, d, t))
    return len(used), len(rooms) * 5 * SLOTS_PER_DAY


def postprocess(problem, assignments, sem="?"):
    """Repair, compact, fill online rooms, then pack + chain-fill (keep best of
    4 trials), rebalance, and finally force-repair section/lecturer overlaps so
    the result ships conflict-free. Returns the final assignment list. Mutates
    clones of the input."""
    problem["soft_sections"] = False
    problem["soft_lecturer"] = False
    # Construction-side tiering: for the whole postprocess, small/mid classes
    # (tier need <= 2) are not allowed to take the 120-seat halls, so no pass
    # can re-plant them there (phase1 was solved tight already as well).
    import src.solver as _solver
    _solver.TIER_STRICT = True
    result = clone_assign(assignments)
    repair_assignments(problem, result)
    print(f"[{sem}] compact ...", flush=True)
    compact(problem, result, time_budget=30)

    # Seat-first now, while the ONLINE split A/B rows are still consuming no
    # room hours and the free cells still exist. If this waits until the later
    # online-to-room sweeps, those sweeps greedily give the cells to
    # legitimately-online rows and the split A/B courses (ES 376 / EL 162 /
    # ...) have nowhere left to go.
    print(f"[{sem}] early mixed-course guard (seat-first) ...", flush=True)
    fx_early = fix_mixed_courses(problem, result, max_rounds=4)
    print(
        f"[{sem}] early mixed-guard: seated {fx_early['seated']} online rows in rooms | "
        f"left: {fx_early['left']}",
        flush=True,
    )

    fill_online_rooms(problem, result, time_budget=15)
    compact(problem, result, time_budget=15)

    print(f"[{sem}] pack (keep best of trials) ...", flush=True)
    best_assign, best_holes = None, None
    for i in range(2):
        cand = clone_assign(result)
        rng = random.Random(random.randint(1, 2**31 - 1))
        for _ in range(2):
            pack(problem, cand, time_budget=15, rng=rng)
            Packer(problem, cand, rng=rng).fill_holes(max_depth=4)
        holes = room_holes(cand, problem)
        print(f"[{sem}] pack trial {i + 1}: room idle holes={holes}", flush=True)
        if best_holes is None or holes < best_holes:
            best_assign, best_holes = cand, holes
    result = best_assign

    print(f"[{sem}] rebalance room-day loads ...", flush=True)
    holes_before = room_holes(result, problem)
    from src.rebalance import rebalance
    rebalance(problem, result, min_load=8, time_budget=60)
    holes_after = room_holes(result, problem)
    if holes_after > holes_before:
        print(f"[{sem}] WARNING rebalance added holes: {holes_before} -> {holes_after}", flush=True)

    print(f"[{sem}] prefer SR 4 for small classes ...", flush=True)
    sr4_moved = prefer_sr4(problem, result)
    print(f"[{sem}] small classes moved to SR 4: {sr4_moved}", flush=True)

    print(f"[{sem}] co-teach fallback for small classes left over ...", flush=True)
    merged = co_teach_merge(problem, result)
    print(f"[{sem}] small classes merged into same-course partners: {merged}", flush=True)

    print(f"[{sem}] convert online classes into free rooms ...", flush=True)
    inperson = fill_online_rooms(problem, result, time_budget=30)

    # Final safeguard: fix any break-crossing sessions
    print(f"[{sem}] fixing break-crossing sessions ...", flush=True)
    fixed = _fix_break_crossing(problem, result)
    if fixed:
        print(f"[{sem}] fixed {fixed} sessions crossing lunch break", flush=True)
    else:
        print(f"[{sem}] no break-crossing sessions found", flush=True)
    print(f"[{sem}] online classes converted in person: {inperson}", flush=True)

    print(f"[{sem}] final conflict repair ...", flush=True)
    result, remaining = ensure_conflict_free(problem, result)
    if any(remaining[k] for k in ("section", "lecturer", "room")):
        print(f"[{sem}] WARNING: could not fully repair {remaining}", flush=True)
    else:
        print(f"[{sem}] conflict-free (section/lecturer/room = 0)", flush=True)

    # Final pass: convert remaining online sessions into any free room slots
    # that survived the pack/rebalance/repair pipeline. Without this, rooms
    # that were empty at certain times (e.g. SR 13 at 06:30) stay idle while
    # compatible online sessions sit unused.
    print(f"[{sem}] final online-to-room sweep ...", flush=True)
    final_online = fill_online_rooms(problem, result, time_budget=60)
    if final_online:
        print(f"[{sem}] final sweep converted {final_online} online sessions to rooms", flush=True)
    else:
        print(f"[{sem}] no more online sessions to place", flush=True)

    # Aggressive fill: chain-shift placement of REMAINING fully-online courses
    # (after consistency checks, all mixed courses are resolved to all-physical or all-online)
    print(f"[{sem}] aggressive fill (chain shifts) ...", flush=True)
    packer = Packer(problem, result)
    agg, _ = packer.aggressive_fill(problem, result, time_budget=180, max_depth=3, max_restarts=6)
    if agg:
        print(f"[{sem}] aggressive fill placed {agg} online sessions", flush=True)
    else:
        print(f"[{sem}] aggressive fill found no placements", flush=True)

    # Repair any conflicts introduced by aggressive fill
    print(f"[{sem}] conflict repair after aggressive fill ...", flush=True)
    result, remaining = ensure_conflict_free(problem, result)
    if any(remaining[k] for k in ("section", "lecturer", "room")):
        print(f"[{sem}] WARNING: could not fully repair {remaining}", flush=True)
    else:
        print(f"[{sem}] conflict-free after aggressive fill (section/lecturer/room = 0)", flush=True)

    # Consistency fixes AFTER aggressive fill: ensure no mixed physical/online sessions remain
    # Aggressive fill may place some sessions of a course physically, leaving others ONLINE.
    # This pass resolves those mixed courses.
    # Paired session consistency fix
    print(f"[{sem}] paired session consistency check (post-aggressive) ...", flush=True)
    fixed = 0
    split_groups = {}
    for a in result:
        sg = getattr(a.session, 'split_group', None)
        if sg:
            split_groups.setdefault(sg, []).append(a)
    
    room_occ = {}
    for r in problem["rooms"]:
        room_occ[r.name] = [None] * N_SLOTS
    for a in result:
        if a.room not in (ONLINE_ROOM, FIELD_WORK_ROOM):
            t = a.slot
            for u in range(t, t + a.session.duration):
                room_occ[a.room][u] = a.session.id
    
    sec_occ = {}
    lec_occ = {}
    for a in result:
        for sec in a.session.sections:
            sec_occ.setdefault(sec, [None] * N_SLOTS)
            if a.room not in (ONLINE_ROOM, FIELD_WORK_ROOM):
                t = a.slot
                for u in range(t, t + a.session.duration):
                    sec_occ[sec][u] = a.session.id
        if a.session.course.lecturer:
            lec_occ.setdefault(a.session.course.lecturer, [None] * N_SLOTS)
            if a.room not in (ONLINE_ROOM, FIELD_WORK_ROOM):
                t = a.slot
                for u in range(t, t + a.session.duration):
                    lec_occ[a.session.course.lecturer][u] = a.session.id
    
    for sg, paired in split_groups.items():
        physical = [a for a in paired if a.room not in (ONLINE_ROOM, FIELD_WORK_ROOM)]
        online = [a for a in paired if a.room == ONLINE_ROOM]
        
        if not physical or not online:
            continue
        
        for online_a in online:
            session = online_a.session
            placed = False
            need = max(session.size, session.course.min_capacity)
            wanted_kind = "lab" if session.course.practical_hours > 0 else "lecture"
            candidates = [r for r in problem["rooms"] if r.kind == wanted_kind and r.capacity >= need]
            if not candidates:
                candidates = [r for r in problem["rooms"] if r.capacity >= need]
            elif wanted_kind == "lab":
                candidates = candidates + [r for r in problem["rooms"] if r.kind != "lab" and r.capacity >= need]
            
            for r in candidates:
                rn = r.name
                if rn in (ONLINE_ROOM, FIELD_WORK_ROOM):
                    continue
                arr = room_occ[rn]
                for t in range(N_SLOTS):
                    free = True
                    for u in range(t, t + session.duration):
                        if u >= N_SLOTS or arr[u] is not None:
                            free = False
                            break
                    if not free:
                        continue
                    sec_conflict = False
                    for sec in session.sections:
                        sec_arr = sec_occ.get(sec, [None] * N_SLOTS)
                        for u in range(t, t + session.duration):
                            if u < N_SLOTS and sec_arr[u] is not None:
                                sec_conflict = True
                                break
                        if sec_conflict:
                            break
                    if sec_conflict:
                        continue
                    lec_conflict = False
                    if session.course.lecturer:
                        lec_arr = lec_occ.get(session.course.lecturer, [None] * N_SLOTS)
                        for u in range(t, t + session.duration):
                            if u < N_SLOTS and lec_arr[u] is not None:
                                lec_conflict = True
                                break
                    if lec_conflict:
                        continue
                    if day_index_of(t) == len(DAYS) - 1 and (not session.online or not session.course.code.startswith("RT")):
                        continue
                    s = slot_in_day(t)
                    if s + session.duration > SLOTS_PER_DAY:
                        continue
                    online_a.room = rn
                    online_a.slot = t
                    for u in range(t, t + session.duration):
                        arr[u] = session.id
                        for sec in session.sections:
                            sec_occ[sec][u] = session.id
                        if session.course.lecturer:
                            lec_occ[session.course.lecturer][u] = session.id
                    placed = True
                    break
                if placed:
                    break
            
            if not placed:
                # Do NOT move the physical half to ONLINE - online is strictly
                # the combined form. Leave the pair; the final fix_mixed_courses
                # pass retries seating and reports anything that still cannot
                # fit into a room.
                fixed += 1
                break
    more = _seat_one_round(problem, result)
    if fixed or more:
        print(f"[{sem}] fixed {fixed} mixed paired sessions, seated {more} (never ONLINE)", flush=True)
    else:
        print(f"[{sem}] all paired sessions consistent", flush=True)

    # Repair any conflicts introduced by consistency passes
    print(f"[{sem}] conflict repair after consistency ...", flush=True)
    result, remaining = ensure_conflict_free(problem, result)
    if any(remaining[k] for k in ("section", "lecturer", "room")):
        print(f"[{sem}] WARNING: could not fully repair {remaining}", flush=True)
    else:
        print(f"[{sem}] conflict-free after consistency (section/lecturer/room = 0)", flush=True)

    # Sequential fill: after consistency pass freed cells, try placing each
    # online session (or A/B split pair together) into a free cell. A course
    # may end up with some sessions in person and some ONLINE - that is allowed;
    # only split A/B pairs must stay uniform (both physical or both ONLINE).
    print(f"[{sem}] sequential session fill (tail) ...", flush=True)
    from src.online_placement import place_courses_sequential
    placed_sessions, result = place_courses_sequential(problem, result)
    if placed_sessions:
        print(f"[{sem}] sequential fill placed {placed_sessions} sessions", flush=True)
    else:
        print(f"[{sem}] sequential fill found no placements", flush=True)

    # Repair any conflicts introduced by sequential fill
    print(f"[{sem}] conflict repair after sequential fill ...", flush=True)
    result, remaining = ensure_conflict_free(problem, result)
    if any(remaining[k] for k in ("section", "lecturer", "room")):
        print(f"[{sem}] WARNING: could not fully repair {remaining}", flush=True)
    else:
        print(f"[{sem}] conflict-free after sequential fill (section/lecturer/room = 0)", flush=True)

    # Paired session consistency backstop. Runs AFTER the last repair because
    # the greedy repair can move one member of an A/B split pair to ONLINE and
    # split it. Seat those ONLINE halves back into a room (never flip the
    # physical half ONLINE - online is strictly the combined form, so an A/B
    # half must be in person or be reported later as a residual).
    print(f"[{sem}] paired session consistency check (post-repair) ...", flush=True)
    fixed = _seat_one_round(problem, result)

    if fixed:
        print(f"[{sem}] seated {fixed} mixed paired sessions (post-repair)", flush=True)
    else:
        print(f"[{sem}] all paired sessions consistent", flush=True)

    # One final guard: in case a pair flip collided with another session,
    # repair once more and only ship a clean timetable.
    result, remaining = ensure_conflict_free(problem, result)
    if any(remaining[k] for k in ("section", "lecturer", "room")):
        print(f"[{sem}] WARNING: could not fully repair {remaining}", flush=True)
    else:
        print(f"[{sem}] final state conflict-free (section/lecturer/room = 0)", flush=True)

    # Student-load pass: spread same-course/day duplicates and cap sessions per
    # section per day (settings.json daily_max_sessions, default 3; field work
    # excluded). Sessions that cannot be moved keep their spot and are reported;
    # only online-eligible sessions fall back to ONLINE as a last resort.
    print(f"[{sem}] spread same-course/daily-cap (student load) ...", flush=True)
    spread = fix_same_day_course(problem, result)
    print(
        f"[{sem}] spread: dups {spread['dups_before']}->{spread['dups_after']} "
        f"| over-cap days {spread['over_cap_before']}->{spread['over_cap_after']} "
        f"| moved {spread['moves']} physical + {spread['moved_online']} online, "
        f"{spread['left_put']} kept in place (cap={spread['cap']})",
        flush=True,
    )

    # The spread pass can leave (or split) ONLINE rows in a unit that must be
    # in person - seat those halves back into a room right away.
    print(f"[{sem}] paired session consistency check (post-spread) ...", flush=True)
    fixed = _seat_one_round(problem, result)
    if fixed:
        print(f"[{sem}] seated {fixed} mixed paired sessions after spread", flush=True)
    else:
        print(f"[{sem}] paired sessions consistent after spread", flush=True)

    # Post-spread online-to-room sweep: the spread pass relocated physical
    # classes and opened free cells (including the large halls) AFTER the
    # earlier conversion sweeps ran. Convert the remaining ONLINE units into
    # those freed cells, still honouring the same-course/day rule, the daily
    # load cap and A/B pair unity so the student-load work is not undone.
    print(f"[{sem}] post-spread online-to-room sweep ...", flush=True)
    from src.online_placement import place_courses_sequential
    overrides = problem.get("overrides") or {}
    sweep_cap = int(overrides.get("daily_max_sessions") or 3)
    swept, result = place_courses_sequential(problem, result, daily_cap=sweep_cap)
    if swept:
        print(f"[{sem}] post-spread sweep converted {swept} online sessions in person", flush=True)
    else:
        print(f"[{sem}] post-spread sweep found no online sessions to convert", flush=True)

    # Size-tier room-fit pass (option B): keep the largest rooms for the
    # largest cohorts - evict smaller classes from the 120-seat halls and seat
    # the biggest ONLINE units into the freed hall cells (chain-swap, both stay
    # in person). Respects the same-course/day rule, the daily cap and pair
    # uniformity.
    print(f"[{sem}] size-tier room-fit pass ...", flush=True)
    from src.online_placement import place_by_size_tiers
    sized = place_by_size_tiers(problem, result)
    print(
        f"[{sem}] size-fit: evicted {sized['evicted']} smaller classes from halls | "
        f"seated {sized['seated_online']} big online in halls | "
        f"{sized['left_big_online']} big online left (reported) | "
        f"small/mid still in halls: {sized['small_mid_in_hall_after']}",
        flush=True,
    )

    # Chain-relocation hall evacuation: classes stuck in halls whose only
    # fitting smaller cells are blocked by another class that CAN move. BFS
    # chains (S -> X's cell -> X -> X's free cell) clear those, freeing hall
    # seats for the big cohorts. Same rules as the spread pass are enforced
    # per hop; field work / ONLINE stay untouched.
    print(f"[{sem}] chain-relocation hall evacuation ...", flush=True)
    from src.online_placement import relocate_stuck_hall_residents
    chained = relocate_stuck_hall_residents(problem, result)
    print(
        f"[{sem}] chain-reloc: moved {chained['moved']} classes out of halls | "
        f"{chained['left_leftover_hall']} small/mid still stuck (reported)",
        flush=True,
    )

    # Re-seat the big ONLINE leftovers into the hall cells that the chain pass
    # just freed; give it a fresh attempt budget.
    print(f"[{sem}] re-seat big online after evacuation ...", flush=True)
    sized2 = place_by_size_tiers(problem, result)
    print(
        f"[{sem}] re-seat: +{sized2['seated_online']} big online in halls | "
        f"{sized2['left_big_online']} big online left | "
        f"small/mid in halls: {sized2['small_mid_in_hall_after']}",
        flush=True,
    )

    # Best-of-K spread: the dup residual is order-dependent (shared swap
    # budget, shuffled traversal). Run the same layout through several shuffle
    # orders and keep the one with the fewest duplicates - deterministic, no
    # full re-solve.
    print(f"[{sem}] best-of-K spread (same layout, 5 shuffle orders) ...", flush=True)
    best = None
    for kseed in (7, 11, 13, 17, 19):
        snapshot = [(a, a.slot, a.room) for a in result]
        stat = fix_same_day_course(problem, result, seed=kseed)
        mixed = _verify(result, problem).get("mixed_course", 0)
        key = (stat["dups_after"], stat["over_cap_after"], mixed)
        if best is None or key < best[0]:
            best = (key, kseed, stat)
        else:
            for a, sl, rr in snapshot:
                a.slot, a.room = sl, rr
    print(
        f"[{sem}] best-of-K: seed={best[1]} | dups={best[0][0]} | "
        f"over-cap-days={best[0][1]} | mixed-units={best[0][2]} | "
        f"moves={best[2]['moves']} + "
        f"online={best[2]['moved_online']}, left_put={best[2]['left_put']}",
        flush=True,
    )

    # Final guard after the spread + pair pass
    result, remaining = ensure_conflict_free(problem, result)
    if any(remaining[k] for k in ("section", "lecturer", "room")):
        print(f"[{sem}] WARNING: spread left conflicts {remaining}", flush=True)
    else:
        print(f"[{sem}] final state conflict-free after spread (section/lecturer/room = 0)", flush=True)

    # Construction-side tiering guard: whatever pass re-planted small/mid
    # classes into the 120-seat halls, evict them into their best fitting room.
    from src.pack import evacuate_smalls_from_halls
    hall_leaks = evacuate_smalls_from_halls(problem, result)
    if hall_leaks:
        print(f"[{sem}] hall-leak guard relocated {hall_leaks} small classes out of halls", flush=True)
        result, remaining = ensure_conflict_free(problem, result)

    # Final mixed-course guard: seat every ONLINE row that must be in person
    # (split A/B halves and ONLINE rows of units that also have physical rows).
    # This is what makes ES 376 / EL 162 style courses share the same row shape
    # as every other course when a room fits, and it reports a residual instead
    # of silently shipping split sessions as ONLINE.
    print(f"[{sem}] final mixed-course guard (seat-first) ...", flush=True)
    fx = fix_mixed_courses(problem, result)
    print(
        f"[{sem}] mixed-guard: seated {fx['seated']} online rows in rooms | "
        f"residual split/illegal ONLINE: {fx['left']}",
        flush=True,
    )
    if fx["left"]:
        for name in fx["left_names"]:
            a = next(x for x in result if x.session.id == name)
            print(f"   RESIDUAL ONLINE (no room fit): {name} @ slot {a.slot}", flush=True)
        result, remaining = ensure_conflict_free(problem, result)
        if any(remaining[k] for k in ("section", "lecturer", "room")):
            print(f"[{sem}] WARNING: mixed-guard left conflicts {remaining}", flush=True)

    # Utilisation sweep: seat remaining combined-ONLINE rows in free room cells
    # (splitting oversized rows into physical A/B halves when no hall fits it).
    print(f"[{sem}] utilisation sweep (seat/split combined-ONLINE rows) ...", flush=True)
    us = max_utilisation_sweep(problem, result)
    print(
        f"[{sem}] util-sweep: seated whole={us['whole']} split-AB={us['split']} | "
        f"left online (unseatable)={us['left']}",
        flush=True,
    )
    result, remaining = ensure_conflict_free(problem, result)
    if any(remaining[k] for k in ("section", "lecturer", "room")):
        print(f"[{sem}] WARNING: utilisation sweep left conflicts {remaining}", flush=True)
    else:
        print(f"[{sem}] conflict-free after utilisation sweep", flush=True)
    sd = fix_same_day_course(problem, result, seed=7)
    print(
        f"[{sem}] same-day smoothing after util-sweep: "
        f"dups={sd['dups_after']} over-cap-days={sd['over_cap_after']}",
        flush=True,
    )
    result, remaining = ensure_conflict_free(problem, result)
    if any(remaining[k] for k in ("section", "lecturer", "room")):
        print(f"[{sem}] WARNING: same-day smoothing left conflicts {remaining}", flush=True)

    skip_fill = not bool(((problem.get("overrides") or {})).get("fill_free_cells", True))
    if skip_fill:
        print(f"[{sem}] fill_free_cells disabled by settings; skipping hole-fill pass", flush=True)
    else:
        before = room_holes(result, problem)
        fc = fill_free_cells(problem, result)
        result, remaining = ensure_conflict_free(problem, result)
        if any(remaining[k] for k in ("section", "lecturer", "room")):
            print(f"[{sem}] WARNING: fill-free-cell pass left conflicts {remaining}", flush=True)
        print(
            f"[{sem}] fill-free-cell pass: direct={fc['direct']} split={fc['split']} "
            f"rows_left={fc['rows_left']} holes {before} -> {fc['rooms_left']}",
            flush=True,
        )

    used_cells, total_cells = room_cell_usage(result, problem["rooms"])
    print(
        f"[{sem}] utilisation: {used_cells}/{total_cells} room-slot cells "
        f"({used_cells / total_cells * 100:.1f}%)",
        flush=True,
    )

    return result


if __name__ == "__main__":
    for sem in sys.argv[1:] or list_semesters():
        run(sem)
    print("ALL DONE", flush=True)
