# Saturday is reserved for ONLINE sessions only (e.g. the Railway Technology
# courses). Physical classes are never scheduled on Saturday.
DAYS = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday"]

# 1-hour teaching slots, Mon-Fri. The 12:30-13:00 lunch break is not a slot.
# Slot index 10 (17:00-18:00) and 11 (18:00-19:00) count as evening.
SLOT_TIMES = [
    "06:30-07:30",
    "07:30-08:30",
    "08:30-09:30",
    "09:30-10:30",
    "10:30-11:30",
    "11:30-12:30",
    "13:00-14:00",
    "14:00-15:00",
    "15:00-16:00",
    "16:00-17:00",
    "17:00-18:00",
    "18:00-19:00",
]

EVENING_START = 10

# Field-work sessions (Fieldtrip, Engineering Practice, Survey Camp) run off
# campus, so they are only allowed to start in the middle of the day: never at
# the early 06:30/07:30 blocks and never in the evening.
FIELD_WORK_START_MIN = 2   # 08:30-09:30
FIELD_WORK_START_MAX = 9   # 16:00-17:00 (a 2h session ends by 18:00; 1h by 17:00)

SLOTS_PER_DAY = len(SLOT_TIMES)
N_SLOTS = len(DAYS) * SLOTS_PER_DAY


def day_index_of(slot):
    return slot // SLOTS_PER_DAY


def slot_in_day(slot):
    return slot % SLOTS_PER_DAY


def day_name(slot):
    return DAYS[day_index_of(slot)]


def slot_time(slot):
    return SLOT_TIMES[slot_in_day(slot)]


def slot_label(slot):
    return f"{day_name(slot)} {slot_time(slot)}"
