"""
Fast greedy+local-search timetable solver.
Replaces CP-SAT phase1 with a constructive heuristic that builds a feasible schedule incrementally.
"""
import random
from collections import defaultdict
from src.loaders import load_problem
from src.solver import _allowed_rooms, _allowed_starts, N_SLOTS, SLOTS_PER_DAY, DAYS, day_index_of, slot_in_day, ONLINE_ROOM, FIELD_WORK_ROOM
from src.solver import Assignment
import time

def greedy_phase1(problem, time_limit=60, seed=7):
    """Build a feasible timetable using greedy constructive heuristic."""
    random.seed(seed)
    sessions = problem["sessions"]
    rooms = problem["rooms"]
    room_capacity = {r.name: r.capacity for r in rooms}
    room_kind = {r.name: r.kind for r in rooms}
    sections = problem["sections"]
    lecturers = problem["lecturers"]
    cohorts = problem["cohorts"]
    
    # Precompute real cohort sizes for each session
    real_sizes = {}
    for s in sessions:
        real_sizes[s.id] = sum(cohorts[sec].size for sec in s.sections if sec in cohorts)
    
    # Attach real sizes to sessions for _allowed_rooms
    for s in sessions:
        s.real_size = real_sizes[s.id]
    
    allowed = {}
    starts = {}
    for s in sessions:
        allowed[s.id] = _allowed_rooms(s, rooms)
        starts[s.id] = _allowed_starts(s)
    
    # Sort sessions: most constrained first
    lec_load = defaultdict(int)
    for s in sessions:
        if not s.online and not s.field_work:
            lec_load[s.course.lecturer] += s.duration
    
    def priority(s):
        if s.online or s.field_work:
            return (0, 0, 0, 0)
        n_rooms = len(allowed[s.id])
        n_starts = len(starts[s.id])
        size = max(real_sizes[s.id], s.course.min_capacity)
        return (-1, -size, n_rooms, n_starts, -lec_load[s.course.lecturer])
    
    sessions_sorted = sorted(sessions, key=priority)
    
    # Track occupied slots
    room_occupied = defaultdict(set)      # room -> set of occupied slots
    section_occupied = defaultdict(set)   # section -> set of occupied slots
    lecturer_occupied = defaultdict(set)  # lecturer -> set of occupied slots
    course_section_day = defaultdict(set) # (course_code, section) -> set of days used
    
    assignments = []
    unassigned = []
    
    start_time = time.time()
    
    for s in sessions_sorted:
        if time.time() - start_time > time_limit:
            unassigned.extend(sessions_sorted[sessions_sorted.index(s):])
            break
            
        if s.online:
            # Online sessions just need a slot (room = ONLINE)
            for t in starts[s.id]:
                conflict = False
                for u in range(t, t + s.duration):
                    if s.sections:
                        for sec in s.sections:
                            if u in section_occupied[sec]:
                                conflict = True
                                break
                        if conflict: break
                    if s.course.lecturer and u in lecturer_occupied[s.course.lecturer]:
                        conflict = True
                        break
                if not conflict:
                    # Assign
                    a = Assignment(s, t, ONLINE_ROOM)
                    assignments.append(a)
                    for u in range(t, t + s.duration):
                        if s.sections:
                            for sec in s.sections:
                                section_occupied[sec].add(u)
                        if s.course.lecturer:
                            lecturer_occupied[s.course.lecturer].add(u)
                        # course-day
                        d = day_index_of(u)
                        if s.sections:
                            for sec in s.sections:
                                course_section_day[(s.course.code, sec)].add(d)
                    break
            else:
                unassigned.append(s)
            continue
        
        if s.field_work:
            a = Assignment(s, starts[s.id][0], FIELD_WORK_ROOM)
            assignments.append(a)
            continue
        
        # Physical session: try rooms and slots
        placed = False
        # Sort rooms by capacity (smallest adequate first) to reserve large rooms for large sessions
        need = max(real_sizes[s.id], s.course.min_capacity)
        room_list = [r for r in allowed[s.id] if room_capacity.get(r, 0) >= need]
        room_list.sort(key=lambda r: room_capacity.get(r, 0))  # smallest adequate first
        
        for r in room_list:
            for t in starts[s.id]:
                # Check all constraints
                conflict = False
                
                # Room overlap
                for u in range(t, t + s.duration):
                    if u in room_occupied[r]:
                        conflict = True
                        break
                if conflict: continue
                
                # Section overlap
                if s.sections:
                    for sec in s.sections:
                        for u in range(t, t + s.duration):
                            if u in section_occupied[sec]:
                                conflict = True
                                break
                        if conflict: break
                if conflict: continue
                
                # Lecturer overlap
                if s.course.lecturer:
                    for u in range(t, t + s.duration):
                        if u in lecturer_occupied[s.course.lecturer]:
                            conflict = True
                            break
                    if conflict: continue
                
                # Same course same day per section
                if s.sections:
                    d = day_index_of(t)
                    for sec in s.sections:
                        if d in course_section_day[(s.course.code, sec)]:
                            conflict = True
                            break
                    if conflict: continue
                
                # All good - place it (room already filtered by capacity)
                a = Assignment(s, t, r)
                assignments.append(a)
                for u in range(t, t + s.duration):
                    room_occupied[r].add(u)
                    if s.sections:
                        for sec in s.sections:
                            section_occupied[sec].add(u)
                    if s.course.lecturer:
                        lecturer_occupied[s.course.lecturer].add(u)
                    d = day_index_of(u)
                    if s.sections:
                        for sec in s.sections:
                            course_section_day[(s.course.code, sec)].add(d)
                placed = True
                break
            
            if placed:
                break
        
        if not placed:
            unassigned.append(s)
    
    print(f"  Greedy placed {len(assignments)}/{len(sessions)} sessions, {len(unassigned)} unassigned")
    
    # Local search: try to fix unassigned by moving already-placed sessions
    if unassigned:
        print(f"  Local search on {len(unassigned)} unassigned...")
        assignments = local_search_fix(assignments, unassigned, problem, starts, allowed, room_capacity, room_occupied, section_occupied, lecturer_occupied, course_section_day, real_sizes, time_limit - (time.time() - start_time))
    
    return assignments

