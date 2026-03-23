# route: (notify) | file: skill_assessment/services/docs_survey_notify.py
"""Уведомление в Telegram о назначении опроса по служебным документам (фаза Part 1)."""

from __future__ import annotations

import logging
import os
from datetime import datetime
from pathlib import Path

import httpx
from dotenv import load_dotenv
from sqlalchemy.orm import Session

from skill_assessment.infrastructure.db_models import AssessmentSessionRow
from skill_assessment.integration.hr_core import employee_greeting_label, get_employee
from skill_assessment.schemas.api import DocsSurveyTelegramOut
from skill_assessment.services import examination_service as examination_svc
from skill_assessment.services.telegram_docs_survey_consent import build_pd_consent_inline_keyboard

_log = logging.getLogger(__name__)
# Корень пакета skill_assessment (рядом с pyproject.toml): services → skill_assessment → корень
_ENV = Path(__file__).resolve().parent.parent.parent / ".env"


def send_docs_survey_assignment_notice(db: Session, session_id: str) -> DocsSurveyTelegramOut:
    load_dotenv(_ENV, override=True)
    token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
    if not token or len(token) < 10:
        return DocsSurveyTelegramOut(sent=False, chat_id=None, skipped_reason="no_bot_token")

    row = db.get(AssessmentSessionRow, session_id)
    if row is None:
        return DocsSurveyTelegramOut(sent=False, chat_id=None, skipped_reason="session_not_found")

    emp = get_employee(db, row.client_id, row.employee_id)
    name = employee_greeting_label(emp) or "коллега"
    position_line = "—"
    if emp is not None and emp.position_label and str(emp.position_label).strip():
        position_line = emp.position_label.strip()

    chat_id: str | None = None
    bind = (
        examination_svc.get_telegram_binding_for_employee(db, row.client_id, row.employee_id)
        if row.employee_id
        else None
    )
    if bind is not None:
        chat_id = str(bind.telegram_chat_id).strip()
    elif emp is not None and emp.telegram_chat_id:
        chat_id = emp.telegram_chat_id.strip()
    else:
        chat_id = os.getenv("TELEGRAM_DOCS_SURVEY_FALLBACK_CHAT_ID", "300398364").strip()

    if not chat_id:
        return DocsSurveyTelegramOut(sent=False, chat_id=None, skipped_reason="no_chat_id")

    short = session_id[:8]
    consent_url = os.getenv("DOCS_SURVEY_CONSENT_DOCUMENT_URL", "").strip()
    consent_link_line = consent_url if consent_url else "(здесь будет ссылка)"
    text = (
        f"Здравствуйте, {name}!\n\n"
        f"Вам назначен опрос по служебным документам в рамках оценки навыков (сессия {short}…). "
        f"Должность: {position_line}.\n\n"
        "Согласно действующей политике вам следует ознакомиться с текстом Согласия на обработку персональных данных.\n\n"
        f"Ссылка на текст Согласия: {consent_link_line}\n\n"
        "Вы даете согласие на обработку персональных данных?\n\n"
        "Сообщение сформировано автоматически (система оценки навыков)."
    )

    try:
        reply_markup = build_pd_consent_inline_keyboard(session_id)
    except ValueError as e:
        _log.warning("docs_survey: keyboard build failed: %s", e)
        return DocsSurveyTelegramOut(sent=False, chat_id=chat_id, skipped_reason="keyboard_callback_too_long")

    url = f"https://api.telegram.org/bot{token}/sendMessage"
    try:
        r = httpx.post(
            url,
            json={"chat_id": chat_id, "text": text, "reply_markup": reply_markup},
            timeout=20.0,
        )
        data = r.json() if r.headers.get("content-type", "").startswith("application/json") else {}
        if not r.is_success:
            detail = data.get("description") if isinstance(data, dict) else r.text[:300]
            _log.warning("docs_survey telegram HTTP %s: %s", r.status_code, detail)
            return DocsSurveyTelegramOut(
                sent=False, chat_id=chat_id, skipped_reason=f"http_{r.status_code}: {detail}"
            )
        if isinstance(data, dict) and data.get("ok"):
            now = datetime.utcnow()
            row.docs_survey_notify_chat_id = chat_id.strip()
            row.docs_survey_pd_consent_status = "awaiting_first"
            row.docs_survey_pd_consent_at = None
            row.docs_survey_consent_prompt_sent_at = now
            row.docs_survey_hr_notified_no_consent_at = None
            db.commit()
            db.refresh(row)
            return DocsSurveyTelegramOut(sent=True, chat_id=chat_id, skipped_reason=None)
        return DocsSurveyTelegramOut(sent=False, chat_id=chat_id, skipped_reason=str(data)[:400])
    except Exception as e:
        _log.exception("docs_survey telegram send failed")
        return DocsSurveyTelegramOut(sent=False, chat_id=chat_id, skipped_reason=str(e)[:200])
