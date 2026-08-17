import time

from .slots import DAYS, N_SLOTS, SLOTS_PER_DAY
from .solver import FIELD_WORK_ROOM, ONLINE_ROOM, _allowed_rooms, _allowed_starts, _is_no_room, _tier

LECTURER_GAP_WEIGHT = 0.6
ROOM_GAP_WEIGHT = 1.5


def gap_score(occ):
    score = 0
    for day in range(len(DAYS)):
        base = day * SLOTS_PER_DAY
        in_day = [u for u in occ if base <= u < base + SLOTS_PER_DAY]
        n = len(in_day)
        if n >= 2:
            score += (max(in_day) - min(in_day) + 1) - n
    return score


def _delta(arr, old, new, sid):
    occ = [u for u, v in enumerate(arr) if v is not None]
    before = gap_score(occ)
    tmp = arr[:]
    for u in old:
        if tmp[u] == sid:
            tmp[u] = None
    for u in new:
        tmp[u] = sid
    after = gap_score([u for u, v in enumerate(tmp) if v is not None])
    return after - before


def compact(problem, assignments, time_budget=120.0, max_passes=60):
    sessions = problem["sessions"]
    room_cap = {r.name: r.capacity for r in problem["rooms"]}
    allowed = {s.id: _allowed_rooms(s, problem["rooms"]) for s in sessions}
    starts = {s.id: _allowed_starts(s) for s in sessions}
    need = {s.id: _tier(max(s.size, s.course.min_capacity)) for s in sessions}

    assign = {a.session.id: a for a in assignments}
    max_online_t3 = problem.get("max_online_tier3_hours") or 0

    sec_occ = {}
    lec_occ = {}
    room_occ = {}
    for a in assignments:
        s = a.session
        slots = range(a.slot, a.slot + s.duration)
        for sec in s.sections:
            arr = sec_occ.setdefault(sec, [None] * N_SLOTS)
            for u in slots:
                arr[u] = s.id
        arr = lec_occ.setdefault(s.course.lecturer, [None] * N_SLOTS)
        for u in slots:
            arr[u] = s.id
        if not _is_no_room(a.room):
            arr = room_occ.setdefault(a.room, [None] * N_SLOTS)
            for u in slots:
                arr[u] = s.id

    t3_online = sum(
        a.session.duration for a in assignments
        if not a.session.online and a.room == ONLINE_ROOM and need[a.session.id] == 3
    )

    deadline = time.time() + time_budget

    def pick_room(s, new):
        cands = []
        for r in allowed[s.id]:
            if _is_no_room(r):
                continue
            arr = room_occ[r]
            free = True
            for u in new:
                v = arr[u]
                if v is not None and v != s.id:
                    free = False
                    break
            if free:
                cands.append(r)
        if cands:
            return min(cands, key=lambda r: (abs(_tier(room_cap[r]) - need[s.id]), room_cap[r]))
        if ONLINE_ROOM in allowed[s.id]:
            return ONLINE_ROOM
        if FIELD_WORK_ROOM in allowed[s.id]:
            return FIELD_WORK_ROOM
        return None

    moved = 0
    for _ in range(max_passes):
        if time.time() > deadline:
            break
        any_move = False
        for s in sessions:
            a = assign[s.id]
            dur = s.duration
            old = range(a.slot, a.slot + dur)
            lec = s.course.lecturer
            lec_arr = lec_occ[lec]
            best_t = None
            best_room = None
            best_delta = 0.0
            for t in starts[s.id]:
                if t == a.slot:
                    continue
                new = range(t, t + dur)
                ok = True
                for sec in s.sections:
                    arr = sec_occ[sec]
                    for u in new:
                        v = arr[u]
                        if v is not None and v != s.id:
                            ok = False
                            break
                    if not ok:
                        break
                if not ok:
                    continue
                for u in new:
                    v = lec_arr[u]
                    if v is not None and v != s.id:
                        ok = False
                        break
                if not ok:
                    continue
                rn = pick_room(s, new)
                if rn is None:
                    continue
                if not s.online and rn == ONLINE_ROOM:
                    delta_online = dur if a.room != ONLINE_ROOM else 0
                    if t3_online + delta_online > max_online_t3:
                        continue
                d = 0.0
                for sec in s.sections:
                    d += _delta(sec_occ[sec], old, new, s.id)
                d += LECTURER_GAP_WEIGHT * _delta(lec_arr, old, new, s.id)
                if not _is_no_room(a.room):
                    d += ROOM_GAP_WEIGHT * _delta(room_occ[a.room], old, [], s.id)
                if not _is_no_room(rn):
                    d += ROOM_GAP_WEIGHT * _delta(room_occ[rn], [], new, s.id)
                if d < best_delta:
                    best_delta = d
                    best_t = t
                    best_room = rn
            if best_t is not None:
                new = range(best_t, best_t + dur)
                for sec in s.sections:
                    arr = sec_occ[sec]
                    for u in old:
                        if arr[u] == s.id:
                            arr[u] = None
                    for u in new:
                        arr[u] = s.id
                for u in old:
                    if lec_arr[u] == s.id:
                        lec_arr[u] = None
                for u in new:
                    lec_arr[u] = s.id
                if not _is_no_room(a.room):
                    arr = room_occ[a.room]
                    for u in old:
                        if arr[u] == s.id:
                            arr[u] = None
                if not s.online and a.room == ONLINE_ROOM:
                    t3_online -= dur
                if not _is_no_room(best_room):
                    arr = room_occ[best_room]
                    for u in new:
                        arr[u] = s.id
                if not s.online and best_room == ONLINE_ROOM:
                    t3_online += dur
                a.slot = best_t
                a.room = best_room
                moved += 1
                any_move = True
        if not any_move:
            break
    return moved


