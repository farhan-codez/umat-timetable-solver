import time

from .slots import N_SLOTS, SLOTS_PER_DAY, day_index_of
from .pack import Packer
from .solver import _is_no_room


def _loads(pk, rooms):
    loads = {}
    for room in rooms:
        arr = pk.room_occ.get(room)
        for day in range(5):
            base = day * SLOTS_PER_DAY
            if arr is None:
                n = 0
            else:
                n = sum(1 for u in range(base, base + SLOTS_PER_DAY) if arr[u] is not None)
            loads[(room, day)] = n
    return loads


def _remove(pk, s, a):
    slots = range(a.slot, a.slot + s.duration)
    for sec in s.sections:
        arr = pk.sec_occ[sec]
        for u in slots:
            if arr[u] == s.id:
                arr[u] = None
    arr = pk.lec_occ[s.course.lecturer]
    for u in slots:
        if arr[u] == s.id:
            arr[u] = None
    if not _is_no_room(a.room):
        arr = pk.room_occ[a.room]
        for u in slots:
            if arr[u] == s.id:
                arr[u] = None


def _edge_status(pk, s, a, src):
    """1 if removing this session leaves the source room-day contiguous
    (session touches the left or right edge of the block), else 0."""
    arr = pk.room_occ[a.room]
    base = day_index_of(a.slot) * SLOTS_PER_DAY
    occ = [u for u in range(base, base + SLOTS_PER_DAY) if arr[u] is not None]
    if len(occ) < 2:
        return 1
    first, last = occ[0], occ[-1]
    return 1 if (a.slot == first or a.slot + s.duration == last + 1) else 0


def _target_start(pk, s, room, day, base):
    """Earliest slot in this room-day that accepts s without creating an idle
    hole: butt against the existing block edge (left or right), or start the
    block at the day boundary when the room-day is empty. Returns None if the
    room-day is full or no hole-neutral placement fits."""
    dur = s.duration
    arr = pk.room_occ[room]
    occ = [u for u in range(base, base + SLOTS_PER_DAY) if arr[u] is not None]
    if not occ:
        return base if pk._free(s, base, room) else None
    first, last = occ[0], occ[-1]
    if first - dur >= base and pk._free(s, first - dur, room):
        return first - dur
    if last + dur < base + SLOTS_PER_DAY and pk._free(s, last + 1, room):
        return last + 1
    return None


def rebalance(problem, assignments, min_load=8, time_budget=180):
    """Spread sessions so no physical room-day sits idle while others are full.
    Greedily fills the lowest-loaded room-days, moving only donor sessions whose
    removal keeps the source block contiguous (edge sessions) into placements
    that keep the target block contiguous. Never creates new idle holes.
    Returns the number of sessions moved."""
    pk = Packer(problem, assignments)
    for r in problem["rooms"]:
        pk.room_occ.setdefault(r.name, [None] * N_SLOTS)
    sessions = [s for s in problem["sessions"] if not s.field_work]
    physical = [s for s in sessions if not s.online]
    online = [s for s in sessions if s.online]
    rooms = [r.name for r in problem["rooms"]]
    deadline = time.time() + time_budget
    moved = 0

    for _ in range(120):
        if time.time() > deadline:
            break
        loads = _loads(pk, rooms)
        order = sorted(loads, key=lambda k: loads[k])
        progress = False
        for room, day in order:
            if loads[(room, day)] >= 12:
                continue
            base = day * SLOTS_PER_DAY
            best = None  # (key, session, start)
            for s in physical + online:
                a = pk.assign.get(s.id)
                if a is None or room not in pk.allowed[s.id]:
                    continue
                if s.duration > SLOTS_PER_DAY:
                    continue
                t = _target_start(pk, s, room, day, base)
                if t is None:
                    continue
                if not _is_no_room(a.room):
                    src = (a.room, day_index_of(a.slot))
                    src_load = loads[src]
                    if src_load - s.duration < min_load:
                        continue
                    if not _edge_status(pk, s, a, src):
                        continue
                key = (-t, s.duration, s.id)
                if best is None or key > best[0]:
                    best = (key, s, t)
            if best is not None:
                _, s, t = best
                a = pk.assign[s.id]
                if not _is_no_room(a.room):
                    loads[(a.room, day_index_of(a.slot))] -= s.duration
                _remove(pk, s, a)
                pk._place(s, t, room)
                loads[(room, day)] += s.duration
                moved += 1
                progress = True
        if not progress:
            break

    _close_holes(pk, rooms)
    return moved


def _close_holes(pk, rooms):
    """Within each room-day, slide sessions earlier into internal holes so blocks
    stay contiguous. Never moves sessions across rooms or days (loads unchanged)."""
    for _ in range(60):
        progress = False
        for room in rooms:
            arr = pk.room_occ[room]
            for day in range(5):
                base = day * SLOTS_PER_DAY
                occ = [u for u in range(base, base + SLOTS_PER_DAY) if arr[u] is not None]
                if len(occ) < 2:
                    continue
                first = occ[0]
                for u in range(first + 1, base + SLOTS_PER_DAY):
                    sid = arr[u]
                    if sid is None:
                        continue
                    a = pk.assign.get(sid)
                    if a is None or a.room != room:
                        continue
                    s = a.session
                    for t in range(first, u):
                        if t in pk.starts[s.id] and pk._free(s, t, room):
                            _remove(pk, s, a)
                            pk._place(s, t, room)
                            progress = True
                            break
        if not progress:
            break


def room_day_stats(assignments, rooms):
    """min/max/avg occupied slots per physical room-day (for reporting)."""
    occ = {}
    for a in assignments:
        if _is_no_room(a.room):
            continue
        day = day_index_of(a.slot)
        for u in range(a.slot, a.slot + a.session.duration):
            occ.setdefault((a.room, day), set()).add(u)
    vals = sorted(len(v) for v in occ.values())
    return vals
