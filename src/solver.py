import json
from dataclasses import dataclass

from ortools.sat.python import cp_model

from .slots import (
    DAYS,
    EVENING_START,
    FIELD_WORK_START_MAX,
    FIELD_WORK_START_MIN,
    N_SLOTS,
    SLOTS_PER_DAY,
    day_index_of,
    slot_in_day,
)

ONLINE_ROOM = "ONLINE"
FIELD_WORK_ROOM = "FIELD WORK"
NO_ROOM_ROOMS = (ONLINE_ROOM, FIELD_WORK_ROOM)


def _is_no_room(room):
    return room in NO_ROOM_ROOMS


@dataclass
class SolverWeights:
    room_oversize: int = 2
    evening: int = 1
    cohort_gap: int = 0
    lecturer_gap: int = 0
    early_utilization: int = 8
    early_penalty_late_level: int = 2
    lecturer_overlap: int = 8
    section_overlap: int = 60
    lab_room: int = 2
    online: int = 100
    room_idle: int = 10


@dataclass
class Assignment:
    session: object
    slot: int
    room: str


@dataclass
class SolveResult:
    status: str
    objective: float
    assignments: list
    checks: dict


def _tier(capacity):
    if capacity <= 40:
        return 1
    if capacity <= 80:
        return 2
    return 3


def _allowed_rooms(session, rooms):
    need = max(session.size, session.course.min_capacity)
    wanted_kind = "lab" if session.course.practical_hours > 0 else "lecture"
    candidates = [r for r in rooms if r.kind == wanted_kind and r.capacity >= need]
    if not candidates:
        candidates = [r for r in rooms if r.capacity >= need]
    elif wanted_kind == "lab":
        # The lab is a preference, not an exclusive venue: the computer lab
        # only seats 60 class-hours a week but lab demand can exceed it, so
        # excess practical classes fall back to any classroom big enough.
        # (A soft weight keeps them in the lab whenever the packing allows.)
        candidates = candidates + [r for r in rooms if r.kind != "lab" and r.capacity >= need]
    names = [r.name for r in candidates]
    if session.field_work:
        # field work happens off campus - it never needs (and must not take) a
        # real classroom, and may run in parallel with other field work groups.
        return [FIELD_WORK_ROOM]
    if session.online:
        # online / VLE sessions may run in person when a classroom is free,
        # otherwise they fall back to the ONLINE venue (which never needs a room).
        names.append(ONLINE_ROOM)
    # physical (online=no) sessions always run in a real classroom.
    # If a combined online session is too large for ANY physical room,
    # ONLINE_ROOM is already in the list (from session.online=True).
    return names


def _allowed_starts(session):
    out = []
    fixed = getattr(session, "fixed_slot", None)
    for t in range(N_SLOTS):
        if fixed is not None and t != fixed:
            continue
        # Saturday is reserved for RT (online) sessions only
        if day_index_of(t) == len(DAYS) - 1 and (not session.online or not session.course.code.startswith("RT")):
            continue
        s = slot_in_day(t)
        if s + session.duration > SLOTS_PER_DAY:
            continue
        # must not cut through the 12:30-13:00 lunch break (between slot 5 and 6)
        if s <= 5 and s + session.duration > 6:
            continue
        # field work happens off campus: keep trips inside the working day
        # (never the 06:30/07:30 early blocks, never the evening).
        if session.field_work and not (FIELD_WORK_START_MIN <= s <= FIELD_WORK_START_MAX):
            continue
        # Level 200+ in-person sessions can start at 06:30/07:30 but with a penalty
        # (handled in objective function via weights.early_penalty_late_level)
        out.append(t)
    return out


