# route: (notify) | file: skill_assessment/services/docs_survey_notify.py
"""Уведомление в Telegram о назначении опроса по служебным документам (фаза Part 1)."""

from __future__ import annotations

import logging
import os
from datetime import datetime, timezone

from sqlalchemy.orm import Session

from skill_assessment.adapters.telegram_outbound import get_telegram_outbound
from skill_assessment.env import load_plugin_env
from skill_assessment.infrastructure.db_models import AssessmentSessionRow
from skill_assessment.integration.hr_core import employee_greeting_label, get_employee
from skill_assessment.schemas.api import DocsSurveyTelegramOut
from skill_assessment.services import examination_service as examination_svc
from skill_assessment.services.part1_docs_checklist import build_part1_docs_employee_page_absolute_url
from skill_assessment.services.telegram_docs_survey_consent import build_pd_consent_inline_keyboard

_log = logging.getLogger(__name__)


def send_docs_survey_assignment_notice_task(session_id: str) -> None:
    """Для FastAPI BackgroundTasks: отдельная сессия БД (не блокировать ответ POST /start)."""
    from app.db import SessionLocal

    db = SessionLocal()
    try:
        out = send_docs_survey_assignment_notice(db, session_id)
        if not out.sent:
            _log.warning(
                "docs_survey: фоновая отправка не выполнена (сессия %s…): %s",
                session_id[:8],
                out.skipped_reason or "unknown",
            )
        else:
            _log.info("docs_survey: фоновая отправка в Telegram ок (сессия %s…, chat_id=%s)", session_id[:8], out.chat_id)
    except Exception:
        _log.exception("docs_survey: фоновая отправка упала (сессия %s…)", session_id[:8])
    finally:
        db.close()


