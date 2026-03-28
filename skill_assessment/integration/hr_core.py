# route: (integration) | file: skill_assessment/integration/hr_core.py
"""
In-process интеграция с HR ядра (typical_infrastructure).

Ожидаемый контракт в ядре (когда появится модуль ``app.hr``)::

    def get_employee(db: Session, client_id: str, employee_id: str) -> Any | None:
        ...

Возвращаемое значение может быть ORM-моделью или DTO; ниже приводится к
:class:`EmployeeSnapshot`. До появления ``app.hr`` используются заглушки
(без ошибок в рантайме при отсутствии ядра).

Для экзамена по регламентам ядро может реализовать
``get_examination_question_texts(db, client_id, employee_id)`` — список формулировок
по KPI и регламентам, привязанным к должности и подразделению (см. ``hr_core.get_examination_question_texts``).
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, replace
from typing import Any

from sqlalchemy.orm import Session

_log = logging.getLogger(__name__)

__all__ = [
    "CORE_HR_AVAILABLE",
    "EmployeeSnapshot",
    "department_function_code_label_ru",
    "get_assessment_case_count",
    "employee_display_label",
    "employee_greeting_label",
    "get_employee",
    "get_examination_instructions_folder_url",
    "get_examination_kpi_labels",
    "get_examination_regulation_reference_text",
    "get_examination_question_texts",
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

_core_get_examination_question_texts: Any = None
try:
    from app.hr import get_examination_question_texts as _core_get_examination_question_texts  # type: ignore[import-untyped,import-not-found]

    _log.debug("skill_assessment: подключён app.hr.get_examination_question_texts")
except ImportError:
    pass

_core_get_examination_instructions_folder_url: Any = None
try:
    from app.hr import get_examination_instructions_folder_url as _core_get_examination_instructions_folder_url  # type: ignore[import-untyped,import-not-found]

    _log.debug("skill_assessment: подключён app.hr.get_examination_instructions_folder_url")
except ImportError:
    pass

_core_get_examination_regulation_reference_text: Any = None
try:
    from app.hr import get_examination_regulation_reference_text as _core_get_examination_regulation_reference_text  # type: ignore[import-untyped,import-not-found]

    _log.debug("skill_assessment: подключён app.hr.get_examination_regulation_reference_text")
except ImportError:
    pass

_core_get_examination_kpi_labels: Any = None
try:
    from app.hr import get_examination_kpi_labels as _core_get_examination_kpi_labels  # type: ignore[import-untyped,import-not-found]

    _log.debug("skill_assessment: подключён app.hr.get_examination_kpi_labels")
except ImportError:
    pass

_core_get_assessment_case_count: Any = None
try:
    from app.hr import get_assessment_case_count as _core_get_assessment_case_count  # type: ignore[import-untyped,import-not-found]

    _log.debug("skill_assessment: подключён app.hr.get_assessment_case_count")
except ImportError:
    pass


@dataclass(frozen=True)
class EmployeeSnapshot:
    """Минимальный срез сотрудника для UI и отчётов плагина."""

    id: str
    client_id: str
    display_name: str | None = None
    email: str | None = None
    position_label: str | None = None
    telegram_chat_id: str | None = None
    last_name: str | None = None
    first_name: str | None = None
    middle_name: str | None = None
    #: Непосредственный руководитель (employee_id), если ядро HR отдаёт связь.
    manager_employee_id: str | None = None
    #: Подразделение (для поиска руководителя отдела).
    org_unit_id: str | None = None
    #: Код должности в каталоге (матрицы KPI / ключевых навыков), если ядро отдаёт.
    position_code: str | None = None
    #: Код функции подразделения (ACC, HR, SALES, …) для матриц, если ядро отдаёт.
    department_code: str | None = None


# Коды функции подразделения (как в sa_competency_matrix.department_code) → подпись в отчётах на русском.
_DEPARTMENT_FUNCTION_CODE_RU: dict[str, str] = {
    "SALES": "Отдел продаж",
    "HR": "Отдел кадров",
    "ACC": "Бухгалтерия",
    "IT": "ИТ-служба",
    "MARKETING": "Маркетинг",
    "LEGAL": "Юридический отдел",
    "OPS": "Операционный блок",
    "GENERAL": "Общий блок",
    "FINANCE": "Финансовый отдел",
    "PROCUREMENT": "Отдел закупок",
    "LOGISTICS": "Логистика",
    "PRODUCTION": "Производство",
    "RND": "НИОКР",
    "R&D": "НИОКР",
}


def department_function_code_label_ru(code: str | None) -> str | None:
    """Русское название подразделения по коду функции (например ``SALES`` → «Отдел продаж»)."""
    if not code or not isinstance(code, str):
        return None
    key = code.strip().upper()
    if not key:
        return None
    return _DEPARTMENT_FUNCTION_CODE_RU.get(key)


def _stub_employee(client_id: str, employee_id: str) -> EmployeeSnapshot:
    """Пока нет ядра HR — только идентификаторы без ФИО."""
    return EmployeeSnapshot(
        id=employee_id,
        client_id=client_id,
        display_name=None,
        email=None,
        position_label=None,
        telegram_chat_id=None,
        last_name=None,
        first_name=None,
        middle_name=None,
        manager_employee_id=None,
        org_unit_id=None,
        position_code=None,
        department_code=None,
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

    last_name_s = _s(ln)
    first_name_s = _s(fn)
    middle_name_s = _s(mn)

    tg_raw = getattr(obj, "telegram_chat_id", None) or getattr(obj, "telegram_id", None) or getattr(obj, "tg_id", None)
    telegram_chat_id: str | None = None
    if tg_raw is not None:
        ts = str(tg_raw).strip()
        if ts:
            telegram_chat_id = ts

    mgr_raw = (
        getattr(obj, "manager_id", None)
        or getattr(obj, "manager_employee_id", None)
        or getattr(obj, "reports_to_employee_id", None)
        or getattr(obj, "supervisor_id", None)
        or getattr(obj, "parent_employee_id", None)
    )
    manager_employee_id: str | None = None
    if mgr_raw is not None:
        ms = str(mgr_raw).strip()
        manager_employee_id = ms or None

    ou_raw = getattr(obj, "org_unit_id", None) or getattr(obj, "department_id", None) or getattr(obj, "unit_id", None)
    org_unit_id: str | None = None
    if ou_raw is not None:
        os_ = str(ou_raw).strip()
        org_unit_id = os_ or None

    position_code: str | None = None
    pc_raw = getattr(obj, "position_code", None)
    if isinstance(pc_raw, str) and pc_raw.strip():
        position_code = pc_raw.strip()
    pos_obj = getattr(obj, "position", None)
    if not position_code and pos_obj is not None:
        for attr in ("code", "position_code"):
            v = getattr(pos_obj, attr, None)
            if isinstance(v, str) and v.strip():
                position_code = v.strip()
                break

    department_code: str | None = None
    for key in ("department_code", "function_code", "org_function_code", "dept_function_code"):
        dc_raw = getattr(obj, key, None)
        if isinstance(dc_raw, str) and dc_raw.strip():
            department_code = dc_raw.strip()
            break

    return EmployeeSnapshot(
        id=str(eid),
        client_id=str(cid),
        display_name=display if isinstance(display, str) else None,
        email=email if isinstance(email, str) else None,
        position_label=position_label,
        telegram_chat_id=telegram_chat_id,
        last_name=last_name_s,
        first_name=first_name_s,
        middle_name=middle_name_s,
        manager_employee_id=manager_employee_id,
        org_unit_id=org_unit_id,
        position_code=position_code,
        department_code=department_code,
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
            if _core_get_position is not None:
                pid = getattr(raw, "position_id", None)
                if pid is not None:
                    try:
                        pos = _core_get_position(db, client_id, str(pid))
                        if not adapted.position_label:
                            pl = _label_from_position_object(pos)
                            if pl:
                                adapted = replace(adapted, position_label=pl)
                        if not adapted.position_code and pos is not None:
                            for attr in ("code", "position_code"):
                                v = getattr(pos, attr, None)
                                if isinstance(v, str) and v.strip():
                                    adapted = replace(adapted, position_code=v.strip())
                                    break
                        if not adapted.department_code and pos is not None:
                            for attr in ("function_code", "department_code", "org_function_code"):
                                v = getattr(pos, attr, None)
                                if isinstance(v, str) and v.strip():
                                    adapted = replace(adapted, department_code=v.strip())
                                    break
                    except Exception:
                        _log.debug("skill_assessment: get_position не дал данные должности", exc_info=True)
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


def get_examination_instructions_folder_url(db: Session, client_id: str, employee_id: str) -> str | None:
    """
    URL папки с должностными инструкциями (если задан в ядре HR).

    Ожидается ``app.hr.get_examination_instructions_folder_url(db, client_id, employee_id) -> str | None``.
    """
    if not employee_id or _core_get_examination_instructions_folder_url is None:
        return None
    try:
        raw = _core_get_examination_instructions_folder_url(db, client_id, employee_id)
    except Exception:
        _log.exception("skill_assessment: app.hr.get_examination_instructions_folder_url failed")
        return None
    if raw is None:
        return None
    s = str(raw).strip()
    return s or None


def get_examination_regulation_reference_text(db: Session, client_id: str, employee_id: str) -> str | None:
    """
    Текст или выдержка регламента для семантического сравнения с ответом (sentence-transformers).

    Ожидается ``app.hr.get_examination_regulation_reference_text(db, client_id, employee_id) -> str | None``.
    """
    if not employee_id or _core_get_examination_regulation_reference_text is None:
        return None
    try:
        raw = _core_get_examination_regulation_reference_text(db, client_id, employee_id)
    except Exception:
        _log.exception("skill_assessment: app.hr.get_examination_regulation_reference_text failed")
        return None
    if raw is None:
        return None
    s = str(raw).strip()
    return s or None


def get_examination_kpi_labels(db: Session, client_id: str, employee_id: str) -> list[str] | None:
    """
    Список KPI для должности/регламента, чтобы показывать их как ориентир в UI и Telegram.

    Источники по приоритету:
    - ``app.hr.get_examination_kpi_labels`` если ядро его реализует;
    - строка ``KPI по регламенту: ...`` из ``get_examination_regulation_reference_text``;
    - разбор KPI из ``get_examination_question_texts``.
    """
    if not employee_id:
        return None

    if _core_get_examination_kpi_labels is not None:
        try:
            raw = _core_get_examination_kpi_labels(db, client_id, employee_id)
        except Exception:
            _log.exception("skill_assessment: app.hr.get_examination_kpi_labels failed")
            raw = None
        if raw is not None:
            if isinstance(raw, str):
                raw = [raw]
            if isinstance(raw, (list, tuple)):
                out = [str(x).strip() for x in raw if isinstance(x, str) and str(x).strip()]
                if out:
                    return out[:16]
                return []

    ref = get_examination_regulation_reference_text(db, client_id, employee_id)
    if ref:
        m = re.search(r"KPI по регламенту:\s*(.+)", ref, flags=re.IGNORECASE)
        if m:
            out = [part.strip(" \t\r\n;,.") for part in m.group(1).split(";") if part.strip(" \t\r\n;,.")]
            if out:
                return out[:16]

    questions = get_examination_question_texts(db, client_id, employee_id)
    if questions:
        out: list[str] = []
        for q in questions:
            m = re.search(r"Опишите KPI «([^»]+)»", str(q))
            if m:
                out.append(m.group(1).strip())
        if out:
            deduped: list[str] = []
            seen: set[str] = set()
            for item in out:
                key = item.lower()
                if key in seen:
                    continue
                seen.add(key)
                deduped.append(item)
            return deduped[:16]
    return None


def get_assessment_case_count(db: Session, client_id: str, employee_id: str | None) -> int | None:
    """
    Количество кейсов для позиции/сотрудника.

    Ожидается реализация в ядре: ``app.hr.get_assessment_case_count(db, client_id, employee_id) -> int | None``.
    При ``None`` плагин использует собственный fallback.
    """
    if not employee_id or _core_get_assessment_case_count is None:
        return None
    try:
        raw = _core_get_assessment_case_count(db, client_id, employee_id)
    except Exception:
        _log.exception("skill_assessment: app.hr.get_assessment_case_count failed")
        return None
    if raw is None:
        return None
    try:
        n = int(raw)
    except Exception:
        return None
    return max(1, min(3, n))


def get_examination_question_texts(db: Session, client_id: str, employee_id: str) -> list[str] | None:
    """
    Тексты вопросов экзамена по внутренним регламентам и KPI, привязанным к должности и подразделению.

    Ожидается реализация в ядре: ``app.hr.get_examination_question_texts(db, client_id, employee_id)``
    на основе таблицы KPI/шаблонов. Возвращает список формулировок (порядок = порядок на экзамене).

    Семантика ответа ядра:

    - ``None`` — ядро не подключено / функция не реализована → плагин использует общий сценарий ``regulation_v1``.
    - ``[]`` — регламент или KPI для должности не найдены → экзамен **приостанавливается**, кадрам уходит уведомление.
    - Непустой список — тексты вопросов по регламенту/KPI.
    """
    if not employee_id or _core_get_examination_question_texts is None:
        return None
    try:
        raw = _core_get_examination_question_texts(db, client_id, employee_id)
    except Exception:
        _log.exception("skill_assessment: app.hr.get_examination_question_texts failed")
        return None
    if raw is None:
        return None
    if isinstance(raw, str):
        raw = [raw]
    if not isinstance(raw, (list, tuple)):
        return None
    if len(raw) == 0:
        return []
    out: list[str] = []
    for item in raw:
        if isinstance(item, str) and item.strip():
            out.append(item.strip())
        elif isinstance(item, dict):
            t = item.get("text") or item.get("question") or item.get("question_text")
            if isinstance(t, str) and t.strip():
                out.append(t.strip())
    if not out:
        return []
    return out[:32]


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
