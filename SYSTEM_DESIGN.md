# UMaT Timetable Scheduling System — Design & Implementation Guide

## 1. Problem Statement

Generate weekly timetables for two semesters at UMaT (University of Mines and Technology) satisfying:

- **Hard constraints (zero tolerance):**
  - No section double-booked (a student group in two rooms at once)
  - No lecturer double-booked
  - No room double-booked
  - Room capacity ≥ class size
  - Same course, same section, at most once per day (Mon–Fri)
  - Saturday reserved for RT (online) sessions only
  - Lunch break (12:30–13:00) must not be crossed
  - Field-work sessions (08:30–16:00) off-campus, no room needed

- **Policy rules:**
  - An online-eligible course may mix in-person and ONLINE sessions; any session delivered ONLINE must be ONLINE for all its sections (A, B) — a split A/B pair is never one-in-person/one-online
  - Paired A/B sections of the same course must both be in-person or both ONLINE
  - ONLINE/VLE sessions may run in-person when a classroom is free; otherwise they fall back to the virtual venue
  - Target: maximize in-person delivery (minimize ONLINE fallback)

- **Scale:**
  - 14 rooms × 72 slots/week = 1,008 room-slots
  - ~530–550 sessions per semester (378 physical + 156 online + 12 field in sem1; 327 + 176 + 29 in sem2)
  - 60 sections, 57–67 lecturers
  - In-person demand: 1009h (sem1, slightly over capacity) / 973h (sem2) vs 1008h capacity

---

## 2. Architecture Overview

```
┌─────────────────────────────────────────────────────────────────┐
│                        DATA SOURCES                             │
│  courses.xlsx, rooms.xlsx, cohorts.xlsx, lecturers.xlsx        │
└──────────────────────┬──────────────────────────────────────────┘
                       ▼
┌─────────────────────────────────────────────────────────────────┐
│                    src/loaders.py                               │
│  Loads & normalizes → Session objects (code, sections, size,   │
│  online flag, duration, split_group, etc.)                     │
└──────────────────────┬──────────────────────────────────────────┘
                       ▼
┌─────────────────────────────────────────────────────────────────┐
│                     PHASE 1 — CP-SAT Feasibility                │
│  regen_worker.py + src/solver.solve()                           │
│  - All sessions variables (real rooms + ONLINE fallback)        │
│  - Hard constraints only (section/lecturer/room/same-day)       │
│  - Hints from school's prior timetable (warm start)             │
│  - feasibility_jump=True, num_workers=1, ~5–30s                 │
└──────────────────────┬──────────────────────────────────────────┘
                       ▼
┌─────────────────────────────────────────────────────────────────┐
│                    POSTPROCESS PIPELINE (regen.py)              │
│  1. compact() — coalesce adjacent sessions of same course       │
│  2. pack() — fill room idle holes, rebalance loads              │
│  3. rebalance_room_day() — spread load across days              │
│  4. prefer_sr4() — move small classes to SR 4 (40-cap)          │
│  5. co_teach_fallback() — merge tiny classes                    │
│  6. fill_online_rooms() — greedy ONLINE→room placement          │
│  7. aggressive_fill() — depth-1 chain shifts for ONLINE sessions│
│  8. ensure_conflict_free() — greedy repair of hard conflicts    │
│  9. paired_consistency() — A/B splits all-in or all-online      │
│ 10. conflict repair after consistency                          │
│ 11. sequential_fill() — per-session / A/B-pair greedy fill     │
│ 12. paired_consistency() backstop — never split an A/B pair    │
│ 13. conflict repair after sequential fill                      │
└──────────────────────┬──────────────────────────────────────────┘
                       ▼
┌─────────────────────────────────────────────────────────────────┐
│                      EXPORT & PUBLISH                           │
│  src/export.py → output/sem{N}/timetable_rows.json             │
│  web/main.py → Admin UI → Publish → output/sem{N}/published.json│
└─────────────────────────────────────────────────────────────────┘
```

---

## 3. Key Components

### 3.1 CP-SAT Model (`src/solver.py`)

**Variables:**
- `z[s.id, t, r]` ∈ {0,1} for each session `s`, allowed start `t`, allowed room `r`
- Every session gets exactly one assignment: `∑ z = 1`

**Constraints (hard):**
- Section exclusivity: `∑_{s∋sec} z[s,t,r] ≤ 1` per slot
- Lecturer exclusivity: `∑_{s: lecturer} z[s,t,r] ≤ 1` per slot
- Room exclusivity: `∑_{s: r∈allowed} z[s,t,r] ≤ 1` per slot
- Same-course-same-day-per-section: `∑_{s∈course,sec,day} z ≤ 1`
- Saturday only for RT-online; no lunch crossing; field-work window

