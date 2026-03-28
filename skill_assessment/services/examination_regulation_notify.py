# route: (examination) | file: skill_assessment/services/examination_regulation_notify.py
"""Уведомления кадров по проблемным состояниям экзамена."""

from __future__ import annotations

import logging
import os
from datetime import datetime

from sqlalchemy.orm import Session

from skill_assessment.adapters.telegram_outbound import get_telegram_outbound
from skill_assessment.infrastructure.db_models import ExaminationSessionRow
from skill_assessment.integration.hr_core import employee_display_label, get_employee

_log = logging.getLogger(__name__)


def _hr_channel_and_token() -> tuple[str, str] | None:
    token = (os.getenv("TELEGRAM_BOT_TOKEN") or "").strip()
    if not token or len(token) < 10:
        _log.warning("examination_regulation_notify: TELEGRAM_BOT_TOKEN not set")
        return None
    ch = (os.getenv("TELEGRAM_EXAM_PROTOCOL_CHANNEL_ID") or "").strip()
    if not ch:
        _log.warning(
            "examination_regulation_notify: TELEGRAM_EXAM_PROTOCOL_CHANNEL_ID not set — HR not notified"
        )
        return None
    return token, ch


def _send_hr_text(text: str) -> None:
    cfg = _hr_channel_and_token()
    if cfg is None:
        return
    token, ch = cfg
    outbound = get_telegram_outbound()
    r = outbound.send_message(token=token, chat_id=ch, text=text, reply_markup=None)
    if not r.ok:
        _log.warning(
            "examination_regulation_notify: send failed chat=%s… %s",
            ch[:12],
            r.description,
        )


def notify_hr_no_examination_regulation(db: Session, row: ExaminationSessionRow) -> None:
    """Сообщение в служебный Telegram-канал (или чат), если задан TELEGRAM_EXAM_PROTOCOL_CHANNEL_ID."""
    emp = get_employee(db, row.client_id, row.employee_id)
    label = employee_display_label(emp) or row.employee_id
    base = (os.getenv("SKILL_ASSESSMENT_PUBLIC_BASE_URL") or "http://127.0.0.1:8000").rstrip("/")
    text = (
        "Экзамен по внутренним регламентам приостановлен: нет применимого регламента/KPI для вопросов.\n\n"
        f"Организация (client_id): {row.client_id}\n"
        f"Сотрудник: {label} (id {row.employee_id})\n"
        f"Сессия экзамена: {row.id}\n\n"
        "После загрузки регламента в системе снимите блок: "
        f"POST {base}/api/skill-assessment/examination/sessions/{{session_id}}/hr/release-regulation-block"
    )
    _send_hr_text(text)


def notify_hr_examination_timeout(
    db: Session,
    row: ExaminationSessionRow,
    *,
    last_answer_at: datetime | None,
    timeout_minutes: int,
) -> None:
    emp = get_employee(db, row.client_id, row.employee_id)
    label = employee_display_label(emp) or row.employee_id
    last_answer_line = "—"
    if last_answer_at is not None:
        last_answer_line = str(last_answer_at)
    text = (
        "Экзамен по внутренним регламентам прерван по таймауту.\n\n"
        f"Организация (client_id): {row.client_id}\n"
        f"Сотрудник: {label} (id {row.employee_id})\n"
        f"Сессия экзамена: {row.id}\n"
        f"Последний ответ: {last_answer_line}\n\n"
        f"Между ответами прошло более {timeout_minutes} минут. "
        "Сессия помечена как interrupted_timeout; для повторной проверки назначьте новую сессию."
    )
    _send_hr_text(text)
