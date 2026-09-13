from __future__ import annotations

from datetime import datetime, timedelta, timezone

# Display timezone: UTC+7. Never mention the offset to the user.
DISPLAY_TZ = timezone(timedelta(hours=7))


def to_display(dt: datetime | None) -> datetime | None:
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(DISPLAY_TZ)


def format_dt(dt: datetime | None, fmt: str = "%Y-%m-%d %H:%M") -> str:
    local = to_display(dt)
    if local is None:
        return "?"
    return local.strftime(fmt)


def now_display() -> datetime:
    return datetime.now(DISPLAY_TZ)


def parse_user_dt(text: str) -> datetime:
    """Parse user datetime in display TZ (UTC+7) → UTC."""
    text = text.strip()
    for fmt in ("%Y-%m-%d %H:%M", "%Y-%m-%d %H:%M:%S", "%Y-%m-%d", "%d.%m.%Y %H:%M", "%d.%m.%Y"):
        try:
            naive = datetime.strptime(text, fmt)
            return naive.replace(tzinfo=DISPLAY_TZ).astimezone(timezone.utc)
        except ValueError:
            continue
    raise ValueError(f"Не удалось распознать дату: {text}")
