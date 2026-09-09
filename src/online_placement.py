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


def place_courses_sequential(problem, assignments, daily_cap=None):
    """
    Sequential greedy placement after the consistency passes.

    Fill unit = an A/B split pair (both members currently ONLINE) or a single
    online session (no split partner, or its partner is already physical). A
    unit is placed all-at-once or not at all, so a split A/B pair is never left
    mixed. Courses may end up partially in person / partially ONLINE - that is
    allowed; only split A/B pairs stay uniform.

    When daily_cap is given, a session is only placed on a day that keeps every
    one of its sections at or below the cap (field work is excluded from the
    count) - the same rule the student-load spread pass enforces.

    Most-constrained-first ordering; repeated rounds until no unit fits.
    Returns (placed_sessions, new_assignments).
    """
    from .solver import Assignment, ONLINE_ROOM, FIELD_WORK_ROOM

    room_occ, sec_occ, lec_occ = _build_full_occupancy(assignments)
    rooms = problem["rooms"]
    room_cap = {r.name: r.capacity for r in rooms}

    def day_count_excluding(to_remove_ids):
        counts = defaultdict(int)
        for a in assignments:
            if a.session.field_work or a.room == FIELD_WORK_ROOM:
                continue
            if a.session.id in to_remove_ids:
                continue
            d = day_index_of(a.slot)
            for sec in a.session.sections:
                counts[(sec, d)] += 1
        return counts

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
            day_count = day_count_excluding(to_remove_ids)
            for a in assignments:
                if a.session.id in to_remove_ids:
                    continue
                d = day_index_of(a.slot)
                for sec in a.session.sections:
                    daily[(sec, a.session.course.code)].add(d)

            sessions = [a.session for a in unit]
            sid_secs = {s.id: frozenset(s.sections) for s in sessions}
            session_cells = {}
            ok = True
            for s in sessions:
                cells = []
                for t in _allowed_starts(s):
                    day = day_index_of(t)
                    if any(day in daily[(sec, s.course.code)] for sec in s.sections):
                        continue
                    if daily_cap is not None and any(
                        day_count[(sec, day)] + 1 > daily_cap
                        for sec in s.sections
                    ):
                        continue
                    for r in _allowed_rooms(s, rooms):
                        if _is_no_room(r):
                            continue
                        if _is_cell_free(s, t, r, room_occ, sec_occ, lec_occ):
                            cells.append((t, r))
                if not cells:
                    ok = False
                    break
                # Tightest-fit first: a session that fits an 80-cap room takes
                # it before a 120-seat hall, so the largest rooms stay free for
                # the largest cohorts. No placement is lost - the backtracking
                # below still falls back to any bigger room if nothing smaller
                # is free.
                cells.sort(key=lambda c: (room_cap[c[1]], c[0]))
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
                    if daily_cap is not None:
                        day = day_index_of(t)
                        for sec in s.sections:
                            c = day_count[(sec, day)]
                            for cs_id, (cs_t, _cs_r) in chosen.items():
                                if day_index_of(cs_t) == day and sec in sid_secs[cs_id]:
                                    c += 1
                            if c + 1 > daily_cap:
                                conflict = True
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


