import sys, json, os, subprocess, time, random, tempfile
from copy import copy
from pathlib import Path
from types import SimpleNamespace
from src.paths import PROJECT_ROOT, DATA_DIR, OUTPUT_DIR
from src.loaders import load_problem, list_semesters
from src.solver import load_solution_json, repair_assignments, _verify, Assignment, ONLINE_ROOM, FIELD_WORK_ROOM, _allowed_starts, fix_same_day_course
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
    print(f"[{sem}] final verify: " + "; ".join(f"{k}={v}" for k, v in checks.items()), flush=True)

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
    compact(problem, result, time_budget=30)
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
                for pa in physical:
                    pa.room = ONLINE_ROOM
                online_a.room = ONLINE_ROOM
                fixed += 1
                break
    
    if fixed:
        print(f"[{sem}] fixed {fixed} mixed paired sessions (moved to ONLINE)", flush=True)
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
    # split it. Moving both members to ONLINE keeps their slots, so their
    # section/lecturer occupancy is unchanged and no new conflict appears.
    print(f"[{sem}] paired session consistency check (post-repair) ...", flush=True)
    fixed = 0
    split_groups = {}
    for a in result:
        sg = getattr(a.session, 'split_group', None)
        if sg:
            split_groups.setdefault(sg, []).append(a)

    for sg, paired in split_groups.items():
        physical = [a for a in paired if a.room not in (ONLINE_ROOM, FIELD_WORK_ROOM)]
        online = [a for a in paired if a.room == ONLINE_ROOM]
        if physical and online:
            for a in paired:
                a.room = ONLINE_ROOM
            fixed += 1

    if fixed:
        print(f"[{sem}] fixed {fixed} mixed paired sessions (moved to ONLINE)", flush=True)
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

    # ONLINE fallback inside the spread pass can split an A/B pair - re-unify.
    print(f"[{sem}] paired session consistency check (post-spread) ...", flush=True)
    fixed = 0
    split_groups = {}
    for a in result:
        sg = getattr(a.session, 'split_group', None)
        if sg:
            split_groups.setdefault(sg, []).append(a)
    for sg, paired in split_groups.items():
        physical = [a for a in paired if a.room not in (ONLINE_ROOM, FIELD_WORK_ROOM)]
        online = [a for a in paired if a.room == ONLINE_ROOM]
        if physical and online:
            for a in paired:
                a.room = ONLINE_ROOM
            fixed += 1
    if fixed:
        print(f"[{sem}] fixed {fixed} mixed paired sessions after spread", flush=True)
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
        key = (stat["dups_after"], stat["over_cap_after"])
        if best is None or key < best[0]:
            best = (key, kseed, stat)
        else:
            for a, sl, rr in snapshot:
                a.slot, a.room = sl, rr
    print(
        f"[{sem}] best-of-K: seed={best[1]} | dups={best[0][0]} | "
        f"over-cap-days={best[0][1]} | moves={best[2]['moves']} + "
        f"online={best[2]['moved_online']}, left_put={best[2]['left_put']}",
        flush=True,
    )

    # Final guard after the spread + pair pass
    result, remaining = ensure_conflict_free(problem, result)
    if any(remaining[k] for k in ("section", "lecturer", "room")):
        print(f"[{sem}] WARNING: spread left conflicts {remaining}", flush=True)
    else:
        print(f"[{sem}] final state conflict-free after spread (section/lecturer/room = 0)", flush=True)

    return result


if __name__ == "__main__":
    for sem in sys.argv[1:] or list_semesters():
        run(sem)
    print("ALL DONE", flush=True)
