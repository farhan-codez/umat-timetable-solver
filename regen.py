import sys, json, os, subprocess, time, random, tempfile
from copy import copy
from pathlib import Path
from types import SimpleNamespace
from src.paths import PROJECT_ROOT, DATA_DIR, OUTPUT_DIR
from src.loaders import load_problem, list_semesters
from src.solver import load_solution_json, repair_assignments, _verify, Assignment
from src.compact import compact, fill_online_rooms
from src.export import export_all
from src.pack import pack, Packer

import logging
log = logging.getLogger("umat.regen")

PY = sys.executable
WORKER = str(Path(__file__).resolve().parent / "regen_worker.py")
WORK = Path(tempfile.gettempdir()) / "umat-tt"
os.makedirs(WORK, exist_ok=True)


def run_phase(sem, phase, time_limit, in_path, out_path, seed, grace=180):
    if os.path.exists(out_path):
        os.remove(out_path)
    cmd = [PY, "-u", WORKER, sem, phase, str(time_limit), in_path or "none", out_path, str(seed)]
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
    for seed in (42, 7):
        print(f"[{sem}] phase1 seed={seed} (hints) ...", flush=True)
        rc, _ = run_phase(sem, "1", phase1_limit, hints_path, out1, seed)
        status, obj, assign1 = best_solution(out1, sessions)
        if assign1:
            phase1_done = True
            print(f"[{sem}] phase1 recovered status={status} sessions={len(assign1)}", flush=True)
            break
        print(f"[{sem}] phase1 seed={seed} found nothing", flush=True)
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

    out_dir = OUTPUT_DIR / sem
    summary = {
        "status": "FEASIBLE" if status in (None, "NO SOLUTION") else status,
        "objective": round(obj) if obj not in (None, float("inf")) else None,
        "conflicts": {k: checks[k] for k in ("section", "room", "capacity")},
        "lecturer_overlaps": checks.get("lecturer", 0),
        "sessions": len(sessions),
        "sections": len(problem["sections"]),
        "lecturers": len(problem["lecturers"]),
        "rooms": len(problem["rooms"]),
        "room_idle_holes": room_holes(result_assign, problem),
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

def postprocess(problem, assignments, sem="?"):
    """Repair, compact, fill online rooms, then pack + chain-fill (keep best of
    4 trials), rebalance, and finally force-repair section/lecturer overlaps so
    the result ships conflict-free. Returns the final assignment list. Mutates
    clones of the input."""
    problem["soft_sections"] = False
    problem["soft_lecturer"] = False
    result = clone_assign(assignments)
    repair_assignments(problem, result)
    print(f"[{sem}] compact ...", flush=True)
    compact(problem, result, time_budget=120)
    fill_online_rooms(problem, result, max_gap_cost=2, time_budget=60)
    compact(problem, result, time_budget=60)

    print(f"[{sem}] pack (keep best of trials) ...", flush=True)
    best_assign, best_holes = None, None
    for i in range(4):
        cand = clone_assign(result)
        rng = random.Random(random.randint(1, 2**31 - 1))
        for _ in range(3):
            pack(problem, cand, time_budget=60, rng=rng)
            Packer(problem, cand, rng=rng).fill_holes(max_depth=4)
        holes = room_holes(cand, problem)
        print(f"[{sem}] pack trial {i + 1}: room idle holes={holes}", flush=True)
        if best_holes is None or holes < best_holes:
            best_assign, best_holes = cand, holes
    result = best_assign

    print(f"[{sem}] rebalance room-day loads ...", flush=True)
    holes_before = room_holes(result, problem)
    from src.rebalance import rebalance
    rebalance(problem, result, min_load=8, time_budget=240)
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
    inperson = fill_online_rooms(problem, result, time_budget=120)

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
    final_online = fill_online_rooms(problem, result, time_budget=120)
    if final_online:
        print(f"[{sem}] final sweep converted {final_online} online sessions to rooms", flush=True)
    else:
        print(f"[{sem}] no more online sessions to place", flush=True)

    return result


if __name__ == "__main__":
    for sem in sys.argv[1:] or list_semesters():
        run(sem)
    print("ALL DONE", flush=True)
