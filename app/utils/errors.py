from __future__ import annotations


def friendly_error(exc: BaseException) -> str:
    """Convert any exception into a short user-facing Russian message."""
    if isinstance(exc, ValueError):
        msg = str(exc).strip()
        return msg or "Некорректные данные."

    name = type(exc).__name__
    raw = str(exc).strip()

    lower = raw.lower()
    if "does not have market symbol" in lower or "symbol" in lower and "not" in lower:
        return "Пара не найдена на выбранной бирже."
    if "rate limit" in lower or "too many requests" in lower:
        return "Биржа временно ограничила запросы. Попробуйте чуть позже."
    if "network" in lower or "timeout" in lower or "connection" in lower:
        return "Нет связи с биржей. Попробуйте позже."
    if "blank-out primary key" in lower or "dependency rule" in lower:
        return "Не удалось удалить запись. Попробуйте ещё раз."
    if "unique" in lower and "constraint" in lower:
        return "Такая пара уже есть в списке отслеживания."

    if raw and len(raw) < 180 and "Traceback" not in raw:
        return f"Ошибка: {raw}"
    return f"Произошла ошибка ({name}). Попробуйте ещё раз или измените запрос."
