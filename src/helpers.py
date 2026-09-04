"""Shared helpers for value normalisation used across loaders, web, and export."""


def _clean(value):
    """Normalise a cell value: NaN/None → '', bool → 'yes'/'no', float → int
    where possible, everything else → stripped str."""
    if value is None:
        return ""
    if isinstance(value, float):
        if value != value:  # NaN
            return ""
        return int(value) if value.is_integer() else value
    if isinstance(value, bool):
        return "yes" if value else "no"
    return str(value).strip()


def _num(value):
    """Try to return an integer from a value; return 0 on failure."""
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return 0


def _truthy(value):
    """Return True if *value* looks like a truthy/online flag."""
    if value is None:
        return False
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in ("yes", "y", "1", "true", "t")
