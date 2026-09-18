import time

from .slots import N_SLOTS, SLOTS_PER_DAY, day_index_of
from .solver import _allowed_rooms, _allowed_starts, _is_no_room, _tier, ONLINE_ROOM, FIELD_WORK_ROOM, _verify

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
    def __init__(self, problem, assignments, debug=False, allow_plateau=False, rng=None):
        import random
        self.sessions = problem["sessions"]
        self.rooms = problem["rooms"]
        self.debug = debug
        self.allow_plateau = allow_plateau
        self.tie_eps = 0.001 if allow_plateau else 0.0
        self.rng = rng if rng is not None else random.Random()
        self.allowed = {s.id: _allowed_rooms(s, problem["rooms"]) for s in self.sessions}
        room_cap = {r.name: r.capacity for r in problem["rooms"]}
        sessions_by_id = {s.id: s for s in self.sessions}
        for sid, rooms in self.allowed.items():
            need = _tier(max(sessions_by_id[sid].size, sessions_by_id[sid].course.min_capacity))

            def pref_key(r, need=need):
                c = room_cap.get(r)
                if c is None:
                    return (1, 0)
                if need > 2:
                    return (0, -c)
                return (0, c)

            rooms.sort(key=pref_key)
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
        if s.id not in self.assign:
            return
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
        if s.id not in self.assign:
            return
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
        if s.id not in self.assign:
            return float('inf')
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
        sessions = list(self.sessions)
        self.rng.shuffle(sessions)
        for s in sessions:
            if s.id not in self.assign:
                continue
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
        active = list(active_ids)
        self.rng.shuffle(active)
        for sid in active:
            a1 = self.assign[sid]
            s1 = a1.session
            if getattr(s1, "fixed_slot", None) is not None:
                continue
            day = day_index_of(a1.slot)
            best_s2, best_d = None, 0.0
            for s2 in self.sessions:
                if s2.id == sid:
                    continue
                if s2.id not in self.assign:
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

    def fill_holes(self, max_depth=4, max_fill=20):
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
        sessions = list(self.sessions)
        self.rng.shuffle(sessions)
        for s in sessions:
            if s.id in moved:
                continue
            if t not in self.starts[s.id] or r not in self.allowed[s.id]:
                continue
            if not self._free(s, t, r):
                continue
            if s.id not in self.assign:
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

    def _blockers_at(self, s, t, r):
        """Return set of session ids occupying (t..t+dur) for s at room r, sections, or lecturer.
        Excludes s itself and ONLINE/FIELD_WORK rooms (they don't occupy room_occ)."""
        dur = s.duration
        new = range(t, t + dur)
        blockers = set()
        # Room blockers
        if not _is_no_room(r):
            arr = self.room_occ.get(r, [])
            for u in new:
                v = arr[u]
                if v is not None:
                    blockers.add(v)
        # Section blockers
        for sec in s.sections:
            arr = self.sec_occ.get(sec, [])
            for u in new:
                v = arr[u]
                if v is not None:
                    blockers.add(v)
        # Lecturer blockers
        lec = s.course.lecturer
        if lec:
            arr = self.lec_occ.get(lec, [])
            for u in new:
                v = arr[u]
                if v is not None:
                    blockers.add(v)
        blockers.discard(s.id)
        return blockers

    def _candidate_homes(self, s, exclude_t=None, exclude_r=None):
        """Yield (t, r) pairs sorted by _score_relocate (best first)."""
        cands = []
        for t in self.starts[s.id]:
            if exclude_t is not None and t == exclude_t:
                continue
            for r in self.allowed[s.id]:
                if r == ONLINE_ROOM or r == FIELD_WORK_ROOM:
                    continue
                if exclude_r is not None and r == exclude_r:
                    continue
                if self._free(s, t, r):
                    d = self._score_relocate(s, t, r)
                    cands.append((d, t, r))
        cands.sort(key=lambda x: x[0])
        for d, t, r in cands:
            yield t, r

    def aggressive_fill(self, problem, assignments, time_budget=180.0, max_depth=3, max_restarts=6):
        """Multi-restart depth-1 chain placement for online sessions.
        Only displaces blockers that have a completely free alternative cell (no recursion).
        Uses _verify for comprehensive conflict checking after each chain commit.
        Returns (placements_made, best_assignments_dict)."""
        import time
        deadline = time.time() + time_budget
        best_placed = -1
        best_assign = None
        online_sessions = [s for s in self.sessions
                           if s.online and s.id in self.assign and self.assign[s.id].room == ONLINE_ROOM]
        if not online_sessions:
            return 0, None
        # Order by constrainedness (fewest candidate homes first)
        def constrainedness(s):
            cnt = 0
            for t in self.starts[s.id]:
                for r in self.allowed[s.id]:
                    if r == ONLINE_ROOM or r == FIELD_WORK_ROOM:
                        continue
                    if self._free(s, t, r):
                        cnt += 1
            return cnt
        for restart in range(max_restarts):
            if time.time() > deadline:
                break
            # Rebuild occupancy arrays from current assignments (fresh per restart)
            self.sec_occ = {}
            self.lec_occ = {}
            self.room_occ = {}
            for a in self.assign.values():
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
            # Shuffle and sort by constrainedness
            ordered = sorted(online_sessions, key=lambda s: (constrainedness(s), self.rng.random()))
            placed = 0
            for s in ordered:
                if s.id not in self.assign:
                    continue
                a = self.assign[s.id]
                if a.room != ONLINE_ROOM:
                    continue  # already placed by earlier online session in this restart
                # Try direct placement first (no chain)
                direct_placed = False
                for t in self.starts[s.id]:
                    for r in self.allowed[s.id]:
                        if r == ONLINE_ROOM or r == FIELD_WORK_ROOM:
                            continue
                        if self._free(s, t, r):
                            self._remove(s)
                            self._place(s, t, r)
                            placed += 1
                            direct_placed = True
                            break
                    if direct_placed:
                        break
                if direct_placed:
                    continue
                # Depth-1 chain: find a cell where all blockers have free alternative homes
                best_chain = None
                for t in self.starts[s.id]:
                    for r in self.allowed[s.id]:
                        if r == ONLINE_ROOM or r == FIELD_WORK_ROOM:
                            continue
                        if t == a.slot and r == a.room:
                            continue
                        blockers = self._blockers_at(s, t, r)
                        if not blockers:
                            continue
                        # Only consider physical blockers
                        physical_blockers = [bid for bid in blockers
                                             if not self._is_online_or_field(bid)]
                        if not physical_blockers:
                            continue
                        # Try to find free alternative homes for ALL blockers
                        blocker_homes = {}
                        moved = set()
                        ok = True
                        for bid in physical_blockers:
                            if bid in moved:
                                ok = False
                                break
                            blocker_session = next((ses for ses in self.sessions if ses.id == bid), None)
                            if blocker_session is None or getattr(blocker_session, "fixed_slot", None) is not None:
                                ok = False
                                break
                            # Find a completely free home for this blocker
                            found = False
                            for bt, br in self._candidate_homes(blocker_session):
                                if (bt, br) == (self.assign[bid].slot, self.assign[bid].room):
                                    continue
                                # Check this home doesn't conflict with other blockers' new homes
                                conflict = False
                                for other_bid, (ot, or_) in blocker_homes.items():
                                    if self._sessions_overlap(blocker_session, bt, br, other_bid):
                                        conflict = True
                                        break
                                if conflict:
                                    continue
                                blocker_homes[bid] = (bt, br)
                                moved.add(bid)
                                found = True
                                break
                            if not found:
                                ok = False
                                break
                        if ok and blocker_homes:
                            best_chain = blocker_homes
                            break
                    if best_chain:
                        break
                if best_chain:
                    # Save original positions for rollback (blockers + online session)
                    original = {bid: (self.assign[bid].slot, self.assign[bid].room) for bid in best_chain if bid in self.assign}
                    original[s.id] = (self.assign[s.id].slot, self.assign[s.id].room)
                    # Commit: move blockers to their new homes
                    for bid, (bt, br) in best_chain.items():
                        blocker = next(ses for ses in self.sessions if ses.id == bid)
                        self._remove(blocker)
                        self._place(blocker, bt, br)
                    # Place online session
                    self._remove(s)
                    self._place(s, t, r)
                    # Full conflict check using proven _verify
                    checks = _verify(assignments, problem)
                    conflict = any(checks[k] for k in checks if checks[k])
                    if conflict:
                        # Rollback
                        for bid in best_chain:
                            blocker = next(ses for ses in self.sessions if ses.id == bid)
                            old_slot, old_room = original[bid]
                            self._remove(blocker)
                            self._place(blocker, old_slot, old_room)
                        self._remove(s)
                        self._place(s, original[s.id][0], original[s.id][1])
                    else:
                        placed += 1
            if placed > best_placed:
                best_placed = placed
                best_assign = {sid: (a.slot, a.room) for sid, a in self.assign.items()}
        if best_assign:
            for sid, (slot, room) in best_assign.items():
                a = self.assign[sid]
                a.slot = slot
                a.room = room
        return best_placed, best_assign

    def compaction_pass(self, deadline=None):
        """Fill ALL gaps in large rooms by moving sessions using chain scheduling.
        Move session A to gap, move blocker B to A's old spot, etc."""
        if deadline is None:
            deadline = float('inf')
        
        # 120-cap rooms
        large_rooms = [r.name for r in self.rooms if r.capacity >= 100]
        moves = 0
        
        # Compute holes for each large room per day
        holes = self._compute_holes()
        
        for room in large_rooms:
            if time.time() > deadline:
                break
            room_holes = holes.get(room, {})
            for day in range(5):  # Mon-Fri
                if time.time() > deadline:
                    break
                day_holes = room_holes.get(day, set())
                if not day_holes:
                    continue
                
                # Try to fill each gap
                for gap_slot in sorted(day_holes):
                    # Find sessions that could fit here (size <= 120, not fixed)
                    candidates = []
                    for s in self.sessions:
                        if s.id not in self.assign:
                            continue
                        a = self.assign[s.id]
                        if a.room in (ONLINE_ROOM, FIELD_WORK_ROOM):
                            continue
                        if getattr(s, "fixed_slot", None) is not None:
                            continue
                        if s.duration > 1 and gap_slot + 1 not in self.holes.get(room, {}).get(day, set()):
                            continue  # Need consecutive slots for 2h sessions
                        if room not in self.allowed[s.id]:
                            continue
                        # Check if session fits at this gap
                        if self._free(s, gap_slot, room):
                            candidates.append((s, a))
                    
                    # Try to move a candidate into this gap using chain scheduling
                    for s, old_a in candidates:
                        if s.id not in self.assign:
                            continue
                        if self._free(s, gap_slot, room):
                            # Move it directly if free
                            self._remove(s)
                            self._place(s, gap_slot, room)
                            moves += 1
                            break
        
        return moves

    def compaction_pass_with_chains(self, deadline=None):
        """Fill gaps using chain scheduling: move A to gap, move blocker B to A's old spot, etc."""
        if deadline is None:
            deadline = float('inf')
        
        large_rooms = [r.name for r in self.problem["rooms"] if r.capacity >= 100]
        moves = 0
        holes = self._compute_holes()
        
        for room in large_rooms:
            if time.time() > deadline:
                break
            room_holes = holes.get(room, {})
            for day in range(5):
                if time.time() > deadline:
                    break
                day_holes = room_holes.get(day, set())
                if not day_holes:
                    continue
                
                for gap_slot in sorted(day_holes):
                    # Find sessions that could fit
                    for s in self.sessions:
                        if time.time() > deadline:
                            break
                        if s.id not in self.assign:
                            continue
                        a = self.assign[s.id]
                        if a.room in (ONLINE_ROOM, FIELD_WORK_ROOM):
                            continue
                        if getattr(s, "fixed_slot", None) is not None:
                            continue
                        if room not in self.allowed[s.id]:
                            continue
                        if s.duration > 1 and gap_slot + 1 not in self.holes.get(room, {}).get(day, set()):
                            continue
                        
                        if self._free(s, gap_slot, room):
                            # Direct move
                            self._remove(s)
                            self._place(s, gap_slot, room)
                            moves += 1
                            break
                        else:
                            # Try chain: move blocker to s's old spot
                            blockers = self._blockers_at(s, gap_slot, room)
                            if not blockers:
                                continue
                            
                            # Try to move one blocker to s's old spot
                            old_slot, old_room = a.slot, a.room
                            for bid in blockers:
                                blocker = next((ses for ses in self.sessions if ses.id == bid), None)
                                if not blocker or getattr(blocker, "fixed_slot", None) is not None:
                                    continue
                                if self._free(blocker, old_slot, old_room):
                                    # Chain move: blocker -> s's old spot, s -> gap
                                    self._remove(blocker)
                                    self._place(blocker, old_slot, old_room)
                                    self._remove(s)
                                    self._place(s, gap_slot, room)
                                    moves += 1
                                    break
                            if moves > 0:
                                break
        
        return moves

    def _is_online_or_field(self, session_id):
        s = next((ses for ses in self.sessions if ses.id == session_id), None)
        return s is not None and (s.online or s.field_work)

    def _sessions_overlap(self, s1, t1, r1, s2_id):
        """Check if s1 at (t1,r1) conflicts with s2 at its assigned slot/room."""
        if s2_id not in self.assign:
            return False
        s2 = self.assign[s2_id].session
        a2 = self.assign[s2_id]
        if r1 != ONLINE_ROOM and r1 != FIELD_WORK_ROOM and a2.room == r1:
            # Same room - check time overlap
            s1_slots = set(range(t1, t1 + s1.duration))
            s2_slots = set(range(a2.slot, a2.slot + s2.duration))
            if s1_slots & s2_slots:
                return True
        # Check section overlap
        if s1.sections & s2.sections:
            s1_slots = set(range(t1, t1 + s1.duration))
            s2_slots = set(range(a2.slot, a2.slot + s2.duration))
            if s1_slots & s2_slots:
                return True
        # Check lecturer overlap
        if s1.course.lecturer and s1.course.lecturer == s2.course.lecturer:
            s1_slots = set(range(t1, t1 + s1.duration))
            s2_slots = set(range(a2.slot, a2.slot + s2.duration))
            if s1_slots & s2_slots:
                return True
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
            
            # Compaction pass: fill gaps in large rooms using chain scheduling
            if time.time() < deadline:
                self.holes = self._compute_holes()
                mc = self.compaction_pass(deadline)
                if mc:
                    print(f"  compaction: {mc} moves", flush=True)
            
            if m1 + m2 == 0:
                break
        return rounds