def solve(problem, time_limit=30.0, hints=None, minimize_objective=True, feasibility_jump=False, progress_cb=None, seed=None, num_workers=None, solution_path=None, log_search_progress=False, fix_hinted=False):
    rooms = problem["rooms"]
    sessions = problem["sessions"]
    sections = problem["sections"]
    lecturers = problem["lecturers"]
    weights = problem["weights"]
    room_capacity = {r.name: r.capacity for r in rooms}
    room_kind = {r.name: r.kind for r in rooms}

    model = cp_model.CpModel()

    allowed = {}
    starts = {}
    for s in sessions:
        allowed[s.id] = _allowed_rooms(s, rooms)
        if not allowed[s.id]:
            raise ValueError(f"No room fits {s.id} (size {s.size}).")
        starts[s.id] = _allowed_starts(s)

    if hints and fix_hinted:
        hint_map = {}
        for a in hints:
            hint_map[a.session.id] = (a.slot, a.room)
        for s in sessions:
            h = hint_map.get(s.id)
            if h is not None and h[1] in allowed[s.id] and h[0] in starts[s.id]:
                allowed[s.id] = [h[1]]
                starts[s.id] = [h[0]]

    z = {}
    for s in sessions:
        for t in starts[s.id]:
            for r in allowed[s.id]:
                z[(s.id, t, r)] = model.NewBoolVar(f"z_{s.id}_{t}_{r}")

    for s in sessions:
        model.Add(sum(z[(s.id, t, r)] for t in starts[s.id] for r in allowed[s.id]) == 1)

    def covering(s, u):
        """z variables for sessions covering absolute slot u."""
        out = []
        for t in starts[s.id]:
            if t <= u < t + s.duration:
                for r in allowed[s.id]:
                    out.append(z[(s.id, t, r)])
        return out

    soft_sections = problem.get("soft_sections")
    section_overlap_vars = {}
    for sec in sections:
        for u in range(N_SLOTS):
            expr = sum(x for s in sessions if sec in s.sections for x in covering(s, u))
            if soft_sections and minimize_objective:
                b = model.NewBoolVar(f"sec_ov_{sec}_{u}")
                model.Add(expr <= 1 + len(sessions) * b)
                section_overlap_vars[(sec, u)] = b
            elif not soft_sections:
                model.Add(expr <= 1)

    # No same course twice per day per section
    course_sec_sessions = {}
    for s in sessions:
        for sec in s.sections:
            course_sec_sessions.setdefault((s.course.code, sec), []).append(s)
    for (code, sec), group in course_sec_sessions.items():
        if len(group) <= 1:
            continue
        for day in range(len(DAYS)):
            day_vars = []
            base = day * SLOTS_PER_DAY
            for s in group:
                for t in starts[s.id]:
                    if day_index_of(t) != day:
                        continue
                    for r in allowed[s.id]:
                        day_vars.append(z[(s.id, t, r)])
            if day_vars:
                model.Add(sum(day_vars) <= 1)

    lecturer_overlap_vars = {}
    soft_lecturer = problem.get("soft_lecturer")
    for lec in lecturers:
        for u in range(N_SLOTS):
            expr = sum(x for s in sessions if s.course.lecturer == lec for x in covering(s, u))
            if soft_lecturer and minimize_objective:
                b = model.NewBoolVar(f"lec_ov_{lec}_{u}")
                model.Add(expr <= 1 + len(sessions) * b)
                lecturer_overlap_vars[(lec, u)] = b
            elif not soft_lecturer:
                model.Add(expr <= 1)

    for r in room_capacity:
        for u in range(N_SLOTS):
            model.Add(sum(z[(s.id, t, r)]
                          for s in sessions if r in allowed[s.id]
                          for t in starts[s.id] if t <= u < t + s.duration) <= 1)

    terms = []

    if minimize_objective:
        if problem.get("soft_sections"):
            for (sec, u), b in section_overlap_vars.items():
                terms.append(weights.section_overlap * b)
        if problem.get("soft_lecturer"):
            for (lec, u), b in lecturer_overlap_vars.items():
                terms.append(weights.lecturer_overlap * b)
        for s in sessions:
            need = _tier(max(s.size, s.course.min_capacity))
            for t in starts[s.id]:
                occupies_evening = any(slot_in_day(u) >= EVENING_START for u in range(t, t + s.duration))
                for r in allowed[s.id]:
                    if r == ONLINE_ROOM:
                        terms.append(weights.online * z[(s.id, t, r)])
                        continue
                    if r == FIELD_WORK_ROOM:
                        continue
                    over = _tier(room_capacity[r]) - need
                    if over > 0:
                        terms.append(weights.room_oversize * over * z[(s.id, t, r)])
                    if s.course.practical_hours > 0 and room_kind.get(r) != "lab":
                        terms.append(weights.lab_room * z[(s.id, t, r)])
                    if occupies_evening:
                        terms.append(weights.evening * z[(s.id, t, r)])

        early = 3  # first three hours of the day (06:30-09:30)
        for r in room_capacity:
            if r == ONLINE_ROOM:
                continue
            for day in range(len(DAYS)):
                for sl in range(early):
                    u = day * SLOTS_PER_DAY + sl
                    expr = sum(z[(s.id, t, r)]
                               for s in sessions if r in allowed[s.id]
                               for t in starts[s.id] if t <= u < t + s.duration)
                    occ = model.NewBoolVar(f"earlyocc_{r}_{u}")
                    model.Add(occ == expr)
                    terms.append(weights.early_utilization * (1 - occ))

        # Penalty for level 200+ sessions starting at 06:30 or 07:30 (slots 0, 1)
        if weights.early_penalty_late_level:
            for s in sessions:
                if s.course.level >= 200 and not s.online:
                    for r in allowed[s.id]:
                        if r == ONLINE_ROOM or r == FIELD_WORK_ROOM:
                            continue
                        for day in range(len(DAYS)):
                            for sl in range(2):  # slots 0 (06:30) and 1 (07:30)
                                u = day * SLOTS_PER_DAY + sl
                                for t in starts[s.id]:
                                    if t <= u < t + s.duration:
                                        terms.append(weights.early_penalty_late_level * z[(s.id, t, r)])

        if weights.room_idle:
            room_slot_idle = {}
            for r in room_capacity:
                if r in (ONLINE_ROOM, FIELD_WORK_ROOM):
                    continue
                for day in range(len(DAYS)):
                    if day == len(DAYS) - 1:
                        continue  # skip Saturday
                    base = day * SLOTS_PER_DAY
                    for sl in range(2, SLOTS_PER_DAY):
                        u = base + sl
                        occ_vars = []
                        for s in sessions:
                            if r not in allowed[s.id]:
                                continue
                            for t in starts[s.id]:
                                if t <= u < t + s.duration:
                                    occ_vars.append(z[(s.id, t, r)])
                        if not occ_vars:
                            continue
                        idle = model.NewBoolVar(f"idle_{r}_{u}")
                        model.Add(idle + sum(occ_vars) == 1)
                        room_slot_idle[(r, u)] = idle
            for (r, u), idle in room_slot_idle.items():
                terms.append(weights.room_idle * idle)

        def add_gap_terms(keys, key_filter, weight, tag):
            for key in keys:
                key_sessions = [s for s in sessions if key_filter(s, key)]
                for day in range(len(DAYS)):
                    base = day * SLOTS_PER_DAY
                    occ = []
                    for sl in range(SLOTS_PER_DAY):
                        u = base + sl
                        expr = sum(x for s in key_sessions for x in covering(s, u))
                        b = model.NewBoolVar(f"occ_{tag}_{key}_{u}")
                        model.Add(b == expr)
                        occ.append(b)
                    left = []
                    right = [None] * SLOTS_PER_DAY
                    for sl in range(SLOTS_PER_DAY):
                        a = model.NewBoolVar(f"left_{tag}_{key}_{base + sl}")
                        if sl == 0:
                            model.Add(a >= occ[sl])
                            model.Add(a <= occ[sl])
                        else:
                            model.Add(a >= occ[sl])
                            model.Add(a >= left[sl - 1])
                            model.Add(a <= left[sl - 1] + occ[sl])
                        left.append(a)
                    for sl in reversed(range(SLOTS_PER_DAY)):
                        b_ = model.NewBoolVar(f"right_{tag}_{key}_{base + sl}")
                        if sl == SLOTS_PER_DAY - 1:
                            model.Add(b_ >= occ[sl])
                            model.Add(b_ <= occ[sl])
                        else:
                            model.Add(b_ >= occ[sl])
                            model.Add(b_ >= right[sl + 1])
                            model.Add(b_ <= right[sl + 1] + occ[sl])
                        right[sl] = b_
                    for sl in range(SLOTS_PER_DAY):
                        g = model.NewBoolVar(f"gap_{tag}_{key}_{base + sl}")
                        model.Add(g <= 1 - occ[sl])
                        model.Add(g <= left[sl])
                        model.Add(g <= right[sl])
                        model.Add(g >= left[sl] + right[sl] + (1 - occ[sl]) - 2)
                        terms.append(weight * g)

        if not problem.get("skip_gaps"):
            add_gap_terms(sections, lambda s, sec: sec in s.sections, weights.cohort_gap, "sec")
            add_gap_terms(lecturers, lambda s, lec: s.course.lecturer == lec, weights.lecturer_gap, "lec")

        model.Minimize(sum(terms))

    if hints:
        for a in hints:
            key = (a.session.id, a.slot, a.room)
            if key in z and not fix_hinted:
                model.AddHint(z[key], 1)

    solver = cp_model.CpSolver()
    solver.parameters.max_time_in_seconds = time_limit
    solver.parameters.num_workers = num_workers if num_workers is not None else 8
    if seed is not None:
        solver.parameters.random_seed = seed
    if feasibility_jump:
        solver.parameters.use_feasibility_jump = True
    if log_search_progress:
        solver.parameters.log_search_progress = True

    callback = None
    if progress_cb is not None or solution_path is not None:
        class _SolutionCb(cp_model.CpSolverSolutionCallback):
            def __init__(self):
                super().__init__()
                self._cb = progress_cb
                self._path = solution_path

            def _write(self, status_label):
                data = {"status": status_label, "objective": self.ObjectiveValue(),
                        "sessions": {a.session.id: [a.slot, a.room] for a in self._snapshot()}}
                with open(self._path, "w", encoding="utf-8") as fh:
                    json.dump(data, fh)

            def _snapshot(self):
                out = []
                for s in sessions:
                    for t in starts[s.id]:
                        for r in allowed[s.id]:
                            if self.Value(z[(s.id, t, r)]) == 1:
                                out.append(Assignment(s, t, r))
                return out

            def OnSolutionCallback(self):
                if self._path is not None:
                    self._write("FEASIBLE")
                if self.WallTime() >= time_limit:
                    self.StopSearch()
                if self._cb is not None:
                    self._cb({
                        "objective": self.ObjectiveValue(),
                        "bound": self.BestObjectiveBound(),
                        "conflicts": self.NumConflicts(),
                        "elapsed": self.WallTime(),
                    })
        callback = _SolutionCb()

    if callback is not None:
        status = solver.solve(model, solution_callback=callback)
    else:
        status = solver.Solve(model)
    if status not in (cp_model.OPTIMAL, cp_model.FEASIBLE):
        if solver.StatusName(status) == "UNKNOWN":
            return SolveResult("NO SOLUTION (time limit)", float("inf"), [], {})
        return SolveResult("INFEASIBLE", float("inf"), [], {})

    assignments = []
    for s in sessions:
        for t in starts[s.id]:
            for r in allowed[s.id]:
                if solver.Value(z[(s.id, t, r)]) == 1:
                    assignments.append(Assignment(s, t, r))

    if solution_path is not None:
        label = "OPTIMAL" if status == cp_model.OPTIMAL else "FEASIBLE"
        data = {"status": label, "objective": solver.ObjectiveValue(),
                "sessions": {a.session.id: [a.slot, a.room] for a in assignments}}
        with open(solution_path, "w", encoding="utf-8") as fh:
            json.dump(data, fh)

    checks = _verify(assignments, problem)
    label = "OPTIMAL" if status == cp_model.OPTIMAL else "FEASIBLE"
    return SolveResult(label, solver.ObjectiveValue(), assignments, checks)