def local_search_fix(assignments, unassigned, problem, starts, allowed, room_capacity, room_occupied, section_occupied, lecturer_occupied, course_section_day, real_sizes, time_limit):
    """Try to fit unassigned sessions by moving already-placed ones."""
    if time_limit <= 0:
        return assignments
    
    sessions = problem["sessions"]
    sections = problem["sections"]
    lecturers = problem["lecturers"]
    
    # Build quick lookup
    assigned_by_session = {a.session.id: a for a in assignments}
    
    start_time = time.time()
    
    for s in unassigned:
        if time.time() - start_time > time_limit:
            break
            
        if s.online or s.field_work:
            continue
            
        # Try to find a slot by potentially moving ONE other session
        need = max(real_sizes[s.id], s.course.min_capacity)
        room_list = [r for r in allowed[s.id] if room_capacity.get(r, 0) >= need]
        room_list.sort(key=lambda r: room_capacity.get(r, 0))
        
        for r in room_list:
            for t in starts[s.id]:
                # Check if this slot works (same checks as greedy)
                conflict = False
                conflicting = []
                
                for u in range(t, t + s.duration):
                    if u in room_occupied[r]:
                        # Find which session occupies it
                        for a in assignments:
                            if a.room == r and a.slot <= u < a.slot + a.session.duration:
                                conflicting.append(a)
                                conflict = True
                                break
                
                if s.sections:
                    for sec in s.sections:
                        for u in range(t, t + s.duration):
                            if u in section_occupied[sec]:
                                for a in assignments:
                                    if sec in a.session.sections and a.slot <= u < a.slot + a.session.duration:
                                        if a not in conflicting:
                                            conflicting.append(a)
                                        conflict = True
                                break
                
                if s.course.lecturer:
                    for u in range(t, t + s.duration):
                        if u in lecturer_occupied[s.course.lecturer]:
                            for a in assignments:
                                if a.session.course.lecturer == s.course.lecturer and a.slot <= u < a.slot + a.session.duration:
                                    if a not in conflicting:
                                        conflicting.append(a)
                                    conflict = True
                            break
                
                if s.sections:
                    d = day_index_of(t)
                    for sec in s.sections:
                        if d in course_section_day[(s.course.code, sec)]:
                            for a in assignments:
                                if sec in a.session.sections and day_index_of(a.slot) == d and a.session.course.code == s.course.code:
                                    if a not in conflicting:
                                        conflicting.append(a)
                                    conflict = True
                            break
                
                need = max(real_sizes[s.id], s.course.min_capacity)
                if room_capacity.get(r, 0) < need:
                    conflict = True
                
                if not conflict:
                    # Place it directly
                    a = Assignment(s, t, r)
                    assignments.append(a)
                    for u in range(t, t + s.duration):
                        room_occupied[r].add(u)
                        if s.sections:
                            for sec in s.sections:
                                section_occupied[sec].add(u)
                        if s.course.lecturer:
                            lecturer_occupied[s.course.lecturer].add(u)
                        d = day_index_of(u)
                        if s.sections:
                            for sec in s.sections:
                                course_section_day[(s.course.code, sec)].add(d)
                    break
                elif len(conflicting) == 1:
                    # Try to move the single conflicting session
                    other = conflicting[0]
                    if try_move_session(other, assignments, starts, allowed, room_capacity, room_occupied, section_occupied, lecturer_occupied, course_section_day, real_sizes, problem):
                        # Now place our session
                        a = Assignment(s, t, r)
                        assignments.append(a)
                        for u in range(t, t + s.duration):
                            room_occupied[r].add(u)
                            if s.sections:
                                for sec in s.sections:
                                    section_occupied[sec].add(u)
                            if s.course.lecturer:
                                lecturer_occupied[s.course.lecturer].add(u)
                            d = day_index_of(u)
                            if s.sections:
                                for sec in s.sections:
                                    course_section_day[(s.course.code, sec)].add(d)
                        break
            else:
                continue
            break
    
    return assignments

