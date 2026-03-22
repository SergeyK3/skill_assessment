# route: (integration) | file: skill_assessment/integration/hr_core.py
"""
In-process интеграция с HR ядра (typical_infrastructure).

Ожидаемый контракт в ядре (когда появится модуль ``app.hr``)::

    def get_employee(db: Session, client_id: str, employee_id: str) -> Any | None:
        ...

Возвращаемое значение может быть ORM-моделью или DTO; ниже приводится к
:class:`EmployeeSnapshot`. До появления ``app.hr`` используются заглушки
(без ошибок в рантайме при отсутствии ядра).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

from sqlalchemy.orm import Session

_log = logging.getLogger(__name__)

__all__ = [
    "CORE_HR_AVAILABLE",
    "EmployeeSnapshot",
    "employee_display_label",
    "get_employee",
]

_core_get_employee: Any = None
CORE_HR_AVAILABLE: bool = False

try:
    from app.hr import get_employee as _core_get_employee  # type: ignore[import-untyped,import-not-found]

    CORE_HR_AVAILABLE = True
    _log.debug("skill_assessment: подключён app.hr.get_employee (ядро HR)")
except ImportError:
    _log.info(
        "skill_assessment: модуль app.hr не найден — HR-интеграция в режиме заглушки "
        "(ожидается in-process API ядра: app.hr.get_employee)."
    )


@dataclass(frozen=True)
class EmployeeSnapshot:
    """Минимальный срез сотрудника для UI и отчётов плагина."""

    id: str
    client_id: str
    display_name: str | None = None
    email: str | None = None


def _stub_employee(client_id: str, employee_id: str) -> EmployeeSnapshot:
    """Пока нет ядра HR — только идентификаторы без ФИО."""
    return EmployeeSnapshot(id=employee_id, client_id=client_id, display_name=None, email=None)


def _adapt_core_employee(obj: Any, client_id: str, employee_id: str) -> EmployeeSnapshot | None:
    """Преобразует ответ ядра в :class:`EmployeeSnapshot` (duck typing)."""
    if obj is None:
        return None
    if isinstance(obj, EmployeeSnapshot):
        return obj

    eid = getattr(obj, "id", None) or getattr(obj, "employee_id", None) or employee_id
    cid = getattr(obj, "client_id", None) or client_id
    display = (
        getattr(obj, "display_name", None)
        or getattr(obj, "full_name", None)
        or getattr(obj, "name", None)
    )
    if not (isinstance(display, str) and display.strip()):
        ln = getattr(obj, "last_name", None)
        fn = getattr(obj, "first_name", None)
        mn = getattr(obj, "middle_name", None)
        if ln or fn or mn:
            parts = [p for p in (ln, fn, mn) if isinstance(p, str) and p.strip()]
            display = " ".join(parts) if parts else None
    email = getattr(obj, "email", None)
    if isinstance(display, str) and not display.strip():
        display = None
    if isinstance(email, str) and not email.strip():
        email = None

    return EmployeeSnapshot(
        id=str(eid),
        client_id=str(cid),
        display_name=display if isinstance(display, str) else None,
        email=email if isinstance(email, str) else None,
    )


def get_employee(db: Session, client_id: str, employee_id: str | None) -> EmployeeSnapshot | None:
    """
    Возвращает срез данных сотрудника для данного клиента.

    При отсутствии ``employee_id`` — ``None``. При отсутствии ``app.hr`` —
    заглушка с тем же ``employee_id`` (без ФИО). Если ядро вернуло ``None``,
    сотрудник не найден — тоже ``None``.
    """
    if not employee_id:
        return None

    if CORE_HR_AVAILABLE and _core_get_employee is not None:
        try:
            raw = _core_get_employee(db, client_id, employee_id)
            if raw is None:
                return None
            adapted = _adapt_core_employee(raw, client_id, employee_id)
            return adapted if adapted is not None else None
        except Exception:
            _log.exception(
                "skill_assessment: app.hr.get_employee завершился с ошибкой — fallback на заглушку"
            )

    return _stub_employee(client_id, employee_id)


def employee_display_label(snapshot: EmployeeSnapshot | None) -> str | None:
    """Строка для подписей в отчётах: ФИО / email / id."""
    if snapshot is None:
        return None
    if snapshot.display_name and snapshot.display_name.strip():
        return snapshot.display_name.strip()
    if snapshot.email and snapshot.email.strip():
        return snapshot.email.strip()
    return snapshot.id
