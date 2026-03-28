# route: (examination) | file: skill_assessment/services/exam_protocol_recipients.py
"""
Кому слать протокол экзамена в Telegram: руководитель по оргструктуре.

Правило (если ядро HR не переопределяет через хук):

- Обычный сотрудник подразделения → руководитель этого подразделения (руководитель отдела).
- Руководитель подразделения → управляющий заместитель (зам. по управлению / операционный блок; задаётся ядром или ``TELEGRAM_EXAM_MANAGING_DEPUTY_EMPLOYEE_ID``).
- Заместитель (в т.ч. зам. директора) → директор (ядро или ``TELEGRAM_EXAM_DIRECTOR_EMPLOYEE_ID``).
- Директор → без вышестоящего в цепочке (опционально совет директоров: ``TELEGRAM_EXAM_PROTOCOL_FALLBACK_ABOVE_DIRECTOR_EMPLOYEE_ID``).

Ядро может реализовать одну функцию и закрыть все кейсы::

    # app.hr (опционально)
    def get_exam_protocol_manager_employee_id(db, client_id: str, employee_id: str) -> str | None: ...
"""

from __future__ import annotations

import logging
import os
import re

from sqlalchemy.orm import Session

from skill_assessment.integration.hr_core import EmployeeSnapshot, get_employee
from skill_assessment.services import examination_service as ex

_log = logging.getLogger(__name__)


def _env_employee_id(name: str) -> str | None:
    v = (os.getenv(name) or "").strip()
    return v or None


def _try_app_hr_hook(db: Session, client_id: str, employee_id: str) -> str | None:
    try:
        from app.hr import get_exam_protocol_manager_employee_id  # type: ignore[import-not-found]
    except ImportError:
        return None
    try:
        mid = get_exam_protocol_manager_employee_id(db, client_id, employee_id)
        if mid:
            return str(mid).strip()
    except Exception:
        _log.exception("exam_protocol_recipients: app.hr.get_exam_protocol_manager_employee_id")
    return None


def _try_org_unit_head(db: Session, client_id: str, org_unit_id: str) -> str | None:
    if not org_unit_id:
        return None
    try:
        from app.hr import get_org_unit  # type: ignore[import-not-found]
    except ImportError:
        return None
    try:
        ou = get_org_unit(db, client_id, org_unit_id)
        if ou is None:
            return None
        hid = (
            getattr(ou, "head_employee_id", None)
            or getattr(ou, "manager_employee_id", None)
            or getattr(ou, "leader_employee_id", None)
        )
        if hid:
            return str(hid).strip()
    except Exception:
        _log.debug("exam_protocol_recipients: get_org_unit", exc_info=True)
    return None


def _try_director(db: Session, client_id: str) -> str | None:
    eid = _env_employee_id("TELEGRAM_EXAM_DIRECTOR_EMPLOYEE_ID")
    if eid:
        return eid
    try:
        from app.hr import get_company_director_employee_id  # type: ignore[import-not-found]
    except ImportError:
        return None
    try:
        d = get_company_director_employee_id(db, client_id)
        return str(d).strip() if d else None
    except Exception:
        _log.debug("exam_protocol_recipients: get_company_director_employee_id", exc_info=True)
    return None


def _try_managing_deputy(db: Session, client_id: str) -> str | None:
    eid = _env_employee_id("TELEGRAM_EXAM_MANAGING_DEPUTY_EMPLOYEE_ID")
    if eid:
        return eid
    try:
        from app.hr import get_managing_deputy_employee_id  # type: ignore[import-not-found]
    except ImportError:
        return None
    try:
        d = get_managing_deputy_employee_id(db, client_id)
        return str(d).strip() if d else None
    except Exception:
        _log.debug("exam_protocol_recipients: get_managing_deputy_employee_id", exc_info=True)
    return None


class _OrgRole:
    EMPLOYEE = "employee"
    DEPT_HEAD = "department_head"
    DEPUTY = "deputy"
    DIRECTOR = "director"