def load_solution_json(path, sessions):
    """Rebuild Assignment objects from a solution JSON file written by solve().
    Returns (status, objective, assignments) or (None, None, []) if absent."""
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        return None, None, []
    by_id = {s.id: s for s in sessions}
    assignments = []
    for sid, (slot, room) in data.get("sessions", {}).items():
        s = by_id.get(sid)
        if s is not None:
            assignments.append(Assignment(s, slot, room))
    return data.get("status"), data.get("objective"), assignments


def _verify(assignments, problem):
    sections = problem["sections"]
    lecturers = problem["lecturers"]
    room_capacity = {r.name: r.capacity for r in problem["rooms"]}
    issues = {"section": [], "lecturer": [], "room": [], "capacity": [], "same_day_course": []}

    def slots_covered(a):
        return range(a.slot, a.slot + a.session.duration)

    for sec in sections:
        seen = {}
        for a in assignments:
            if sec in a.session.sections:
                for u in slots_covered(a):
                    if u in seen:
                        issues["section"].append((sec, u, seen[u], a.session.id))
                    seen[u] = a.session.id

    for lec in lecturers:
        seen = {}
        for a in assignments:
            if a.session.course.lecturer == lec:
                for u in slots_covered(a):
                    if u in seen:
                        issues["lecturer"].append((lec, u, seen[u], a.session.id))
                    seen[u] = a.session.id

    for r, cap in room_capacity.items():
        seen = {}
        for a in assignments:
            if a.room != r:
                continue
            for u in slots_covered(a):
                if u in seen:
                    issues["room"].append((r, u, seen[u], a.session.id))
                seen[u] = a.session.id
                if cap < a.session.size:
                    real_size = sum(problem["cohorts"][s].size for s in a.session.sections if s in problem["cohorts"])
                    if real_size > cap:
                        issues["capacity"].append((r, a.session.id, real_size, cap))

    for sec in sections:
        by_course_day = {}
        for a in assignments:
            if sec in a.session.sections:
                d = day_index_of(a.slot)
                key = (a.session.course.code, d)
                by_course_day.setdefault(key, []).append(a.session.id)
        for (code, day), sids in by_course_day.items():
            if len(sids) > 1:
                issues["same_day_course"].append((sec, code, day, sids))

    # Paired sessions (split A/B) must both be physical or both be ONLINE
    split_groups = {}
    for a in assignments:
        sg = getattr(a.session, 'split_group', None)
        if sg:
            split_groups.setdefault(sg, []).append(a)

    for sg, paired in split_groups.items():
        if len(paired) != 2:
            continue
        a1, a2 = paired[0], paired[1]
        online1 = a1.room == ONLINE_ROOM
        online2 = a2.room == ONLINE_ROOM
        if online1 != online2:
            issues.setdefault("paired_session", []).append(
                (sg, a1.session.id, a2.session.id, "mixed physical/online")
            )

    return {k: len(v) for k, v in issues.items()}