def place_by_size_tiers(problem, assignments, big_tier=96, max_rounds=4, max_attempts=5000):
    """Room-fit reallocation so the largest rooms host the largest cohorts.

    Two phases, both preserving section/lecturer/room exclusivity, the
    same-course-per-day rule, the daily session cap (settings
    daily_max_sessions) and A/B pair uniformity:

    Phase 1 - tidy: physical classes sized <= big_tier sitting in a 120-seat
    hall are relocated into the smallest fitting room with a compliant free
    cell (SR 4 for <=40, an 80-cap room for mid-sized).

    Phase 2 - seat the biggest ONLINE units into hall cells: a big online unit
    (A/B pair or single) takes a compliant hall cell; if that cell is held by a
    smaller physical class, the class is chain-swapped out to a fitting room
    first, so utilisation is not lost. Units are seated all-or-nothing and any
    failed attempt is rolled back.

    Returns counters dict. Mutates Assignment objects in place.
    """
    from .slots import N_SLOTS, day_index_of

    rooms = problem["rooms"]
    overrides = problem.get("overrides") or {}
    cap = int(overrides.get("daily_max_sessions") or 3)
    if cap <= 0:
        cap = 3
    rcap = {r.name: r.capacity for r in rooms}
    hall_rooms = sorted(r.name for r in rooms if r.capacity >= 120)
    non_hall = sorted(r.name for r in rooms if r.capacity < 120)

    def refresh():
        return _build_full_occupancy(assignments)

    room_occ, sec_occ, lec_occ = refresh()

    def cell_free(s, t, r):
        return _is_cell_free(s, t, r, room_occ, sec_occ, lec_occ)

    def is_field(a):
        return a.session.field_work or a.room == FIELD_WORK_ROOM

    def day_ok(s, d, exclude):
        for sec in s.sections:
            n = 0
            for b in assignments:
                if b.session.id in exclude or is_field(b):
                    continue
                if day_index_of(b.slot) != d or sec not in b.session.sections:
                    continue
                if b.session.course.code == s.course.code:
                    return False
                n += 1
            if n >= cap:
                return False
        return True

    def candidate_cells(s, rooms_sel):
        allowed = _allowed_rooms(s, rooms)
        out = []
        for t in _allowed_starts(s):
            d = day_index_of(t)
            if not day_ok(s, d, {s.id}):
                continue
            for r in rooms_sel:
                if r not in allowed or _is_no_room(r):
                    continue
                if cell_free(s, t, r):
                    out.append((t, r))
        return out

    def sole_occupant(r, t, dur):
        ids = set()
        arr = room_occ.get(r)
        if not arr:
            return None
        for u in range(t, t + dur):
            v = arr[u]
            if v is not None:
                ids.add(v)
        if len(ids) != 1:
            return None
        b = next((a for a in assignments if a.session.id == next(iter(ids))), None)
        if b is None or b.room != r:
            return None
        bs = b.session
        if not (b.slot <= t and b.slot + bs.duration >= t + dur):
            return None
        return b

    def relocate(b, evicted_here):
        prev = (b.slot, b.room)
        cands = candidate_cells(b.session, non_hall)
        if not cands:
            return False
        cands.sort(key=lambda it: (rcap[it[1]], it[0]))
        b.slot, b.room = cands[0]
        evicted_here.append((b, prev))
        return True

    attempts = 0
    evicted = []

    # Phase 1: tidy smaller physical classes out of the halls.
    def phase1(evicted):
        nonlocal attempts, room_occ, sec_occ, lec_occ
        changed = False
        for a in list(assignments):
            if attempts >= max_attempts:
                break
            if is_field(a) or a.session.field_work or a.room not in hall_rooms:
                continue
            if a.session.size > big_tier:
                continue
            cands = candidate_cells(a.session, non_hall)
            if not cands:
                continue
            cands.sort(key=lambda it: (rcap[it[1]], it[0]))
            prev = (a.slot, a.room)
            a.slot, a.room = cands[0]
            evicted.append((a, prev))
            changed = True
            attempts += 1
            room_occ, sec_occ, lec_occ = refresh()
        return changed

    # Phase 2: seat the biggest ONLINE units into the halls.
    online_units = {}
    for a in assignments:
        if a.room != ONLINE_ROOM or a.session.field_work:
            continue
        if a.session.size < big_tier:
            continue
        key = a.session.split_group or ("sid", a.session.id)
        online_units.setdefault(key, []).append(a)

    seated = []
    tried = set()

    def try_seat(a, evicted_here):
        nonlocal attempts, room_occ, sec_occ, lec_occ
        allowed = _allowed_rooms(a.session, rooms)
        for t in _allowed_starts(a.session):
            d = day_index_of(t)
            if not day_ok(a.session, d, {a.session.id}):
                continue
            for r in hall_rooms:
                if r not in allowed or _is_no_room(r):
                    continue
                attempts += 1
                if attempts > max_attempts:
                    return False
                if cell_free(a.session, t, r):
                    a.slot, a.room = t, r
                    room_occ, sec_occ, lec_occ = refresh()
                    return True
                b = sole_occupant(r, t, a.session.duration)
                if b is None or is_field(b) or b.session.size > big_tier or b.session.online:
                    continue
                if relocate(b, evicted_here):
                    a.slot, a.room = t, r
                    room_occ, sec_occ, lec_occ = refresh()
                    return True
        return False

    def phase2(pos_seated, pos_tried):
        nonlocal attempts, room_occ, sec_occ, lec_occ
        changed = False
        for unit in sorted(online_units.values(), key=len, reverse=True):
            key = unit[0].session.split_group or ("sid", unit[0].session.id)
            if key in pos_tried:
                continue
            pos_tried.add(key)
            unit_prev = [(a, a.slot, a.room) for a in unit]
            evicted_here = []
            ok = all(try_seat(a, evicted_here) for a in unit)
            if ok:
                pos_seated.extend(a.session.id for a in unit)
                changed = True
            else:
                for a, sl, rr in unit_prev:
                    a.slot, a.room = sl, rr
                for b, (sl, rr) in evicted_here:
                    b.slot, b.room = sl, rr
                room_occ, sec_occ, lec_occ = refresh()
        return changed

    for _ in range(max_rounds):
        changed1 = phase1(evicted)
        changed2 = phase2(seated, tried)
        if not (changed1 or changed2) or attempts >= max_attempts:
            break

    leftover = sum(
        1 for a in assignments
        if a.room == ONLINE_ROOM and not a.session.field_work and a.session.size >= big_tier
    )
    still_in_hall = sum(
        1 for a in assignments
        if not is_field(a) and a.room in hall_rooms and a.session.size <= big_tier
    )
    return {
        "evicted": len(evicted),
        "seated_online": len(seated),
        "left_big_online": leftover,
        "small_mid_in_hall_after": still_in_hall,
        "attempts": attempts,
    }