def fill_online_rooms(problem, assignments, max_gap_cost=None, time_budget=60):
    """Move online sessions that currently sit in the ONLINE venue into a real
    classroom whenever a free (room, slot) exists, filling otherwise-idle rooms.

    Rule: a class is kept online only if NO classroom is free for it - an online
    session is converted in person whenever any room is empty at a compatible
    slot. `max_gap_cost` (None = always accept) is an optional upper bound on the
    idle slots a move may add for the affected cohort/lecturer; the conversion
    still prefers the slot that adds the fewest gaps (its current slot = delta 0)."""
    sessions = problem["sessions"]
    room_cap = {r.name: r.capacity for r in problem["rooms"]}
    allowed = {s.id: _allowed_rooms(s, problem["rooms"]) for s in sessions}
    starts = {s.id: _allowed_starts(s) for s in sessions}
    need = {s.id: _tier(max(s.size, s.course.min_capacity)) for s in sessions}

    assign = {a.session.id: a for a in assignments}

    sec_occ = {}
    lec_occ = {}
    room_occ = {}
    for a in assignments:
        s = a.session
        slots = range(a.slot, a.slot + s.duration)
        for sec in s.sections:
            arr = sec_occ.setdefault(sec, [None] * N_SLOTS)
            for u in slots:
                arr[u] = s.id
        arr = lec_occ.setdefault(s.course.lecturer, [None] * N_SLOTS)
        for u in slots:
            arr[u] = s.id
        if not _is_no_room(a.room):
            arr = room_occ.setdefault(a.room, [None] * N_SLOTS)
            for u in slots:
                arr[u] = s.id

    deadline = time.time() + time_budget
    placed = 0
    for _ in range(30):
        if time.time() > deadline:
            break
        progress = False
        for s in sessions:
            a = assign.get(s.id)
            if a is None or not s.online or a.room != ONLINE_ROOM:
                continue
            dur = s.duration
            old = range(a.slot, a.slot + dur)
            lec = s.course.lecturer
            lec_arr = lec_occ[lec]
            best_t = None
            best_room = None
            best_delta = float("inf")
            for t in starts[s.id]:
                new = range(t, t + dur)
                ok = True
                for sec in s.sections:
                    arr = sec_occ[sec]
                    for u in new:
                        v = arr[u]
                        if v is not None and v != s.id:
                            ok = False
                            break
                    if not ok:
                        break
                if not ok:
                    continue
                for u in new:
                    v = lec_arr[u]
                    if v is not None and v != s.id:
                        ok = False
                        break
                if not ok:
                    continue
                rn = None
                for r in allowed[s.id]:
                    if r == ONLINE_ROOM:
                        continue
                    arr = room_occ[r]
                    free = True
                    for u in new:
                        v = arr[u]
                        if v is not None and v != s.id:
                            free = False
                            break
                    if free:
                        rn = r
                        break
                if rn is None:
                    continue
                d = 0.0
                for sec in s.sections:
                    d += _delta(sec_occ[sec], old, new, s.id)
                d += LECTURER_GAP_WEIGHT * _delta(lec_arr, old, new, s.id)
                if d < best_delta:
                    best_delta = d
                    best_t = t
                    best_room = rn
            if best_t is not None and (max_gap_cost is None or best_delta <= max_gap_cost):
                new = range(best_t, best_t + dur)
                for sec in s.sections:
                    arr = sec_occ[sec]
                    for u in old:
                        if arr[u] == s.id:
                            arr[u] = None
                    for u in new:
                        arr[u] = s.id
                for u in old:
                    if lec_arr[u] == s.id:
                        lec_arr[u] = None
                for u in new:
                    lec_arr[u] = s.id
                if a.room != ONLINE_ROOM:
                    arr = room_occ[a.room]
                    for u in old:
                        if arr[u] == s.id:
                            arr[u] = None
                arr = room_occ[best_room]
                for u in new:
                    arr[u] = s.id
                a.slot = best_t
                a.room = best_room
                placed += 1
                progress = True
        if not progress:
            break
    return placed
