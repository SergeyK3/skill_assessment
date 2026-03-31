# route: (notify) | file: skill_assessment/services/docs_survey_hr_notify.py
"""Уведомление отдела кадров в Telegram об отказе или отсутствии ответа по согласию ПДн (Part1)."""

from __future__ import annotations

import logging
import os
from datetime import datetime
from typing import Literal

import httpx
from sqlalchemy.orm import Session

from skill_assessment.env import PLUGIN_ENV_FILE, load_env_file
from skill_assessment.infrastructure.db_models import AssessmentSessionRow
from skill_assessment.integration.hr_core import employee_display_label, get_employee

_log = logging.getLogger(__name__)

Reason = Literal["declined", "timeout"]


def _load_env() -> None:
    load_env_file(PLUGIN_ENV_FILE, override=False)


def send_telegram_text_to_chat(chat_id: str, text: str) -> bool:
    """Отправка текста в указанный чат Bot API. Возвращает True при успехе."""
    _load_env()
    token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
    if not token or len(token) < 10:
        _log.warning("docs_survey_hr: TELEGRAM_BOT_TOKEN отсутствует — уведомление HR не отправлено")
        return False
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    try:
        r = httpx.post(url, json={"chat_id": chat_id.strip(), "text": text}, timeout=20.0)
        data = r.json() if r.headers.get("content-type", "").startswith("application/json") else {}
        if r.is_success and isinstance(data, dict) and data.get("ok"):
            return True
        detail = data.get("description") if isinstance(data, dict) else r.text[:300]
        _log.warning("docs_survey_hr: sendMessage HTTP %s: %s", r.status_code, detail)
    except Exception:
        _log.exception("docs_survey_hr: sendMessage failed")
    return False


def notify_hr_docs_survey_consent_issue(
    db: Session, row: AssessmentSessionRow, reason: Reason
) -> bool:
    """
    Шлёт в чат HR текст о проблеме с согласием. Не коммитит БД.
    Возвращает True, если сообщение ушло успешно (можно проставить hr_notified).
    """
    _load_env()
    hr_chat = os.getenv("TELEGRAM_DOCS_SURVEY_HR_NOTIFY_CHAT_ID", "").strip()
    if not hr_chat:
        _log.warning(
            "docs_survey_hr: TELEGRAM_DOCS_SURVEY_HR_NOTIFY_CHAT_ID не задан — "
            "уведомление отдела кадров пропущено (сессия %s…)",
            row.id[:8],
        )
        return False

    emp = get_employee(db, row.client_id, row.employee_id) if row.employee_id else None
    who = employee_display_label(emp) or (row.employee_id or "—")
    pos = (emp.position_label.strip() if emp and emp.position_label else None) or "—"
    short = row.id[:8]

    if reason == "declined":
        body = (
            "Оценка навыков (Part 1): сотрудник нажал «Нет» в ответ на запрос согласия "
            "на обработку персональных данных (опрос по служебным документам).\n\n"
            f"Сессия: {short}…\n"
            f"Сотрудник: {who}\n"
            f"Должность: {pos}\n\n"
            "Сообщение сформировано автоматически."
        )
    else:
        body = (
            "Оценка навыков (Part 1): сотрудник не ответил на запрос согласия "
            "на обработку персональных данных в течение 10 минут (нет реакции в чате с ботом).\n\n"
            f"Сессия: {short}…\n"
            f"Сотрудник: {who}\n"
            f"Должность: {pos}\n\n"
            "Сообщение сформировано автоматически."
        )

    notify_chat = (row.docs_survey_notify_chat_id or "").strip()
    suppress_same_chat = (os.getenv("TELEGRAM_DOCS_SURVEY_SUPPRESS_HR_NOTIFY_TO_EMPLOYEE_CHAT") or "1").strip().lower() in (
        "1",
        "true",
        "yes",
        "on",
    )
    if suppress_same_chat and notify_chat and hr_chat and notify_chat == hr_chat:
        _log.info(
            "docs_survey_hr: suppress notify to same chat_id=%s (session %s…, reason=%s)",
            hr_chat,
            short,
            reason,
        )
        return False
    if notify_chat:
        body += (
            f"\n\nТехнически: запрос согласия отправлялся в Telegram chat_id={notify_chat}. "
            "Если это не личный чат сотрудника с ботом — проверьте поле Telegram у сотрудника в HR "
            "или привязку POST /api/skill-assessment/examination/telegram/bindings; иначе сотрудник не видел кнопки «Да»/«Нет»."
        )

    ok = send_telegram_text_to_chat(hr_chat, body)
    if ok:
        _log.info("docs_survey_hr: уведомление отправлено (%s), сессия %s…", reason, short)
    return ok
