from dataclasses import dataclass


@dataclass
class Room:
    name: str
    capacity: int
    kind: str = "lecture"


@dataclass
class Cohort:
    programme: str
    level: int
    section: str
    size: int

    @property
    def id(self):
        return f"{self.programme}{self.level}-{self.section}"

    @property
    def label(self):
        return f"{self.programme}{self.level} {self.section}"


@dataclass
class Course:
    code: str
    name: str
    programme: str
    level: int
    cohort: str
    lecturer: str
    lecture_hours: float
    practical_hours: float
    credits: float
    online: bool
    sessions_per_week: int
    min_capacity: int
    field_work: bool = False
    seq: int = 0  # unique per course row; keeps session ids distinct across duplicate rows


@dataclass
class Session:
    course: Course
    index: int
    size: int
    sections: set
    duration: int = 2  # hours (number of 1-hour slots this class occupies)
    online: bool = False
    field_work: bool = False

    @property
    def id(self):
        c = self.course
        return f"{c.code}-{c.programme}{c.level}-{c.cohort}#{c.seq}.{self.index}"

    @property
    def cohort_label(self):
        return f"{self.course.programme}{self.course.level} {self.course.cohort}"
