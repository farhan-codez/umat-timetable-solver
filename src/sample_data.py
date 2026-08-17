from pathlib import Path

import pandas as pd

ROOMS = [
    ("SR1", 40),
    ("SR2", 40),
    ("SR3", 40),
    ("SR4", 40),
    ("SR5A", 80),
    ("SR5B", 80),
    ("SR6A", 80),
    ("SR6B", 80),
    ("SR7A", 80),
    ("LH1", 120),
    ("LH2", 120),
    ("LH3", 120),
    ("Auditorium", 120),
]

PROGRAMMES = [
    ("CE", "Computer Engineering"),
    ("EE", "Electrical & Electronic Engineering"),
    ("ME", "Mechanical Engineering"),
]

LEVELS = [100, 200]

COMMON = {
    100: [
        ("MA 121", "Engineering Mathematics I", 2, 0, 2, "Prof. A. Owusu"),
        ("GNS 101", "Communication Skills", 2, 0, 2, "Mrs. A. Frimpong"),
    ],
    200: [
        ("MA 221", "Engineering Mathematics II", 2, 0, 2, "Dr. T. Acquah"),
        ("GNS 205", "Academic Writing", 2, 0, 2, "Mr. D. Opoku"),
    ],
}

PROGRAM_COURSES = {
    "CE": {
        100: [
            ("CE 101", "Introduction to Programming & Lab", 2, 2, 3, "Dr. K. Mensah"),
            ("CE 107", "Digital Logic & Lab", 2, 2, 3, "Ing. E. Boateng"),
        ],
        200: [
            ("CE 201", "Data Structures & Algorithms", 2, 2, 3, "Dr. S. Appiah"),
            ("CE 203", "Computer Organisation", 2, 0, 2, "Dr. K. Mensah"),
        ],
    },
    "EE": {
        100: [
            ("EE 101", "Circuit Theory & Lab", 2, 2, 3, "Prof. B. Danquah"),
            ("EE 107", "Electrical Measurements & Lab", 2, 2, 3, "Dr. F. Amoako"),
        ],
        200: [
            ("EE 201", "Signals & Systems", 2, 2, 3, "Dr. C. Asante"),
            ("EE 203", "Electrical Machines I", 2, 2, 3, "Prof. B. Danquah"),
        ],
    },
    "ME": {
        100: [
            ("ME 101", "Engineering Mechanics", 2, 2, 3, "Dr. G. Nyarko"),
            ("ME 107", "Thermodynamics I", 2, 2, 3, "Ing. H. Wireko"),
        ],
        200: [
            ("ME 201", "Strength of Materials", 2, 2, 3, "Prof. I. Agyeman"),
            ("ME 203", "Fluid Mechanics I", 2, 0, 2, "Mrs. J. Obiri"),
        ],
    },
}

COMBINED_CAMPUS = {
    "CE": ("CE 113", "Computer Aided Design & Graphics", "Dr. S. Appiah"),
    "EE": ("EE 113", "Engineering Drawing", "Mr. D. Opoku"),
    "ME": ("ME 113", "Engineering Drawing", "Ing. H. Wireko"),
}

COHORT_SIZE_A = 58
COHORT_SIZE_B = 58


def _build_cohorts():
    rows = []
    for prog, _ in PROGRAMMES:
        for level in LEVELS:
            rows.append({"programme": prog, "level": level, "section": "A", "size": COHORT_SIZE_A})
            rows.append({"programme": prog, "level": level, "section": "B", "size": COHORT_SIZE_B})
    return rows


def _build_courses():
    rows = []
    for level in LEVELS:
        for prog, _ in PROGRAMMES:
            for code, name, lec, prac, cred, lecturer in COMMON[level]:
                rows.append(
                    {
                        "course_code": code,
                        "course_name": name,
                        "programme": prog,
                        "level": level,
                        "cohort": "AB",
                        "lecturer": lecturer,
                        "lecture_hours": lec,
                        "practical_hours": prac,
                        "credits": cred,
                        "online": "yes",
                        "sessions_per_week": None,
                        "min_room_size": None,
                    }
                )
            for code, name, lec, prac, cred, lecturer in PROGRAM_COURSES[prog][level]:
                for cohort in ("A", "B"):
                    rows.append(
                        {
                            "course_code": code,
                            "course_name": name,
                            "programme": prog,
                            "level": level,
                            "cohort": cohort,
                            "lecturer": lecturer,
                            "lecture_hours": lec,
                            "practical_hours": prac,
                            "credits": cred,
                            "online": "no",
                            "sessions_per_week": None,
                            "min_room_size": None,
                        }
                    )
            code, name, lecturer = COMBINED_CAMPUS[prog]
            rows.append(
                {
                    "course_code": code,
                    "course_name": name,
                    "programme": prog,
                    "level": level,
                    "cohort": "AB",
                    "lecturer": lecturer,
                    "lecture_hours": 2,
                    "practical_hours": 2,
                    "credits": 3,
                    "online": "no",
                    "sessions_per_week": None,
                    "min_room_size": None,
                }
            )
    return rows


def generate_sample_data(data_dir):
    data_dir = Path(data_dir)
    data_dir.mkdir(parents=True, exist_ok=True)

    rooms_df = pd.DataFrame([{"name": n, "capacity": c, "kind": "lecture"} for n, c in ROOMS])
    rooms_df.to_excel(data_dir / "rooms.xlsx", index=False)

    cohorts_df = pd.DataFrame(_build_cohorts())
    cohorts_df.to_excel(data_dir / "cohorts.xlsx", index=False)

    courses_df = pd.DataFrame(_build_courses())
    courses_df.to_excel(data_dir / "courses.xlsx", index=False)

    settings = {
        "weights": {
            "room_oversize": 2,
            "evening": 4,
            "cohort_gap": 10,
            "lecturer_gap": 6,
        }
    }
    (data_dir / "settings.json").write_text(
        __import__("json").dumps(settings, indent=2), encoding="utf-8"
    )