def send_docs_survey_assignment_notice(db: Session, session_id: str) -> DocsSurveyTelegramOut:
    load_plugin_env(override=False)
    use_mock = os.getenv("SKILL_ASSESSMENT_TELEGRAM_OUTBOUND", "").strip().lower() == "mock"
    token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
    if not use_mock and (not token or len(token) < 10):
        _log.warning("docs_survey: пропуск — no_bot_token (сессия %s…)", session_id[:8])
        return DocsSurveyTelegramOut(sent=False, chat_id=None, used_fallback_chat=False, skipped_reason="no_bot_token")

    row = db.get(AssessmentSessionRow, session_id)
    if row is None:
        return DocsSurveyTelegramOut(sent=False, chat_id=None, used_fallback_chat=False, skipped_reason="session_not_found")

    emp = get_employee(db, row.client_id, row.employee_id)
    name = employee_greeting_label(emp) or "коллега"
    position_line = "—"
    if emp is not None and emp.position_label and str(emp.position_label).strip():
        position_line = emp.position_label.strip()

    chat_id: str | None = None
    used_fallback = False
    chat_id_source = "unset"
    bind = (
        examination_svc.get_telegram_binding_for_employee(db, row.client_id, row.employee_id)
        if row.employee_id
        else None
    )
    if bind is not None and str(bind.telegram_chat_id).strip():
        chat_id = str(bind.telegram_chat_id).strip()
        chat_id_source = "examination_telegram_binding"
    elif emp is not None and emp.telegram_chat_id and str(emp.telegram_chat_id).strip():
        chat_id = emp.telegram_chat_id.strip()
        chat_id_source = "hr_employee_telegram_chat_id"
    else:
        disallow_fb = (os.getenv("TELEGRAM_DOCS_SURVEY_DISALLOW_FALLBACK") or "").strip().lower() in (
            "1",
            "true",
            "yes",
            "on",
        )
        if disallow_fb:
            _log.warning(
                "docs_survey: пропуск — у сотрудника нет Telegram (привязка/поле HR), "
                "а TELEGRAM_DOCS_SURVEY_DISALLOW_FALLBACK включён (сессия %s…)",
                session_id[:8],
            )
            return DocsSurveyTelegramOut(
                sent=False,
                chat_id=None,
                used_fallback_chat=False,
                skipped_reason="no_employee_telegram_disallow_fallback",
            )
        # Пустая строка в .env не должна отключать fallback (getenv с default не срабатывает для TELEGRAM_DOCS_SURVEY_FALLBACK_CHAT_ID="").
        chat_id = (os.getenv("TELEGRAM_DOCS_SURVEY_FALLBACK_CHAT_ID") or "300398364").strip()
        used_fallback = True
        chat_id_source = "env_TELEGRAM_DOCS_SURVEY_FALLBACK_CHAT_ID"
        _log.warning(
            "docs_survey: у сотрудника %s нет chat_id — запрос ПДн уходит на FALLBACK %s (сессия %s…). "
            "Сотрудник не увидит сообщение в своём чате; задайте telegram в HR или POST …/examination/telegram/bindings.",
            row.employee_id or "—",
            chat_id[:12] + ("…" if len(chat_id) > 12 else ""),
            session_id[:8],
        )

    if not chat_id:
        _log.warning("docs_survey: пропуск — no_chat_id (сессия %s…)", session_id[:8])
        return DocsSurveyTelegramOut(sent=False, chat_id=None, used_fallback_chat=False, skipped_reason="no_chat_id")

    short = session_id[:8]
    consent_url = os.getenv("DOCS_SURVEY_CONSENT_DOCUMENT_URL", "").strip()
    consent_link_line = consent_url if consent_url else "(здесь будет ссылка)"
    text = (
        f"Здравствуйте, {name}!\n\n"
        f"Вам назначен опрос по служебным документам в рамках оценки навыков (сессия {short}…). "
        f"Должность (для сверки с регламентом при проверке): {position_line}.\n\n"
        "Согласно действующей политике вам следует ознакомиться с текстом Согласия на обработку персональных данных.\n\n"
        f"Ссылка на текст Согласия: {consent_link_line}\n\n"
        "Вы даете согласие на обработку персональных данных?\n\n"
        "Сообщение сформировано автоматически (система оценки навыков)."
    )
    checklist_url = build_part1_docs_employee_page_absolute_url(db, session_id)
    if checklist_url:
        text += "\n\nЛичная страница чек-листа по документам:\n" + checklist_url

    try:
        reply_markup = build_pd_consent_inline_keyboard(session_id)
    except ValueError as e:
        _log.warning("docs_survey: keyboard build failed: %s", e)
        return DocsSurveyTelegramOut(
            sent=False, chat_id=chat_id, used_fallback_chat=used_fallback, skipped_reason="keyboard_callback_too_long"
        )

    _log.info(
        "docs_survey: сессия %s… → sendMessage chat_id=%s (источник: %s; employee_id=%s)",
        session_id[:8],
        chat_id,
        chat_id_source,
        row.employee_id or "—",
    )

    outbound = get_telegram_outbound()
    send_token = token if token else "mock_token_for_tests"
    try:
        result = outbound.send_message(
            token=send_token,
            chat_id=chat_id,
            text=text,
            reply_markup=reply_markup,
        )
        if not result.ok:
            detail = result.description or "send_failed"
            _log.warning("docs_survey telegram send failed: %s", detail)
            return DocsSurveyTelegramOut(
                sent=False,
                chat_id=chat_id,
                used_fallback_chat=used_fallback,
                skipped_reason=detail[:400] if detail else "send_failed",
            )
        now = datetime.now(timezone.utc)
        row.docs_survey_notify_chat_id = chat_id.strip()
        row.docs_survey_pd_consent_status = "awaiting_first"
        row.docs_survey_pd_consent_at = None
        row.docs_survey_consent_prompt_sent_at = now
        row.docs_survey_hr_notified_no_consent_at = None
        db.commit()
        db.refresh(row)
        return DocsSurveyTelegramOut(
            sent=True, chat_id=chat_id, used_fallback_chat=used_fallback, skipped_reason=None
        )
    except Exception as e:
        _log.exception("docs_survey telegram send failed")
        return DocsSurveyTelegramOut(
            sent=False, chat_id=chat_id, used_fallback_chat=used_fallback, skipped_reason=str(e)[:200]
        )
