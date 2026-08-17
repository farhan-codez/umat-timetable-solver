import time

from .slots import N_SLOTS, SLOTS_PER_DAY, day_index_of
from .solver import _allowed_rooms, _allowed_starts, _is_no_room

ROOM_W = 2.0
COHORT_W = 1.0
LEC_W = 0.5


def _day_gap(arr, day):
    base = day * SLOTS_PER_DAY
    first = last = None
    n = 0
    for u in range(base, base + SLOTS_PER_DAY):
        if arr[u] is not None:
            if first is None:
                first = u
            last = u
            n += 1
    if n < 2:
        return 0
    return (last - first + 1) - n


def _rearrange_delta(arr, removals, additions):
    """gap-score change of applying removals then additions, relative to the
    current array state. Pure: works on a copy, never mutates `arr`."""
    work = list(arr)
    days = set()
    for slots, _ in removals + additions:
        for u in slots:
            days.add(day_index_of(u))
    before = sum(_day_gap(work, d) for d in days)
    for slots, sid in removals:
        for u in slots:
            if work[u] == sid:
                work[u] = None
    for slots, sid in additions:
        for u in slots:
            if work[u] is None:
                work[u] = sid
    after = sum(_day_gap(work, d) for d in days)
    return after - before


class Packer:
    def __init__(self, problem, assignments, debug=False, allow_plateau=False):
        self.sessions = problem["sessions"]
        self.debug = debug
        self.allow_plateau = allow_plateau
        self.tie_eps = 0.001 if allow_plateau else 0.0
        self.allowed = {s.id: _allowed_rooms(s, problem["rooms"]) for s in self.sessions}
        self.starts = {s.id: _allowed_starts(s) for s in self.sessions}
        self.assign = {a.session.id: a for a in assignments}
        self.sec_occ = {}
        self.lec_occ = {}
        self.room_occ = {}
        for a in assignments:
            s = a.session
            slots = range(a.slot, a.slot + s.duration)
            for sec in s.sections:
                arr = self.sec_occ.setdefault(sec, [None] * N_SLOTS)
                for u in slots:
                    arr[u] = s.id
            arr = self.lec_occ.setdefault(s.course.lecturer, [None] * N_SLOTS)
            for u in slots:
                arr[u] = s.id
            if not _is_no_room(a.room):
                arr = self.room_occ.setdefault(a.room, [None] * N_SLOTS)
                for u in slots:
                    arr[u] = s.id
        self.holes = {}

    def _compute_holes(self):
        """room -> day -> set of empty slot indices inside the occupied range."""
        holes = {}
        for room, arr in self.room_occ.items():
            h = {}
            for day in range(5):
                base = day * SLOTS_PER_DAY
                occ = [u for u in range(base, base + SLOTS_PER_DAY) if arr[u] is not None]
                if len(occ) < 2:
                    continue
                hs = set()
                for u in range(occ[0] + 1, occ[-1]):
                    if arr[u] is None:
                        hs.add(u)
                if hs:
                    h[day] = hs
            if h:
                holes[room] = h
        return holes

    def _remove(self, s):
        a = self.assign[s.id]
        slots = range(a.slot, a.slot + s.duration)
        for sec in s.sections:
            arr = self.sec_occ[sec]
            for u in slots:
                if arr[u] == s.id:
                    arr[u] = None
        arr = self.lec_occ[s.course.lecturer]
        for u in slots:
            if arr[u] == s.id:
                arr[u] = None
        if not _is_no_room(a.room):
            arr = self.room_occ[a.room]
            for u in slots:
                if arr[u] == s.id:
                    arr[u] = None

    def _free(self, s, t, r):
        slots = range(t, t + s.duration)
        if not _is_no_room(r):
            arr = self.room_occ.get(r)
            if arr is None:
                return False
            if any(arr[u] is not None for u in slots):
                return False
        for sec in s.sections:
            if any(self.sec_occ[sec][u] is not None for u in slots):
                return False
        if any(self.lec_occ[s.course.lecturer][u] is not None for u in slots):
            return False
        return True

    def _place(self, s, t, r):
        a = self.assign[s.id]
        slots = range(t, t + s.duration)
        for sec in s.sections:
            arr = self.sec_occ[sec]
            for u in slots:
                arr[u] = s.id
        arr = self.lec_occ[s.course.lecturer]
        for u in slots:
            arr[u] = s.id
        if not _is_no_room(r):
            arr = self.room_occ[r]
            for u in slots:
                arr[u] = s.id
        a.slot = t
        a.room = r
        if self.debug:
            self._sanity()

    def _sanity(self):
        import sys
        import traceback
        bad = 0
        for a in self.assign.values():
            s = a.session
            slots = range(a.slot, a.slot + s.duration)
            for sec in s.sections:
                for u in slots:
                    if self.sec_occ[sec][u] != s.id:
                        print(f"DEBUG ARRAY-MISMATCH sec[{sec}] slot={u}: arr={self.sec_occ[sec][u]} assign={s.id} {s.course.code}", file=sys.stderr)
                        bad += 1
                        if bad > 3:
                            traceback.print_stack(file=sys.stderr)
                            raise SystemExit
            if not _is_no_room(a.room):
                for u in slots:
                    if self.room_occ[a.room][u] != s.id:
                        print(f"DEBUG ARRAY-MISMATCH room[{a.room}] slot={u}: arr={self.room_occ[a.room][u]} assign={s.id} {s.course.code}", file=sys.stderr)
                        bad += 1
                        if bad > 3:
                            traceback.print_stack(file=sys.stderr)
                            raise SystemExit

    def _score_relocate(self, s, t, r):
        a = self.assign[s.id]
        old = range(a.slot, a.slot + s.duration)
        new = range(t, t + s.duration)
        d = 0.0
        if not _is_no_room(a.room):
            d += ROOM_W * _rearrange_delta(self.room_occ[a.room], [(old, s.id)], [])
        if not _is_no_room(r):
            d += ROOM_W * _rearrange_delta(self.room_occ[r], [], [(new, s.id)])
        for sec in s.sections:
            d += COHORT_W * _rearrange_delta(self.sec_occ[sec], [(old, s.id)], [(new, s.id)])
        d += LEC_W * _rearrange_delta(self.lec_occ[s.course.lecturer], [(old, s.id)], [(new, s.id)])
        return d

    def relocate_pass(self, deadline=None):
        holes = self.holes
        moves = 0
        for s in self.sessions:
            a = self.assign[s.id]
            if getattr(s, "fixed_slot", None) is not None:
                continue
            day = day_index_of(a.slot)
            starts = self.starts[s.id]
            cands = set()
            if _is_no_room(a.room):
                for r in self.allowed[s.id]:
                    if _is_no_room(r):
                        continue
                    for hs in holes.get(r, {}).values():
                        for t in hs:
                            if t in starts:
                                cands.add((t, r))
            else:
                for t in holes.get(a.room, {}).get(day, set()):
                    if t in starts:
                        cands.add((t, a.room))
                for r in self.allowed[s.id]:
                    if r == a.room or _is_no_room(r):
                        continue
                    for t in holes.get(r, {}).get(day, set()):
                        if t in starts:
                            cands.add((t, r))
            best_t, best_r, best_d = None, None, self.tie_eps
            for t, r in cands:
                if t == a.slot and r == a.room:
                    continue
                if not self._free(s, t, r):
                    continue
                d = self._score_relocate(s, t, r)
                if d < best_d:
                    best_d, best_t, best_r = d, t, r
            if deadline is not None and time.time() > deadline:
                return moves
            if best_t is not None:
                self._remove(s)
                self._place(s, best_t, best_r)
                moves += 1
        return moves

    def _swap_clear(self, s1, s2, ta1, ta2):
        """True if s1@s2's slot and s2@s1's slot do not collide with each other
        in any resource they share (room, lecturer, section)."""
        n1 = set(range(ta1.slot, ta1.slot + s1.duration))
        n2 = set(range(ta2.slot, ta2.slot + s2.duration))
        if not (n1 & n2):
            return True
        if ta1.room == ta2.room and not _is_no_room(ta1.room):
            return False
        if s1.course.lecturer == s2.course.lecturer:
            return False
        for sec in s1.sections:
            if sec in s2.sections:
                return False
        return True

    def _score_swap(self, s1, s2):
        a1, a2 = self.assign[s1.id], self.assign[s2.id]
        o1 = range(a1.slot, a1.slot + s1.duration)
        o2 = range(a2.slot, a2.slot + s2.duration)
        n1 = range(a2.slot, a2.slot + s1.duration)
        n2 = range(a1.slot, a1.slot + s2.duration)
        d = 0.0
        if not _is_no_room(a1.room):
            d += ROOM_W * _rearrange_delta(self.room_occ[a1.room], [(o1, s1.id)], [(n2, s2.id)])
        if not _is_no_room(a2.room):
            d += ROOM_W * _rearrange_delta(self.room_occ[a2.room], [(o2, s2.id)], [(n1, s1.id)])
        for sec in set(s1.sections) | set(s2.sections):
            rem = []
            add = []
            if sec in s1.sections:
                rem.append((o1, s1.id))
                add.append((n1, s1.id))
            if sec in s2.sections:
                rem.append((o2, s2.id))
                add.append((n2, s2.id))
            d += COHORT_W * _rearrange_delta(self.sec_occ[sec], rem, add)
        d += LEC_W * _rearrange_delta(self.lec_occ[s1.course.lecturer], [(o1, s1.id)], [(n1, s1.id)])
        d += LEC_W * _rearrange_delta(self.lec_occ[s2.course.lecturer], [(o2, s2.id)], [(n2, s2.id)])
        return d

    def swap_pass(self, active_ids, deadline=None):
        moves = 0
        checked = 0
        for sid in active_ids:
            a1 = self.assign[sid]
            s1 = a1.session
            if getattr(s1, "fixed_slot", None) is not None:
                continue
            day = day_index_of(a1.slot)
            best_s2, best_d = None, 0.0
            for s2 in self.sessions:
                if s2.id == sid:
                    continue
                a2 = self.assign[s2.id]
                if getattr(s2, "fixed_slot", None) is not None:
                    continue
                if day_index_of(a2.slot) != day:
                    continue
                if a2.slot == a1.slot and a2.room == a1.room:
                    continue
                if a1.room not in self.allowed[s2.id] or a2.room not in self.allowed[s1.id]:
                    continue
                self._remove(s1)
                self._remove(s2)
                ok = self._free(s1, a2.slot, a2.room) and self._free(s2, a1.slot, a1.room)
                if ok:
                    ok = self._swap_clear(s1, s2, a2, a1)
                if ok:
                    d = self._score_swap(s1, s2)
                    if d < best_d:
                        best_d, best_s2 = d, s2
                self._place(s1, a1.slot, a1.room)
                self._place(s2, a2.slot, a2.room)
                checked += 1
                if deadline is not None and checked % 200 == 0 and time.time() > deadline:
                    return moves
            if deadline is not None and time.time() > deadline:
                return moves
            if best_s2 is not None:
                a2 = self.assign[best_s2.id]
                t1, r1 = a1.slot, a1.room
                self._remove(s1)
                self._remove(best_s2)
                self._place(s1, a2.slot, a2.room)
                self._place(best_s2, t1, r1)
                moves += 1
        return moves

    def active_sessions(self):
        active = set()
        for a in self.assign.values():
            r = a.room
            if _is_no_room(r):
                continue
            if day_index_of(a.slot) in self.holes.get(r, {}):
                active.add(a.session.id)
        return active

    def fill_holes(self, max_depth=4, max_fill=8):
        """Targeted chain search: for each remaining room hole, try to fill it
        by relocating sessions; if a session's departure opens a new hole, recurse
        until a departure closes a block edge. Returns holes filled."""
        filled = 0
        for _ in range(3):
            self.holes = self._compute_holes()
            targets = [(r, d, t) for r, days in self.holes.items() for d, hs in days.items() for t in hs]
            if not targets:
                break
            for r, d, t in targets:
                if t not in {u for hs in self.holes.get(r, {}).values() for u in hs}:
                    continue
                if self._try_fill(t, r, max_depth, set()):
                    filled += 1
            if filled >= max_fill:
                break
        return filled

    def _try_fill(self, t, r, depth, moved):
        if depth == 0:
            return False
        for s in self.sessions:
            if s.id in moved:
                continue
            if t not in self.starts[s.id] or r not in self.allowed[s.id]:
                continue
            if not self._free(s, t, r):
                continue
            a = self.assign[s.id]
            old_t, old_r = a.slot, a.room
            if old_t == t and old_r == r:
                continue
            old = range(old_t, old_t + s.duration)
            leaving_clean = True
            if not _is_no_room(old_r):
                d_leave = _rearrange_delta(self.room_occ[old_r], [(old, s.id)], [])
                if d_leave > 0:
                    leaving_clean = False
            self._remove(s)
            self._place(s, t, r)
            if leaving_clean:
                moved.add(s.id)
                return True
            moved.add(s.id)
            if self._try_fill(old_t, old_r, depth - 1, moved):
                return True
            moved.discard(s.id)
            self._remove(s)
            self._place(s, old_t, old_r)
        return False

    def run(self, time_budget=180.0, max_rounds=400):
        deadline = time.time() + time_budget
        rounds = 0
        while time.time() < deadline and rounds < max_rounds:
            rounds += 1
            self.holes = self._compute_holes()
            m1 = self.relocate_pass(deadline)
            self.holes = self._compute_holes()
            m2 = self.swap_pass(self.active_sessions(), deadline)
            if m1 + m2 == 0:
                break
        return rounds


def pack(problem, assignments, time_budget=180.0, max_rounds=400, debug=False, allow_plateau=False):
    p = Packer(problem, assignments, debug=debug, allow_plateau=allow_plateau)
    return p.run(time_budget, max_rounds)