def _classify_role(emp: EmployeeSnapshot) -> str:
    """Грубая классификация по названию должности, если ядро не прислало флаги."""
    pl = (emp.position_label or "").lower()
    if not pl:
        return _OrgRole.EMPLOYEE
    # Сначала заместители (в т.ч. зам. директора) → вышестоящий = директор
    if re.search(r"замест", pl) or "зам." in pl or "зам " in pl:
        return _OrgRole.DEPUTY
    if "генеральный директор" in pl or "главный исполнительный" in pl:
        return _OrgRole.DIRECTOR
    if re.search(r"\bдиректор\b", pl):
        return _OrgRole.DIRECTOR
    # Руководитель подразделения / начальник отдела → зам. по управлению / директор
    if any(
        x in pl
        for x in (
            "начальник отдела",
            "руководитель отдела",
            "руководитель подраздел",
            "head of department",
            "начальник управления",
        )
    ):
        return _OrgRole.DEPT_HEAD
    if re.search(r"\bначальник\b", pl) and "отдел" in pl:
        return _OrgRole.DEPT_HEAD
    return _OrgRole.EMPLOYEE


def resolve_exam_protocol_manager_employee_id(db: Session, client_id: str, employee_id: str) -> str | None:
    """
    Кому направить протокол как руководителю (employee_id в ядре), не Telegram chat_id.
    """
    mid = _try_app_hr_hook(db, client_id, employee_id)
    if mid:
        return mid

    emp = get_employee(db, client_id, employee_id)
    if emp is None:
        return _env_employee_id("TELEGRAM_EXAM_DEFAULT_MANAGER_EMPLOYEE_ID")

    if emp.manager_employee_id and emp.manager_employee_id.strip():
        return emp.manager_employee_id.strip()

    role = _classify_role(emp)
    if role == _OrgRole.DIRECTOR:
        return _env_employee_id("TELEGRAM_EXAM_PROTOCOL_FALLBACK_ABOVE_DIRECTOR_EMPLOYEE_ID")

    if role == _OrgRole.DEPUTY:
        did = _try_director(db, client_id)
        if did:
            return did
        return _env_employee_id("TELEGRAM_EXAM_DEFAULT_MANAGER_EMPLOYEE_ID")

    if role == _OrgRole.DEPT_HEAD:
        md = _try_managing_deputy(db, client_id)
        if md:
            return md
        did = _try_director(db, client_id)
        if did:
            return did
        return _env_employee_id("TELEGRAM_EXAM_DEFAULT_MANAGER_EMPLOYEE_ID")

    # Обычный сотрудник → руководитель подразделения
    if emp.org_unit_id:
        head = _try_org_unit_head(db, client_id, emp.org_unit_id)
        if head and head != employee_id:
            return head

    fallback = _env_employee_id("TELEGRAM_EXAM_DEFAULT_MANAGER_EMPLOYEE_ID")
    if fallback:
        return fallback
    return None


def telegram_chat_id_for_employee(db: Session, client_id: str, target_employee_id: str) -> str | None:
    """chat_id Telegram для сотрудника: привязка экзамена или поле в карточке HR."""
    if not target_employee_id:
        return None
    bind = ex.get_telegram_binding_for_employee(db, client_id, target_employee_id)
    if bind:
        return str(bind.telegram_chat_id).strip()
    snap = get_employee(db, client_id, target_employee_id)
    if snap and snap.telegram_chat_id:
        return str(snap.telegram_chat_id).strip()
    return None


def resolve_manager_telegram_chat_for_protocol(db: Session, client_id: str, employee_id: str) -> str | None:
    """Руководитель для рассылки протокола: алгоритм выше + привязка Telegram."""
    mgr_eid = resolve_exam_protocol_manager_employee_id(db, client_id, employee_id)
    if mgr_eid:
        chat = telegram_chat_id_for_employee(db, client_id, mgr_eid)
        if chat:
            return chat
        _log.info(
            "exam_protocol_recipients: manager employee %s… has no Telegram chat id",
            mgr_eid[:8],
        )
    legacy = (os.getenv("TELEGRAM_EXAM_MANAGER_CHAT_ID") or "").strip()
    return legacy or None
