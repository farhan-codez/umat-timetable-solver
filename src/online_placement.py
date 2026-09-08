"""Place ONLINE sessions into free rooms.

- place_online_courses: reduced CP-SAT (as wired historically).
- place_courses_sequential: greedy per-unit fill (A/B split pair or single
  online session) used after the consistency passes in regen.py.
"""

from dataclasses import dataclass
from collections import defaultdict

from ortools.sat.python import cp_model

from .solver import (
    _allowed_rooms,
    _allowed_starts,
    ONLINE_ROOM,
    FIELD_WORK_ROOM,
    _is_no_room,
)
from .slots import N_SLOTS, SLOTS_PER_DAY, day_index_of, slot_in_day


def _build_fixed_occupancy(assignments):
    """Build room/section/lecturer occupancy arrays from non-ONLINE assignments."""
    room_occ = {}
    sec_occ = {}
    lec_occ = {}
    for a in assignments:
        if _is_no_room(a.room):
            continue
        s = a.session
        slots = range(a.slot, a.slot + s.duration)
        for sec in s.sections:
            arr = sec_occ.setdefault(sec, [None] * N_SLOTS)
            for u in slots:
                arr[u] = s.id
        arr = lec_occ.setdefault(s.course.lecturer, [None] * N_SLOTS)
        for u in slots:
            arr[u] = s.id
        arr = room_occ.setdefault(a.room, [None] * N_SLOTS)
        for u in slots:
            arr[u] = s.id
    return room_occ, sec_occ, lec_occ


def _build_full_occupancy(assignments):
    """
    Build room/section/lecturer occupancy from all non-ONLINE sessions.
    FIELD WORK sessions DO occupy section/lecturer (students away, lecturer busy)
    but do NOT occupy a room. This matches the full CP-SAT model constraints.
    """
    room_occ = {}
    sec_occ = {}
    lec_occ = {}
    for a in assignments:
        if a.room == ONLINE_ROOM:
            continue
        s = a.session
        slots = range(a.slot, a.slot + s.duration)
        if a.room != FIELD_WORK_ROOM:
            arr = room_occ.setdefault(a.room, [None] * N_SLOTS)
            for u in slots:
                arr[u] = s.id
        for sec in s.sections:
            arr = sec_occ.setdefault(sec, [None] * N_SLOTS)
            for u in slots:
                arr[u] = s.id
        lec = s.course.lecturer
        if lec:
            arr = lec_occ.setdefault(lec, [None] * N_SLOTS)
            for u in slots:
                arr[u] = s.id
    return room_occ, sec_occ, lec_occ


def _is_cell_free(s, t, r, room_occ, sec_occ, lec_occ):
    """Check if (t, r) is free for session s given fixed occupancy."""
    dur = s.duration
    for u in range(t, t + dur):
        if u >= N_SLOTS:
            return False
        if room_occ.get(r, [None] * N_SLOTS)[u] is not None:
            return False
        for sec in s.sections:
            if sec_occ.get(sec, [None] * N_SLOTS)[u] is not None:
                return False
        lec = s.course.lecturer
        if lec and lec_occ.get(lec, [None] * N_SLOTS)[u] is not None:
            return False
    return True


@dataclass
class PlacementResult:
    placed_courses: int
    placed_sessions: int
    new_assignments: list