def pack(problem, assignments, time_budget=180.0, max_rounds=400, debug=False, allow_plateau=False, rng=None):
    p = Packer(problem, assignments, debug=debug, allow_plateau=allow_plateau, rng=rng)
    return p.run(time_budget, max_rounds)


def evacuate_smalls_from_halls(problem, assignments):
    """Construction-side tiering guard: move any small/mid class (need tier <= 2,
    i.e. at most 80 seats) that is still sitting in a 120-seat hall into its best
    conflict-free fitting room. Runs last so it catches whichever tail pass
    re-planted them (repairs/spreads that use absolute room names). Uses the
    Packer's own conflict-safe relocate machinery. Returns sessions moved."""
    from .solver import _tier

    pk = Packer(problem, assignments)
    cap = {r.name: r.capacity for r in problem["rooms"]}
    moved = 0
    for _ in range(3):
        changed = False
        for s in pk.sessions:
            if s.field_work or _tier(max(s.size, s.course.min_capacity)) > 2:
                continue
            a = pk.assign.get(s.id)
            if a is None or _is_no_room(a.room) or _tier(cap.get(a.room, 0)) < 3:
                continue
            for t, r in pk._candidate_homes(s, exclude_t=a.slot, exclude_r=a.room):
                pk._remove(s)
                pk._place(s, t, r)
                moved += 1
                changed = True
                break
        if not changed:
            break
    return moved
