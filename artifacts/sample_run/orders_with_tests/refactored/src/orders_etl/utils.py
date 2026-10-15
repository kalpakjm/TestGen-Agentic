"""Small pure-Python helpers."""


def safe_divide(numerator: float, denominator: float) -> float:
    """Divide, returning 0.0 when the denominator is zero."""
    if denominator == 0:
        return 0.0
    return numerator / denominator