def place_online_courses(problem, assignments, time_limit=30.0, seed=42):
    """
    Place fully-online courses into rooms using a reduced CP-SAT model.
    
    Returns updated assignments list with online sessions moved to rooms where possible.
    """
    # Split assignments: freeze physical + field work; collect online sessions
    # FIELD WORK goes to frozen so its section/lecturer occupancy is respected
    frozen = []
    online_sessions = []
    for a in assignments:
        if a.room == ONLINE_ROOM and a.session.online:
            online_sessions.append(a)
        else:
            frozen.append(a)
    
    if not online_sessions:
        return PlacementResult(0, 0, assignments)
    
    # Group online sessions by course
    course_sessions = defaultdict(list)
    for a in online_sessions:
        course_sessions[a.session.course.code].append(a)
    
    # Build fixed occupancy from frozen schedule (includes FIELD WORK for section/lecturer)
    room_occ, sec_occ, lec_occ = _build_fixed_occupancy(frozen)
    
    # Pre-filter allowed (t, r) cells for each online session
    rooms = problem["rooms"]
    allowed_cells = {}
    for a in online_sessions:
        s = a.session
        cells = []
        for t in _allowed_starts(s):
            for r in _allowed_rooms(s, rooms):
                if _is_no_room(r):
                    continue
                if _is_cell_free(s, t, r, room_occ, sec_occ, lec_occ):
                    cells.append((t, r))
        if cells:
            allowed_cells[s.id] = cells
    
    # Only keep courses where ALL sessions have at least one allowed cell
    # (no-mixed-courses rule: if one session can't be placed, whole course stays online)
    placeable_courses = {}
    for code, sess_list in course_sessions.items():
        if all(a.session.id in allowed_cells for a in sess_list):
            placeable_courses[code] = sess_list
    
    if not placeable_courses:
        return PlacementResult(0, 0, assignments)
    
    # Build reduced CP-SAT model
    model = cp_model.CpModel()
    
    # Per-session binary: 1 = this session placed physically
    # (Course-level all-or-nothing is enforced by consistency pass afterwards)
    session_vars = {}
    for code, sess_list in placeable_courses.items():
        for a in sess_list:
            s = a.session
            session_vars[s.id] = model.NewBoolVar(f"place_sess_{s.id}")
    
    # Per-session z vars over allowed cells
    z_vars = {}
    for code, sess_list in placeable_courses.items():
        for a in sess_list:
            s = a.session
            for t, r in allowed_cells[s.id]:
                z_vars[(s.id, t, r)] = model.NewBoolVar(f"z_{s.id}_{t}_{r}")
    
    # Each online session: sum(z) == session_var
    for s_id, sv in session_vars.items():
        model.Add(sum(z_vars[(s_id, t, r)] for t, r in allowed_cells[s_id]) == sv)
    
    # Build covering sets for efficient constraints
    room_slot_covering = defaultdict(list)
    sec_slot_covering = defaultdict(list)
    lec_slot_covering = defaultdict(list)
    course_sec_day_covering = defaultdict(list)
    
    for code, sess_list in placeable_courses.items():
        for a in sess_list:
            s = a.session
            for t, r in allowed_cells[s.id]:
                z = z_vars[(s.id, t, r)]
                for u in range(t, t + s.duration):
                    room_slot_covering[(r, u)].append(z)
                    for sec in s.sections:
                        sec_slot_covering[(sec, u)].append(z)
                    lec = s.course.lecturer
                    if lec:
                        lec_slot_covering[(lec, u)].append(z)
                for sec in s.sections:
                    day = day_index_of(t)
                    if day < 5:
                        course_sec_day_covering[(code, sec, day)].append(z)
    
    # Room exclusivity
    for key, covering in room_slot_covering.items():
        if len(covering) > 1:
            model.Add(sum(covering) <= 1)
    
    # Section exclusivity
    for key, covering in sec_slot_covering.items():
        if len(covering) > 1:
            model.Add(sum(covering) <= 1)
    
    # Lecturer exclusivity
    for key, covering in lec_slot_covering.items():
        if len(covering) > 1:
            model.Add(sum(covering) <= 1)
    
    # Same course, same section, once per day (Mon-Fri)
    for key, covering in course_sec_day_covering.items():
        if len(covering) > 1:
            model.Add(sum(covering) <= 1)
    
    # Objective: maximize number of physically placed sessions
    terms = []
    for s_id, sv in session_vars.items():
        terms.append(-100 * sv)  # reward for placing session physically (minimize negative = maximize)
    # Small soft preferences: evening penalty, room oversize
    for code, sess_list in placeable_courses.items():
        for a in sess_list:
            s = a.session
            for t, r in allowed_cells[s.id]:
                room = next((rr for rr in rooms if rr.name == r), None)
                if room:
                    need = max(s.size, s.course.min_capacity)
                    over = (1 if room.capacity > 80 else 0) if need <= 80 else (1 if room.capacity > 120 else 0)
                    if over:
                        terms.append(2 * over * z_vars[(s.id, t, r)])
                    occupies_evening = any(slot_in_day(u) >= 9 for u in range(t, t + s.duration))
                    if occupies_evening:
                        terms.append(1 * z_vars[(s.id, t, r)])
    
    model.Minimize(sum(terms))
    
    # Hints: current state (all online -> sv=0)
    for sv in session_vars.values():
        model.AddHint(sv, 0)
    
    solver = cp_model.CpSolver()
    solver.parameters.max_time_in_seconds = time_limit
    solver.parameters.num_workers = 1
    solver.parameters.random_seed = seed
    solver.parameters.log_search_progress = False
    
    status = solver.Solve(model)
    if status not in (cp_model.OPTIMAL, cp_model.FEASIBLE):
        return PlacementResult(0, 0, assignments)
    
    # Build new assignments
    new_assignments = list(frozen)
    placed_sessions = 0
    
    for s_id, sv in session_vars.items():
        is_placed = solver.Value(sv) == 1
        if is_placed:
            placed_sessions += 1
        # Find the original assignment to get the course
        orig_a = next(a for a in online_sessions if a.session.id == s_id)
        s = orig_a.session
        if is_placed and s_id in allowed_cells:
            for t, r in allowed_cells[s_id]:
                if solver.Value(z_vars[(s_id, t, r)]) == 1:
                    from .solver import Assignment
                    new_assignments.append(Assignment(s, t, r))
                    break
        else:
            new_assignments.append(orig_a)
    
    # Also add courses that weren't placeable (stay online)
    for code, sess_list in course_sessions.items():
        if code not in placeable_courses:
            for a in sess_list:
                new_assignments.append(a)
    
    return PlacementResult(len(session_vars) - sum(1 for v in session_vars.values() if solver.Value(v) == 0), placed_sessions, new_assignments)


