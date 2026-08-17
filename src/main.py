import argparse
import sys
from pathlib import Path

from .export import export_all
from .loaders import load_problem
from .sample_data import generate_sample_data
from .solver import solve
from .templates import generate_templates

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_DATA = ROOT / "data"
DEFAULT_OUTPUT = ROOT / "output"


def _summary(problem, result):
    lines = []
    lines.append(f"Sessions to schedule : {len(problem['sessions'])}")
    lines.append(f"Cohort sections      : {len(problem['sections'])}")
    lines.append(f"Lecturers            : {len(problem['lecturers'])}")
    lines.append(f"Rooms                : {len(problem['rooms'])}")
    lines.append(f"Solver status        : {result.status}")
    lines.append(f"Objective value      : {result.objective:.0f}")
    for key, count in result.checks.items():
        lines.append(f"Conflicts ({key})     : {count}")
    return "\n".join(lines)


def main(argv=None):
    parser = argparse.ArgumentParser(description="UMaT-SRID course timetable solver")
    parser.add_argument("--sample", action="store_true", help="regenerate sample data files first")
    parser.add_argument("--templates", action="store_true", help="write blank templates to fill with real data")
    parser.add_argument("--data-dir", default=str(DEFAULT_DATA), help="folder with rooms.xlsx, cohorts.xlsx, courses.xlsx")
    parser.add_argument("--output-dir", default=str(DEFAULT_OUTPUT), help="folder for generated timetables")
    parser.add_argument("--time-limit", type=float, default=30.0, help="max solve time in seconds")
    args = parser.parse_args(argv)

    data_dir = Path(args.data_dir)
    if args.sample:
        generate_sample_data(data_dir)
        print(f"Sample data written to {data_dir}")
    if args.templates:
        generate_templates(data_dir)
        print(f"Blank templates written to {data_dir}")
        if not args.sample:
            return 0

    problem = load_problem(data_dir)
    print("Loaded problem:")
    print(f"  {len(problem['sessions'])} sessions, {len(problem['sections'])} cohort sections, {len(problem['lecturers'])} lecturers, {len(problem['rooms'])} rooms")

    result = solve(problem, time_limit=args.time_limit)
    print(_summary(problem, result))

    if result.status == "INFEASIBLE":
        print("No valid timetable found. Relax constraints or add room capacity.")
        return 1

    out_path = export_all(problem, result, Path(args.output_dir))
    print(f"Timetable written to {out_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
