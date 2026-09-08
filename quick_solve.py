from src.loaders import load_problem
from src.paths import DATA_DIR
from src.solver import solve, _verify
import time

sem = 'sem1'
p = load_problem(str(DATA_DIR / 'semesters' / sem))
print(f'Sessions: {len(p["sessions"])}')

# Try short runs to see if feasibility_jump finds anything
for tl in [30, 60]:
    t0 = time.time()
    r = solve(p, time_limit=tl, minimize_objective=False, feasibility_jump=True, seed=42)
    elapsed = time.time() - t0
    c = _verify(r.assignments, p)
    total_issues = sum(len(v) if isinstance(v, list) else 0 for v in c.values())
    print(f'  time_limit={tl}s: {r.status}, {len(r.assignments)}/{len(p["sessions"])} assigned, issues={total_issues}, elapsed={elapsed:.1f}s')
    if r.status.startswith('OPTIMAL') or r.status.startswith('FEASIBLE'):
        for k, v in c.items():
            if isinstance(v, list) and v:
                print(f'    {k}: {len(v)}')
        break