def place_courses_sequential(problem, assignments):
    """
    Sequential greedy placement after the consistency passes.

    Fill unit = an A/B split pair (both members currently ONLINE) or a single
    online session (no split partner, or its partner is already physical). A
    unit is placed all-at-once or not at all, so a split A/B pair is never left
    mixed. Courses may end up partially in person / partially ONLINE - that is
    allowed; only split A/B pairs stay uniform.

    Most-constrained-first ordering; repeated rounds until no unit fits.
    Returns (placed_sessions, new_assignments).
    """
    from .solver import Assignment, ONLINE_ROOM, FIELD_WORK_ROOM

    room_occ, sec_occ, lec_occ = _build_full_occupancy(assignments)
    rooms = problem["rooms"]

    # Group currently-ONLINE assignments into fill units.
    online_units = defaultdict(list)  # key: split_group else session id
    for a in assignments:
        if a.room != ONLINE_ROOM:
            continue
        key = a.session.split_group or ("sid", a.session.id)
        online_units[key].append(a)

    if not online_units:
        return 0, assignments

    units = list(online_units.values())
    placed_sessions = 0

    while True:
        progress_this_round = False

        # Score units by total free cells (most constrained first).
        scored = []
        for unit in units:
            if any(a.room != ONLINE_ROOM for a in unit):
                continue  # already placed in an earlier round
            total_free = 0
            ok = True
            for a in unit:
                s = a.session
                free_for_s = 0
                for t in _allowed_starts(s):
                    for r in _allowed_rooms(s, rooms):
                        if _is_no_room(r):
                            continue
                        if _is_cell_free(s, t, r, room_occ, sec_occ, lec_occ):
                            free_for_s += 1
                if free_for_s == 0:
                    ok = False
                    break
                total_free += free_for_s
            if ok:
                scored.append((total_free, unit))

        if not scored:
            break

        scored.sort(key=lambda x: x[0])  # fewest free cells first

        for total_free, unit in scored:
            if any(a.room != ONLINE_ROOM for a in unit):
                continue

            to_remove_ids = {a.session.id for a in unit}

            # Days on which each (section, course) is already busy, excluding
            # this unit's own ONLINE sessions (they are removed on commit).
            daily = defaultdict(set)
            for a in assignments:
                if a.session.id in to_remove_ids:
                    continue
                d = day_index_of(a.slot)
                for sec in a.session.sections:
                    daily[(sec, a.session.course.code)].add(d)

            sessions = [a.session for a in unit]
            session_cells = {}
            ok = True
            for s in sessions:
                cells = []
                for t in _allowed_starts(s):
                    day = day_index_of(t)
                    if any(day in daily[(sec, s.course.code)] for sec in s.sections):
                        continue
                    for r in _allowed_rooms(s, rooms):
                        if _is_no_room(r):
                            continue
                        if _is_cell_free(s, t, r, room_occ, sec_occ, lec_occ):
                            cells.append((t, r))
                if not cells:
                    ok = False
                    break
                session_cells[s.id] = cells
            if not ok:
                continue

            # Order sessions within the unit by fewest options.
            ordered_sessions = sorted(sessions, key=lambda s: len(session_cells[s.id]))

            # Backtracking search over the unit's sessions.
            chosen = {}  # session_id -> (t, r)

            def backtrack(idx):
                if idx >= len(ordered_sessions):
                    return True
                s = ordered_sessions[idx]
                for t, r in session_cells[s.id]:
                    if not _is_cell_free(s, t, r, room_occ, sec_occ, lec_occ):
                        continue
                    conflict = False
                    for cs_id, (cs_t, cs_r) in chosen.items():
                        cs = next(ss for ss in ordered_sessions if ss.id == cs_id)
                        # Same room
                        if r == cs_r:
                            for u in range(t, t + s.duration):
                                if cs_t <= u < cs_t + cs.duration:
                                    conflict = True
                                    break
                        if conflict:
                            break
                        # Shared section
                        for sec in s.sections:
                            if sec in cs.sections:
                                for u in range(t, t + s.duration):
                                    if cs_t <= u < cs_t + cs.duration:
                                        conflict = True
                                        break
                                if conflict:
                                    break
                        if conflict:
                            break
                        # Same lecturer
                        lec = s.course.lecturer
                        if lec and lec == cs.course.lecturer:
                            for u in range(t, t + s.duration):
                                if cs_t <= u < cs_t + cs.duration:
                                    conflict = True
                                    break
                            if conflict:
                                break
                    if conflict:
                        continue
                    chosen[s.id] = (t, r)
                    if backtrack(idx + 1):
                        return True
                    del chosen[s.id]
                return False

            if not backtrack(0):
                continue

            # Commit the unit's placements.
            placed_session_ids = set(chosen.keys())
            new_assignments = []
            for a in assignments:
                if a.session.id in placed_session_ids:
                    continue
                new_assignments.append(a)
            for s_id, (t, r) in chosen.items():
                s = next(ss for ss in ordered_sessions if ss.id == s_id)
                new_assignments.append(Assignment(s, t, r))
                slots = range(t, t + s.duration)
                arr = room_occ.setdefault(r, [None] * N_SLOTS)
                for u in slots:
                    arr[u] = s.id
                for sec in s.sections:
                    arr = sec_occ.setdefault(sec, [None] * N_SLOTS)
                    for u in slots:
                        arr[u] = s.id
                lec = s.course.lecturer
                if lec:
                    arr = lec_occ.setdefault(lec, [None] * N_SLOTS)
                    for u in slots:
                        arr[u] = s.id
            assignments = new_assignments
            placed_sessions += len(ordered_sessions)
            progress_this_round = True

        if not progress_this_round:
            break

    return placed_sessions, assignments
