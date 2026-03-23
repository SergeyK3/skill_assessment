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
from dataclasses import dataclass, replace
from typing import Any

from sqlalchemy.orm import Session

_log = logging.getLogger(__name__)

__all__ = [
    "CORE_HR_AVAILABLE",
    "EmployeeSnapshot",
    "employee_display_label",
    "employee_greeting_label",
    "get_employee",
]

_core_get_employee: Any = None
_core_get_position: Any = None
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

try:
    from app.hr import get_position as _core_get_position  # type: ignore[import-untyped,import-not-found]

    _log.debug("skill_assessment: подключён app.hr.get_position")
except ImportError:
    _core_get_position = None


@dataclass(frozen=True)
class EmployeeSnapshot:
    """Минимальный срез сотрудника для UI и отчётов плагина."""

    id: str
    client_id: str
    display_name: str | None = None
    email: str | None = None
    position_label: str | None = None
    telegram_chat_id: str | None = None
    first_name: str | None = None
    middle_name: str | None = None


def _stub_employee(client_id: str, employee_id: str) -> EmployeeSnapshot:
    """Пока нет ядра HR — только идентификаторы без ФИО."""
    return EmployeeSnapshot(
        id=employee_id,
        client_id=client_id,
        display_name=None,
        email=None,
        position_label=None,
        telegram_chat_id=None,
        first_name=None,
        middle_name=None,
    )


def _label_from_position_object(pos: Any) -> str | None:
    if pos is None:
        return None
    if isinstance(pos, str) and pos.strip():
        return pos.strip()
    for attr in ("name", "title", "label", "display_name", "code"):
        v = getattr(pos, attr, None)
        if isinstance(v, str) and v.strip():
            return v.strip()
    return None


def _adapt_core_employee(obj: Any, client_id: str, employee_id: str) -> EmployeeSnapshot | None:
    """Преобразует ответ ядра в :class:`EmployeeSnapshot` (duck typing)."""
    if obj is None:
        return None
    if isinstance(obj, EmployeeSnapshot):
        return obj

    eid = getattr(obj, "id", None) or getattr(obj, "employee_id", None) or employee_id
    cid = getattr(obj, "client_id", None) or client_id
    ln = getattr(obj, "last_name", None)
    fn = getattr(obj, "first_name", None)
    mn = getattr(obj, "middle_name", None) or getattr(obj, "patronymic", None) or getattr(obj, "patronymic_name", None)
    display = (
        getattr(obj, "display_name", None)
        or getattr(obj, "full_name", None)
        or getattr(obj, "name", None)
    )
    if not (isinstance(display, str) and display.strip()):
        if ln or fn or mn:
            parts = [p for p in (ln, fn, mn) if isinstance(p, str) and p.strip()]
            display = " ".join(parts) if parts else None
    email = getattr(obj, "email", None)
    if isinstance(display, str) and not display.strip():
        display = None
    if isinstance(email, str) and not email.strip():
        email = None

    position_label: str | None = None
    pt = getattr(obj, "position_title", None) or getattr(obj, "position_name", None)
    if isinstance(pt, str) and pt.strip():
        position_label = pt.strip()
    else:
        pos = getattr(obj, "position", None)
        if pos is not None:
            if isinstance(pos, str) and pos.strip():
                position_label = pos.strip()
            else:
                pn = getattr(pos, "name", None) or getattr(pos, "title", None)
                if isinstance(pn, str) and pn.strip():
                    position_label = pn.strip()
    if not position_label:
        jt = getattr(obj, "job_title", None) or getattr(obj, "role_title", None)
        if isinstance(jt, str) and jt.strip():
            position_label = jt.strip()

    def _s(x: Any) -> str | None:
        return x.strip() if isinstance(x, str) and x.strip() else None

    first_name_s = _s(fn)
    middle_name_s = _s(mn)

    tg_raw = getattr(obj, "telegram_chat_id", None) or getattr(obj, "telegram_id", None) or getattr(obj, "tg_id", None)
    telegram_chat_id: str | None = None
    if tg_raw is not None:
        ts = str(tg_raw).strip()
        if ts:
            telegram_chat_id = ts

    return EmployeeSnapshot(
        id=str(eid),
        client_id=str(cid),
        display_name=display if isinstance(display, str) else None,
        email=email if isinstance(email, str) else None,
        position_label=position_label,
        telegram_chat_id=telegram_chat_id,
        first_name=first_name_s,
        middle_name=middle_name_s,
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
            if adapted is None:
                return None
            if not adapted.position_label and _core_get_position is not None:
                pid = getattr(raw, "position_id", None)
                if pid is not None:
                    try:
                        pos = _core_get_position(db, client_id, str(pid))
                        pl = _label_from_position_object(pos)
                        if pl:
                            adapted = replace(adapted, position_label=pl)
                    except Exception:
                        _log.debug("skill_assessment: get_position не дал название должности", exc_info=True)
            return adapted
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


def employee_greeting_label(snapshot: EmployeeSnapshot | None) -> str | None:
    """Обращение в уведомлениях: «Имя Отчество» при наличии данных; иначе ФИО / эвристика по display_name."""
    if snapshot is None:
        return None
    fn = (snapshot.first_name or "").strip()
    mn = (snapshot.middle_name or "").strip()
    if fn and mn:
        return f"{fn} {mn}"
    if fn:
        return fn
    dn = (snapshot.display_name or "").strip()
    parts = dn.split()
    if len(parts) >= 3:
        return f"{parts[1]} {parts[2]}"
    if len(parts) == 2:
        return parts[0]
    return employee_display_label(snapshot)