**Objective (phase 1 = feasibility only, minimize_objective=False):**
- Soft terms: online penalty (weight 100), room oversize, lab preference, evening penalty, early utilization, room idle, cohort/lecturer gaps (disabled in production)

**Phase 1 runs with `fix_hinted=False`** — hints guide but don't pin (school's timetable may be infeasible).

### 3.2 Postprocess Pipeline (`regen.py`)

All steps mutate a list of `Assignment(session, slot, room)` objects.

| Step | Purpose | Key Mechanism |
|------|---------|---------------|
| `compact` | Merge adjacent same-course sessions | Slide sessions together if no gap |
| `Packer.pack` | Eliminate room idle holes | Iterative relocate/swap passes + `fill_holes` (depth-4 recursive chain) |
| `rebalance_room_day` | Balance daily room load | Move sessions to less-loaded days |
| `prefer_sr4` | Free large rooms for big classes | Move ≤40-student classes to SR 4 |
| `co_teach_fallback` | Merge tiny classes | Same course+section → one room |
| `fill_online_rooms` | Greedy ONLINE→room | First-fit into any free compatible cell |
| `aggressive_fill` | Depth-1 chain shifts | Move blocker to free cell, place ONLINE session |
| `ensure_conflict_free` | Hard conflict repair | Greedy relocate to free cell respecting section/lecturer/room |
| `paired_consistency` | A/B splits uniform | If mixed → both ONLINE |
| `sequential_fill` | Tail fill after consistency | Per-session / A/B-pair greedy into freed cells |
| `paired_consistency` (backstop) | Guard after fill | Reverts a split pair to ONLINE if ever mixed |

### 3.3 Sequential Fill (Tail Pass)

**Location:** `src/online_placement.py::place_courses_sequential()`

**Why:** The consistency passes free rooms by moving mixed split-pair sessions ONLINE; the tail pass reclaims those cells afterwards. A course may end up partially in-person / partially ONLINE (allowed) — only split A/B pairs stay uniform.

**Algorithm:**
1. Build occupancy maps (room/sec/lec) from current schedule; FIELD WORK blocks section/lecturer but not rooms
2. Fill unit = an **A/B split pair** (both currently ONLINE) or a **single online session** (no split partner, or partner already physical)
3. Score units by **total free cells** (fewest first = most constrained)
4. Backtracking per unit (1–2 sessions) into free cells with no room/section/lecturer/same-day conflicts
5. Commit the unit together or not at all → a split A/B pair is never mixed
6. Repeat rounds until no progress

**Performance:** <1s per semester; places dozens of sessions (some courses end up partially in person).

---

## 4. Configuration Files

### `data/semesters/sem1/settings.json` (sem2 similar)
```json
{
  "weights": {
    "room_oversize": 2,
    "evening": 1,
    "early_utilization": 8,
    "early_penalty_late_level": 2,
    "lecturer_overlap": 8,
    "section_overlap": 60,
    "lab_room": 2,
    "online": 100,
    "room_idle": 10
  },
  "overrides": {
    "phase1_time_limit": 1800,
    "split_combined_above": 90,
    "sr4_room": "",
    "small_class_cap": 0,
    "max_class_cap": 0,
    "regen_pack_budget": 0
  }
}
```

### Rooms (`rooms.xlsx`)
| name | capacity | kind |
|------|----------|------|
| M. AUDITORIUM | 120 | lecture |
| SR 12, SR 13 | 120 | lecture |
| SR 14, 15, 1A, 1B, 3, 5A, 5B, 7A, 7B | 80 | lecture |
| SR 4 | 40 | lecture |
| COMPUTER LAB | 80 | lab |

---

## 5. Running the System

### Prerequisites
- Python 3.11+
- `ortools` (`pip install ortools`)
- `pandas`, `openpyxl`

### Generate a Semester
```bash
cd umat-timetable-solver
python -m regen sem1    # 3–5 minutes
python -m regen sem2    # 1–2 minutes
```

Outputs:
- `output/sem1/timetable_rows.json` — flat rows for web UI
- `output/sem1/solve_result.json` — summary stats
- `output/sem1/*.csv` — lecturer/room/section views

### Web UI & Publishing
```bash
python web/main.py
# Visit http://localhost:5000
# Admin → Login (admin/admin) → Publish
```
Publish writes `output/sem{N}/published.json` served to students.

### Deploy to Oracle VM
```bash
scp -i ssh-key-2026-08-19-001.key -r output/ ubuntu@147.15.136.161:/var/www/umat-timetable/
ssh -i ssh-key-2026-08-19-001.key ubuntu@147.15.136.161 "systemctl restart umat-web"
```

