"""Demand-vs-capacity feasibility diagnostics.

Run before solving so a semester whose courses no longer fit the room stock
fails fast with an explanation instead of a silent CP-SAT timeout. Also used
by tools/healthcheck.py so adding courses or rooms can be checked with one
command.

Note on interpretation: a course taught several times a week is one model
session per weekly meeting, and each session occupies `duration` slots, so the
demand count (sum of durations) is the exact room-hours needed - comparable
1:1 with the room capacity (slots x rooms). A shortfall of a few hours is
marginal (the packers can usually still fit it); a shortfall of tens of hours
means the data needs more rooms or more co-teaching.
"""

from .slots import DAYS, N_SLOTS, SLOTS_PER_DAY

# Saturday is reserved for ONLINE (RT) sessions; physical classes can only use
# the Mon-Fri slots, so room capacity for physical classes excludes it.
PHYSICAL_SLOTS = N_SLOTS - SLOTS_PER_DAY


def session_hours(s):
    # A session occupies `duration` consecutive slots in its room each week.
    # sessions_per_week is already materialised as one model session per weekly
    # meeting, so the real room-hours needed is just the session's duration.
    return s.duration


def demand_report(problem):
    rooms = problem["rooms"]
    real = [r for r in rooms]
    phys = [s for s in problem["sessions"] if not s.online and not s.field_work]
    total_hours = sum(session_hours(s) for s in phys)
    total_capacity = PHYSICAL_SLOTS * len(real)
    lab = [s for s in phys if s.course.practical_hours > 0]
    lab_hours = sum(session_hours(s) for s in lab)
    lab_capacity = PHYSICAL_SLOTS * sum(1 for r in real if r.kind == "lab")
    lecture_hours = total_hours - lab_hours
    lecture_capacity = PHYSICAL_SLOTS * sum(1 for r in real if r.kind == "lecture")
    top = sorted(phys, key=session_hours, reverse=True)[:10]
    by_room = []
    for r in sorted(real, key=lambda r: -r.capacity):
        by_room.append((r.name, r.capacity, r.kind))
    return {
        "physical_sessions": len(phys),
        "total_sessions": len(problem["sessions"]),
        "demand_hours": total_hours,
        "capacity_hours": total_capacity,
        "shortfall_hours": total_hours - total_capacity,
        "lecture": {"demand_hours": lecture_hours, "capacity_hours": lecture_capacity},
        "lab": {"demand_hours": lab_hours, "capacity_hours": lab_capacity},
        "rooms": by_room,
        "top_demand": [(s.id, session_hours(s), s.size) for s in top],
    }


def format_report(sem, report, detail=True):
    lines = [
        f"[{sem}] demand: {report['demand_hours']}h/week of physical classes vs "
        f"{report['capacity_hours']}h/week of room capacity "
        f"({report['physical_sessions']} physical sessions of {report['total_sessions']} total)",
    ]
    lec = report["lecture"]
    lab = report["lab"]
    lines.append(
        f"[{sem}]   lecture rooms: {lec['demand_hours']}h demanded / {lec['capacity_hours']}h available"
    )
    if lab["capacity_hours"]:
        lines.append(
            f"[{sem}]   lab rooms:     {lab['demand_hours']}h demanded / {lab['capacity_hours']}h available"
            f" (excess can fall back to lecture rooms)"
        )
    if report["shortfall_hours"] > 0:
        pct = report["shortfall_hours"] / max(report["capacity_hours"], 1) * 100
        lines.append(
            f"[{sem}]   WARNING: demand exceeds capacity by "
            f"{report['shortfall_hours']}h/week ({pct:.0f}%). "
            f"{'Small overruns can still pack (rooms are nearly full), so this is marginal.' if pct < 5 else 'Large overruns need more rooms or more co-teaching.'}"
        )
    else:
        lines.append(f"[{sem}]   demand fits within room capacity (slack {abs(report['shortfall_hours'])}h/week)")
    if detail:
        lines.append(f"[{sem}]   rooms: " + ", ".join(f"{n}({c}{' lab' if k=='lab' else ''})" for n, c, k in report["rooms"]))
        lines.append(f"[{sem}]   biggest demand: " + "; ".join(f"{sid} {h}h" for sid, h, _ in report["top_demand"]))
    return "\n".join(lines)


def print_demand_report(sem, report, detail=True):
    print(format_report(sem, report, detail=detail), flush=True)