# route: (telegram) | file: skill_assessment/services/telegram_docs_survey.py
"""
Inline-кнопки «календаря» для согласования даты/времени опроса по документам (Part 1).

Callback в Telegram ограничен 64 байтами; формат: ``dsd|...`` / ``dst|...`` с разделителем ``|``.
"""

from __future__ import annotations

import logging
import os
import re
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Any

from sqlalchemy.orm import Session

from skill_assessment.infrastructure.db_models import AssessmentSessionRow
from skill_assessment.integration.hr_core import get_employee
from skill_assessment.services import examination_service as examination_svc
from skill_assessment.services.docs_survey_time import local_slot_to_utc_naive

_log = logging.getLogger(__name__)

PREFIX_DAY = "dsd"
PREFIX_TIME = "dst"


def _time_slots_half_hour_workday() -> tuple[str, ...]:
    """Рабочий день: с 9:00 до 12:30 и с 14:00 до 17:30 с шагом 30 минут (перерыв 13:00–13:30 как раньше)."""
    slots: list[str] = []
    for h in range(9, 13):
        for m in (0, 30):
            slots.append(f"{h:02d}:{m:02d}")
    for h in range(14, 17):
        for m in (0, 30):
            slots.append(f"{h:02d}:{m:02d}")
    slots.extend(("17:00", "17:30"))
    return tuple(slots)


TIME_SLOTS = _time_slots_half_hour_workday()


@dataclass(frozen=True)
class DocsSurveyCallbackResult:
    """Ответ на callback: всплывающая подсказка и исходящие сообщения (текст + опционально клавиатура)."""

    popup_text: str | None
    outgoing: list[tuple[str, dict[str, Any] | None]]


def _next_workdays(count: int = 5) -> list[date]:
    out: list[date] = []
    d = date.today()
    while len(out) < count:
        if d.weekday() < 5:
            out.append(d)
        d += timedelta(days=1)
    return out


def build_docs_survey_slot_keyboard_days(session_id: str, workday_count: int = 5) -> dict[str, Any]:
    """Ближайшие рабочие дни (по одной кнопке в ряд). ``workday_count`` — например 3 при переносе после «не готов»."""
    rows: list[list[dict[str, str]]] = []
    for d in _next_workdays(workday_count):
        label = d.strftime("%d.%m.%Y")
        cb = f"{PREFIX_DAY}|{session_id}|{d.strftime('%Y%m%d')}"
        if len(cb.encode("utf-8")) > 64:
            _log.warning("callback_data exceeds 64 bytes: %r", cb)
            continue
        rows.append([{"text": label, "callback_data": cb}])
    return {"inline_keyboard": rows}


def build_docs_survey_slot_keyboard(session_id: str) -> dict[str, Any]:
    """Первая ступень календаря Part1: пять ближайших рабочих дней."""
    return build_docs_survey_slot_keyboard_days(session_id, 5)


def _build_time_keyboard(session_id: str, ymd: str) -> dict[str, Any]:
    rows: list[list[dict[str, str]]] = []
    for slot in TIME_SLOTS:
        hhmm = slot.replace(":", "")
        cb = f"{PREFIX_TIME}|{session_id}|{ymd}|{hhmm}"
        if len(cb.encode("utf-8")) > 64:
            _log.warning("callback_data exceeds 64 bytes: %r", cb)
            continue
        rows.append([{"text": slot, "callback_data": cb}])
    return {"inline_keyboard": rows}


def _telegram_chat_ids_equal(a: str | None, b: str | None) -> bool:
    if not a or not b:
        return False
    x, y = str(a).strip(), str(b).strip()
    if x == y:
        return True
    if x.isdigit() and y.isdigit():
        return int(x) == int(y)
    return False


