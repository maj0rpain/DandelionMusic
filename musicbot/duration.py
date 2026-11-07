from typing import Optional


def whole_seconds(value: Optional[float]) -> Optional[int]:
    """A length in seconds as whole seconds. A missing length, or one
    that is exactly zero, is unknown (None)."""
    return round(value) if value else None