---

## 6. Key Design Decisions & Rationale

| Decision | Rationale |
|----------|-----------|
| Phase 2 (global optimize) disabled | Full soft-constraint model (~324k vars) intractable >300s; postprocess achieves comparable quality in <100s |
| Hints never pinned (`fix_hinted=False`) | School's own timetable often infeasible; pinning causes INFEASIBLE |
| ONLINE as fallback cell in model | Lets CP-SAT place online sessions physically if space exists; objective (weight 100) drives physical placement |
| Consistency AFTER placement | Aggressive fill could mix an A/B split; the paired-consistency pass resolves pairs cleanly |
| Per-session fill (not whole-course) | A course may mix in-person & ONLINE sessions; only split A/B pairs must stay uniform |
| Repair after consistency | Consistency passes were introducing room conflicts by moving sessions to ONLINE without repair |
| Sequential tail pass | Exploits cells freed by consistency; per-session / A-B-pair greedy without CP-SAT overhead |
| FIELD WORK in section/lecturer occupancy | Students/lecturer away → can't attend other classes; matches full CP-SAT model |

---

## 7. Current Results

| Semester | Utilization | Physical | Online | Field | Conflicts | Same-day | Split-Pair Splits |
|----------|-------------|----------|--------|-------|-----------|----------|-------------------|
| sem1 | **80.0%** | 437 | 97 | 12 | 0 | 27 | 0 |
| sem2 | **81.0%** | 431 | 72 | 29 | 0 | 21 | 0 |

Utilization = share of sessions placed in real rooms. `Conflicts` = section + room + capacity
violations and lecturer overlaps (all 0, checked on the shipped state by `_verify` in
`regen.py`). `Split-Pair Splits` = split A/B sections where one is in person and the other
ONLINE (0 — the binding rule). Courses may mix in-person and ONLINE sessions; partial-course
mixing is allowed. `Same-day` is a post-check only (students with 2+ classes in one day); it is
not yet constrained or minimized by the model.

Key courses in-person: **DS 167** (sem1), **CE 474, EL 162** (sem2).

---

## 8. Troubleshooting / Common Issues

| Symptom | Likely Cause | Fix |
|---------|--------------|-----|
| Phase 1 "NO SOLUTION (time limit)" | Demand > capacity, or hints file stale | Delete `C:\Users\USER\AppData\Local\Temp\umat-tt\solve_sem{N}_p1.json` and re-run |
| Split A/B pair mixed (one in person, one online) | Fill unit / backstop failed | Post-fill `paired_consistency` backstop reverts the pair to ONLINE |
| Room conflicts in M. AUDITORIUM evening | Large classes + limited big rooms | Check `prefer_sr4` moved small classes out; increase `split_combined_above` |
| "CE 474" missing in output | Course only exists in sem2 | Verify semester data files |
| MemoryError on full run | Old bug; fixed by clearing temp files | `del %TEMP%\umat-tt\*.json` before run |

---

## 9. Extending the System

- **New semester:** Create `data/semesters/sem3/{courses,cohorts,rooms}.xlsx` + `settings.json`; run `python -m regen sem3`
- **New constraint:** Add to `src/solver.py` `_build_model()` (hard) or objective terms (soft)
- **Different objective:** Modify `place_courses_sequential()` or add a new CP-SAT phase
- **Web UI changes:** Edit `web/templates/*.html`, `web/main.py`

---

## 10. File Map (Core)

```
umat-timetable-solver/
├── regen.py              # Main pipeline orchestration
├── regen_worker.py       # Phase 1 subprocess wrapper
├── src/
│   ├── loaders.py        # Excel → Session objects
│   ├── solver.py         # CP-SAT model + solve()
│   ├── pack.py           # Packer class (relocate/swap/fill_holes/aggressive_fill)
│   ├── compact.py        # compact(), fill_online_rooms()
│   ├── rebalance.py      # rebalance_room_day()
│   ├── online_placement.py # place_online_courses (CP-SAT) + place_courses_sequential (greedy)
│   ├── export.py         # Output writers
│   ├── models.py         # Room, Cohort, Course, Session dataclasses
│   └── slots.py          # Slot arithmetic (N_SLOTS=72, day_index_of, etc.)
├── web/
│   ├── main.py           # Flask app + Admin/Publish
│   └── templates/        # HTML templates
├── data/
│   ├── semesters/sem1/   # Semester 1 input data
│   └── semesters/sem2/   # Semester 2 input data
└── output/sem{N}/        # Generated outputs
```

---

*Last updated: 2026-09-08 — reflects pipeline with sequential tail pass + dual repair stages.*