def _chat_owns_session(db: Session, session_id: str, chat_id: str) -> bool:
    row = db.get(AssessmentSessionRow, session_id)
    if row is None:
        _log.warning("docs_survey callback: session not found %s", session_id[:12])
        return False
    tid = str(chat_id).strip()
    stored = (row.docs_survey_notify_chat_id or "").strip()
    if stored and _telegram_chat_ids_equal(stored, tid):
        return True
    if not row.employee_id:
        _log.warning(
            "docs_survey callback: no employee_id on session %s, chat=%s stored=%r",
            session_id[:12],
            tid,
            stored or None,
        )
        return False
    bind = examination_svc.get_telegram_binding_for_employee(db, row.client_id, row.employee_id)
    if bind is not None and _telegram_chat_ids_equal(str(bind.telegram_chat_id), tid):
        return True
    emp = get_employee(db, row.client_id, row.employee_id)
    if emp is not None and emp.telegram_chat_id and _telegram_chat_ids_equal(str(emp.telegram_chat_id), tid):
        return True
    dev_c = os.getenv("TELEGRAM_DEV_CLIENT_ID", "").strip()
    dev_e = os.getenv("TELEGRAM_DEV_EMPLOYEE_ID", "").strip()
    if dev_c and dev_e and row.client_id == dev_c and row.employee_id == dev_e:
        return True
    return False


def handle_docs_survey_callback(
    db: Session, chat_id: str, callback_data: str, _callback_query_id: str
) -> DocsSurveyCallbackResult | None:
    data = (callback_data or "").strip()
    if not data.startswith(f"{PREFIX_DAY}|") and not data.startswith(f"{PREFIX_TIME}|"):
        return None

    parts = data.split("|")
    if parts[0] == PREFIX_DAY and len(parts) == 3:
        _, session_id, ymd = parts
        if not re.fullmatch(r"[0-9]{8}", ymd):
            return DocsSurveyCallbackResult("Некорректная дата", [])
        if not _chat_owns_session(db, session_id, chat_id):
            return DocsSurveyCallbackResult("Нет доступа к этой сессии", [])
        srow = db.get(AssessmentSessionRow, session_id)
        if srow is None or srow.docs_survey_pd_consent_status != "accepted":
            return DocsSurveyCallbackResult(
                "Сначала подтвердите согласие кнопками «Да»/«Нет» или сообщением в чате.", []
            )
        try:
            d = date(int(ymd[:4]), int(ymd[4:6]), int(ymd[6:8]))
        except ValueError:
            return DocsSurveyCallbackResult("Некорректная дата", [])
        human = d.strftime("%d.%m.%Y")
        kb = _build_time_keyboard(session_id, ymd)
        return DocsSurveyCallbackResult(
            "Выберите время",
            [
                (
                    f"Выберите время на {human} (опрос 15–30 минут):",
                    kb if kb.get("inline_keyboard") else None,
                )
            ],
        )

    if parts[0] == PREFIX_TIME and len(parts) == 4:
        _, session_id, ymd, hhmm = parts
        if not re.fullmatch(r"[0-9]{8}", ymd) or not re.fullmatch(r"[0-9]{4}", hhmm):
            return DocsSurveyCallbackResult("Некорректное время", [])
        if not _chat_owns_session(db, session_id, chat_id):
            return DocsSurveyCallbackResult("Нет доступа к этой сессии", [])
        srow = db.get(AssessmentSessionRow, session_id)
        if srow is None or srow.docs_survey_pd_consent_status != "accepted":
            return DocsSurveyCallbackResult(
                "Сначала подтвердите согласие кнопками «Да»/«Нет» или сообщением в чате.", []
            )
        try:
            d = date(int(ymd[:4]), int(ymd[4:6]), int(ymd[6:8]))
        except ValueError:
            return DocsSurveyCallbackResult("Некорректная дата", [])
        hh = hhmm[:2]
        mm = hhmm[2:]
        human_dt = f"{d.strftime('%d.%m.%Y')} {hh}:{mm}"
        # Локальное время в зоне DOCS_SURVEY_LOCAL_TIMEZONE (по умолчанию Europe/Moscow) → UTC в БД
        sched = local_slot_to_utc_naive(d, int(hh), int(mm))
        if srow is not None:
            srow.docs_survey_scheduled_at = sched
            srow.docs_survey_reminder_30m_sent_at = None
            srow.docs_survey_exam_gate_awaiting = False
            db.commit()
            db.refresh(srow)
        return DocsSurveyCallbackResult(
            f"Готово: {human_dt}",
            [
                (
                    f"Записано: опрос по служебным документам — {human_dt}.\n"
                    "При необходимости согласуйте изменение с HR.",
                    None,
                ),
            ],
        )

    return DocsSurveyCallbackResult("Не удалось обработать выбор", [])