def try_move_session(session_to_move, assignments, starts, allowed, room_capacity, room_occupied, section_occupied, lecturer_occupied, course_section_day, real_sizes, problem):
    """Try to find a new slot for an already-placed session."""
    s = session_to_move.session
    old_t = session_to_move.slot
    old_r = session_to_move.room
    
    # Remove from occupied sets
    for u in range(old_t, old_t + s.duration):
        room_occupied[old_r].discard(u)
        if s.sections:
            for sec in s.sections:
                section_occupied[sec].discard(u)
        if s.course.lecturer:
            lecturer_occupied[s.course.lecturer].discard(u)
        d = day_index_of(u)
        if s.sections:
            for sec in s.sections:
                course_section_day[(s.course.code, sec)].discard(d)
    
    # Try new positions
    need = max(real_sizes[s.id], s.course.min_capacity)
    room_list = [r for r in allowed[s.id] if room_capacity.get(r, 0) >= need]
    room_list.sort(key=lambda r: room_capacity.get(r, 0))
    for r in room_list:
        if r == old_r:
            continue
        for t in starts[s.id]:
            if t == old_t:
                continue
            
            conflict = False
            for u in range(t, t + s.duration):
                if u in room_occupied[r]:
                    conflict = True
                    break
            if conflict: continue
            
            if s.sections:
                for sec in s.sections:
                    for u in range(t, t + s.duration):
                        if u in section_occupied[sec]:
                            conflict = True
                            break
                    if conflict: break
            if conflict: continue
            
            if s.course.lecturer:
                for u in range(t, t + s.duration):
                    if u in lecturer_occupied[s.course.lecturer]:
                        conflict = True
                        break
                if conflict: continue
            
            if s.sections:
                d = day_index_of(t)
                for sec in s.sections:
                    if d in course_section_day[(s.course.code, sec)]:
                        conflict = True
                        break
                if conflict: continue
            
            need = max(real_sizes[s.id], s.course.min_capacity)
            if room_capacity.get(r, 0) < need:
                conflict = True
            if conflict: continue
            
            # Found new spot!
            session_to_move.slot = t
            session_to_move.room = r
            for u in range(t, t + s.duration):
                room_occupied[r].add(u)
                if s.sections:
                    for sec in s.sections:
                        section_occupied[sec].add(u)
                if s.course.lecturer:
                    lecturer_occupied[s.course.lecturer].add(u)
                d = day_index_of(u)
                if s.sections:
                    for sec in s.sections:
                        course_section_day[(s.course.code, sec)].add(d)
            return True
    
    # Restore old position
    for u in range(old_t, old_t + s.duration):
        room_occupied[old_r].add(u)
        if s.sections:
            for sec in s.sections:
                section_occupied[sec].add(u)
        if s.course.lecturer:
            lecturer_occupied[s.course.lecturer].add(u)
        d = day_index_of(u)
        if s.sections:
            for sec in s.sections:
                course_section_day[(s.course.code, sec)].add(d)
    
    return False

if __name__ == "__main__":
    problem = load_problem("data/semesters/sem1")
    assignments = greedy_phase1(problem, time_limit=60, seed=7)
    print(f"Final: {len(assignments)} assigned")