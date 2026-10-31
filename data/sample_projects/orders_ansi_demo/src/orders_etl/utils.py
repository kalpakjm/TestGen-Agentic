"""Small pure-Python helpers."""


def safe_divide(numerator: float, denominator: float) -> float:
    """Divide, returning 0.0 when the denominator is zero."""
    if denominator == 0:
        return 0.0
    return numerator / denominator


def normalize_country(code: str) -> str:
    """Upper-case and trim an ISO country code."""
    return code.strip().upper()


def is_weekend(day_of_week: int) -> bool:
    """True for Saturday (6) and Sunday (7)."""
    return day_of_week in (6, 7)
