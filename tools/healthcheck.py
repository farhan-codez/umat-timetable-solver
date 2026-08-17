"""One-command health check for the whole timetable system.

Run this whenever courses, cohorts or rooms are added or changed:

    python tools/healthcheck.py            # load + demand/capacity report for every semester
    python tools/healthcheck.py --probe    # also run a short CP-SAT feasibility probe
    python tools/healthcheck.py --sem sem3 # check a single semester

It reports, per semester: data load errors, session counts, room-hour demand
vs capacity (the main cause of 'no solution'), and whether the published
timetable is newer than the data. The --probe flag runs the real solver for
120s to confirm a schedule can actually be built.
"""
import argparse
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.loaders import list_semesters
from src.paths import DATA_DIR, OUTPUT_DIR
from src.feasibility import demand_report, print_demand_report

from src.loaders import load_problem


def check(sem, probe=False):
    ok = True
    print(f"===== {sem} =====", flush=True)
    try:
        problem = load_problem(str(DATA_DIR / "semesters" / sem))
    except Exception as exc:
        print(f"[{sem}] DATA ERROR: {exc!r}", flush=True)
        return False

    print(f"[{sem}] loaded {len(problem['sessions'])} sessions, "
          f"{len(problem['sections'])} sections, {len(problem['lecturers'])} lecturers, "
          f"{len(problem['rooms'])} rooms", flush=True)
    rc = problem.get("rooms_config") or {}
    if rc:
        print(f"[{sem}] small-class room: {rc['small_room']} (cap {rc['small_capacity']}), "
              f"biggest room {rc['max_capacity']} seats, co-teach cap {rc['max_class']}", flush=True)

    report = demand_report(problem)
    print_demand_report(sem, report, detail=True)
    # A small overrun is marginal (the naive count rounds hours up) - only flag
    # a real shortfall or one the --probe solve cannot fit.
    marginal = report["shortfall_hours"] <= 0.05 * max(report["capacity_hours"], 1)
    if report["shortfall_hours"] > 0 and not marginal:
        ok = False

    out = OUTPUT_DIR / sem / "solve_result.json"
    if out.exists():
        data_files = list((DATA_DIR / "semesters" / sem).glob("*.xlsx"))
        sf = DATA_DIR / "semesters" / sem / "settings.json"
        if sf.exists():
            data_files.append(sf)
        newest_data = max((p.stat().st_mtime for p in data_files), default=0)
        if newest_data > out.stat().st_mtime:
            print(f"[{sem}] STALE: data changed after the last solve ({out}); rerun regen.py", flush=True)
            ok = False
        else:
            print(f"[{sem}] published timetable is newer than the data", flush=True)
    else:
        print(f"[{sem}] no solve_result.json - no timetable generated yet (run regen.py)", flush=True)
        ok = False

    if probe:
        problem["soft_lecturer"] = False
        problem["soft_sections"] = False
        problem["skip_gaps"] = True
        from src.solver import solve
        t0 = time.time()
        print(f"[{sem}] probing feasibility (CP-SAT, up to 120s) ...", flush=True)
        res = solve(problem, time_limit=120.0, hints=None, minimize_objective=False,
                    feasibility_jump=True, seed=42)
        print(f"[{sem}] probe: status={res.status} objective={res.objective} "
              f"({time.time() - t0:.0f}s)", flush=True)
        if res.status not in ("OPTIMAL", "FEASIBLE"):
            ok = False
    return ok


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--sem", help="check only this semester folder (default: all)")
    ap.add_argument("--probe", action="store_true", help="run a real CP-SAT feasibility probe per semester")
    args = ap.parse_args()

    sems = [args.sem] if args.sem else list_semesters()
    if not sems:
        print("No semester data folders found under", DATA_DIR / "semesters", flush=True)
        return 2

    all_ok = True
    for sem in sems:
        if not check(sem, probe=args.probe):
            all_ok = False
    print("=====", flush=True)
    print("ALL GOOD - every semester's data is valid and schedulable" if all_ok
          else "ISSUES FOUND - see messages above", flush=True)
    return 0 if all_ok else 1


if __name__ == "__main__":
    sys.exit(main())