def relocate_stuck_hall_residents(problem, assignments, big_tier=96, max_depth=3,
                                  max_rounds=6, max_attempts=1500000):
    """Chain-relocate physical classes stuck in 120-seat halls.

    The size-tier pass only moves a class that can go DIRECTLY to a free
    smaller cell. A class can be stuck though - every fitting smaller cell is
    blocked by one other class which itself has a free cell somewhere. This
    pass finds those chains:

        S (in hall) -> cell of X -> X -> X's free cell -> ...

    Depth-limited BFS over the relocation graph. Rules are identical to the
    rest of the pipeline: same-course-per-day and the daily cap (settings
    daily_max_sessions) are enforced for every hop, field work is untouched,
    ONLINE is untouched, and no class ever leaves a real room (utilization is
    preserved). Chains are committed as a unit - if the tail cannot be placed
    the search backtracks and nothing moves.

    Returns counters dict. Mutates Assignment objects in place.
    """
    from .slots import N_SLOTS, SLOTS_PER_DAY, day_index_of

    rooms = problem["rooms"]
    overrides = problem.get("overrides") or {}
    cap = int(overrides.get("daily_max_sessions") or 3)
    if cap <= 0:
        cap = 3
    rcap = {r.name: r.capacity for r in rooms}
    hall_rooms = sorted(r.name for r in rooms if r.capacity >= 120)
    non_hall = sorted(r.name for r in rooms if r.capacity < 120)
    by_id = {a.session.id: a for a in assignments}

    def refresh():
        return _build_full_occupancy(assignments)

    room_occ, sec_occ, lec_occ = refresh()
    secday = {}

    def rebuild_secday():
        secday.clear()
        for a in assignments:
            if a.session.field_work or a.room == FIELD_WORK_ROOM:
                continue
            d = day_index_of(a.slot)
            for sec in a.session.sections:
                secday.setdefault((sec, d), set()).add(a.session.id)

    rebuild_secday()

    def is_field(a):
        return a.session.field_work or a.room == FIELD_WORK_ROOM

    def day_ok(sid, d, displaced):
        """Day d may host session sid given the ids that leave their days in
        this chain (displaced). Same-course-per-day plus the daily cap."""
        s = by_id[sid].session
        dep = displaced | {sid}
        for sec in s.sections:
            ids = secday.get((sec, d), set()) - dep
            if len(ids) >= cap:
                return False
            for i in ids:
                if by_id[i].session.course.code == s.course.code:
                    return False
        return True

    def stucks():
        out = []
        for a in assignments:
            if is_field(a) or a.room not in hall_rooms:
                continue
            if a.session.size > big_tier:
                continue
            allow = _allowed_rooms(a.session, rooms) or []
            target_rooms = [r for r in non_hall if r in allow and not _is_no_room(r)]
            direct = False
            for t in _allowed_starts(a.session):
                d = day_index_of(t)
                if not day_ok(a.session.id, d, set()):
                    continue
                for r in target_rooms:
                    if _is_cell_free(a.session, t, r, room_occ, sec_occ, lec_occ):
                        direct = True
                        break
                if direct:
                    break
            if not direct:
                out.append(a)
        return out

    def fits_rooms(sid):
        s = by_id[sid].session
        allow = _allowed_rooms(s, rooms) or []
        return [r for r in non_hall if r in allow and not _is_no_room(r)]

    attempts = 0
    moved = 0
    for _ in range(max_rounds):
        if attempts >= max_attempts:
            break
        targets = stucks()
        if not targets:
            break
        targets.sort(key=lambda a: (a.session.size, a.session.id))
        round_moved = 0
        for root in targets:
            if attempts >= max_attempts:
                break
            chain = None
            queue = [(root.session.id, ())]
            seen = {root.session.id}
            qi = 0
            budget_local = 0
            while qi < len(queue) and budget_local < 4000:
                sid, moves = queue[qi]
                qi += 1
                displaced = set(i for i, _, _ in moves)
                s = by_id[sid].session
                fr = fits_rooms(sid)
                if not fr:
                    continue
                for t in _allowed_starts(s):
                    d = day_index_of(t)
                    if not day_ok(sid, d, displaced):
                        continue
                    for r in fr:
                        budget_local += 1
                        attempts += 1
                        if attempts > max_attempts:
                            break
                        # Full check: room + section + lecturer all clear -> the
                        # chain can end here on an empty, compliant cell.
                        if _is_cell_free(s, t, r, room_occ, sec_occ, lec_occ):
                            chain = moves + ((sid, t, r),)
                            break
                        # Otherwise the block is taken: usable as a hop only if
                        # the room's sole occupant owns every section/lecturer
                        # slot in the block (so the cell is fully clear once X
                        # leaves in the chain).
                        block = None
                        block_ok = True
                        arr = room_occ.get(r, [None] * N_SLOTS)
                        for u in range(t, t + s.duration):
                            occ = arr[u]
                            if occ is None:
                                block_ok = False
                                break
                            if block is None:
                                block = occ
                            elif occ != block:
                                block_ok = False
                                break
                        if not block_ok:
                            continue
                        X = by_id.get(block)
                        if X is None or is_field(X) or X.session.online:
                            continue
                        owner_ok = True
                        for u in range(t, t + s.duration):
                            for sec in s.sections:
                                v = sec_occ.get(sec, [None] * N_SLOTS)[u]
                                if v is not None and v != X.session.id:
                                    owner_ok = False
                                    break
                            if not owner_ok:
                                break
                            lec = s.course.lecturer
                            if lec:
                                v = lec_occ.get(lec, [None] * N_SLOTS)[u]
                                if v is not None and v != X.session.id:
                                    owner_ok = False
                                    break
                        if not owner_ok:
                            continue
                        if block in displaced or block in seen:
                            continue
                        seen.add(block)
                        next_m = moves + ((sid, t, r),)
                        if len(next_m) <= max_depth:
                            queue.append((block, next_m))
                    if chain:
                        break
                if chain:
                    break
            if chain is None:
                continue
            for sid, t, r in reversed(chain):
                by_id[sid].slot, by_id[sid].room = t, r
            moved += len(chain)
            round_moved += len(chain)
            room_occ, sec_occ, lec_occ = refresh()
            rebuild_secday()
        if round_moved == 0:
            break

    leftover = len(stucks())
    return {
        "moved": moved,
        "left_leftover_hall": leftover,
        "attempts": attempts,
    }