def repair_assignments(problem, assignments):
    """Greedily relocate sessions that violate HARD constraints (section / room)
    to a free (slot, room) pair so the result is hard-conflict-free. Lecturer
    overlaps are treated as hard only when soft_lecturer is not set, otherwise
    they are left alone (they are allowed by design). Respects fixed_slot pins.
    Mutates the Assignment objects in place; returns (assignments, fixed_count)."""
    rooms = problem["rooms"]
    soft_lecturer = problem.get("soft_lecturer")

    def slots_of(a):
        return range(a.slot, a.slot + a.session.duration)

    room_occ = {}
    sec_occ = {}
    lec_occ = {}

    def add(a):
        for u in slots_of(a):
            if not _is_no_room(a.room):
                room_occ.setdefault((a.room, u), set()).add(a.session.id)
            for sec in a.session.sections:
                sec_occ.setdefault((sec, u), set()).add(a.session.id)
            lec_occ.setdefault((a.session.course.lecturer, u), set()).add(a.session.id)

    def remove(a):
        for u in slots_of(a):
            if not _is_no_room(a.room):
                room_occ.get((a.room, u), set()).discard(a.session.id)
            for sec in a.session.sections:
                sec_occ.get((sec, u), set()).discard(a.session.id)
            lec_occ.get((a.session.course.lecturer, u), set()).discard(a.session.id)

    def hard_conflicts(a):
        for u in slots_of(a):
            if not _is_no_room(a.room) and len(room_occ.get((a.room, u), ())) > 1:
                return True
            if not problem.get("soft_sections"):
                for sec in a.session.sections:
                    if len(sec_occ.get((sec, u), ())) > 1:
                        return True
        return False

    def lec_conflicts(a):
        if soft_lecturer:
            return False
        return any(len(lec_occ.get((a.session.course.lecturer, u), ())) > 1
                   for u in slots_of(a))

    for a in assignments:
        add(a)

    # Track sessions originally in a real classroom — they must not be
    # relocated back to ONLINE/Field, which would undo the fill_online_rooms
    # conversion and create idle room slots.
    physical_hosts = {
        a.session.id for a in assignments if not _is_no_room(a.room)
    }

    fixed = {a.session.id for a in assignments
             if getattr(a.session, "fixed_slot", None) is not None}

    fixed_count = 0
    for _ in range(8):
        to_fix = [a for a in assignments
                  if a.session.id not in fixed and (hard_conflicts(a) or lec_conflicts(a))]
        if not to_fix:
            break
        moved = 0
        for a in to_fix:
            if not (hard_conflicts(a) or lec_conflicts(a)):
                continue
            s = a.session
            remove(a)
            found = None
            candidate_rooms = _allowed_rooms(s, rooms)
            # Sessions that were already in a real room must not be relocated
            # back to ONLINE/Field — that would undo fill_online_rooms work and
            # leave idle room slots that could have been used.
            if s.id in physical_hosts and len(candidate_rooms) > 1:
                candidate_rooms = [r for r in candidate_rooms if not _is_no_room(r)] or candidate_rooms
            for t, r in [(t, r) for t in _allowed_starts(s) for r in candidate_rooms]:
                ok = True
                for u in range(t, t + s.duration):
                    if not _is_no_room(r) and room_occ.get((r, u)):
                        ok = False
                        break
                    if any(sec_occ.get((sec, u)) for sec in s.sections):
                        ok = False
                        break
                    if not soft_lecturer and lec_occ.get((s.course.lecturer, u)):
                        ok = False
                        break
                if ok:
                    found = (t, r)
                    break
            if found:
                a.slot, a.room = found
                moved += 1
            add(a)
        fixed_count += moved
        if moved == 0:
            break

    # Last resort: an online-eligible session that still has hard conflicts is
    # sent back to the ONLINE venue at a time when its sections/lecturer are
    # free. This resolves room + lecturer overlaps without needing a real room;
    # it only touches sessions that could not be relocated to any free cell.
    online_day_used = set()
    for b in assignments:
        for sec in b.session.sections:
            online_day_used.add((sec, b.session.course.code, day_index_of(b.slot)))
    for a in assignments:
        if not (hard_conflicts(a) or lec_conflicts(a)):
            continue
        s = a.session
        if not s.online:
            continue
        current_day = day_index_of(a.slot)
        remove(a)
        found = None
        for t in _allowed_starts(s):
            d = day_index_of(t)
            if d != current_day and any((sec, s.course.code, d) in online_day_used
                                        for sec in s.sections):
                continue
            ok = True
            for u in range(t, t + s.duration):
                if any(sec_occ.get((sec, u)) for sec in s.sections):
                    ok = False
                    break
                if not soft_lecturer and lec_occ.get((s.course.lecturer, u)):
                    ok = False
                    break
            if ok:
                found = t
                break
        if found is not None:
            a.slot, a.room = found, ONLINE_ROOM
            fixed_count += 1
        add(a)
    return assignments, fixed_count
