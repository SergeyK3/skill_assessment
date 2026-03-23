# route: (background) | file: skill_assessment/services/docs_survey_reminder_30m.py
"""Напоминание в Telegram за ~30 минут до слота опроса по документам (готовность: Да/Нет)."""

from __future__ import annotations

import logging
import os
from datetime import datetime
from pathlib import Path

import httpx
from dotenv import load_dotenv
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db import SessionLocal

from skill_assessment.infrastructure.db_models import AssessmentSessionRow
from skill_assessment.services.telegram_docs_survey_readiness import build_readiness_inline_keyboard

_log = logging.getLogger(__name__)
_ENV = Path(__file__).resolve().parent.parent.parent / ".env"


def _minutes_before_scheduled(scheduled_at: datetime, now: datetime) -> float:
    return (scheduled_at - now).total_seconds() / 60.0


def _reminder_target_minutes() -> int:
    load_dotenv(_ENV, override=True)
    raw = os.getenv("DOCS_SURVEY_REMINDER_MINUTES_BEFORE", "30").strip()
    try:
        return max(1, min(120, int(raw)))
    except ValueError:
        return 30


def _in_reminder_window(minutes_left: float, target: int) -> bool:
    """Окно ±1 минута вокруг целевого времени (при опросе раз в минуту)."""
    return (target - 1) <= minutes_left <= (target + 1)


def send_reminder_30m_for_session(db: Session, row: AssessmentSessionRow, minutes_label: int) -> bool:
    """Отправляет напоминание один раз; помечает docs_survey_reminder_30m_sent_at при успехе."""
    load_dotenv(_ENV, override=True)
    token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
    if not token or len(token) < 10:
        _log.warning("docs_survey_reminder_30m: нет TELEGRAM_BOT_TOKEN")
        return False

    chat_id = (row.docs_survey_notify_chat_id or "").strip()
    if not chat_id:
        _log.warning("docs_survey_reminder_30m: нет docs_survey_notify_chat_id, сессия %s…", row.id[:8])
        return False

    short = row.id[:8]
    try:
        kb = build_readiness_inline_keyboard(row.id)
    except ValueError as e:
        _log.warning("docs_survey_reminder_30m: клавиатура: %s", e)
        return False

    text = (
        f"Напоминание: примерно через {minutes_label} мин. запланирован опрос по служебным документам "
        f"(сессия {short}…).\n\n"
        "Готовы ли вы пройти опрос в назначенное время?\n\n"
        "Нажмите «Да» или «Нет».\n\n"
        "Сообщение сформировано автоматически (система оценки навыков)."
    )

    url = f"https://api.telegram.org/bot{token}/sendMessage"
    try:
        r = httpx.post(
            url,
            json={"chat_id": chat_id, "text": text, "reply_markup": kb},
            timeout=20.0,
        )
        data = r.json() if r.headers.get("content-type", "").startswith("application/json") else {}
        if r.is_success and isinstance(data, dict) and data.get("ok"):
            row.docs_survey_reminder_30m_sent_at = datetime.utcnow()
            db.commit()
            db.refresh(row)
            _log.info("docs_survey_reminder_30m: отправлено сессия %s…", short)
            return True
        detail = data.get("description") if isinstance(data, dict) else r.text[:300]
        _log.warning("docs_survey_reminder_30m: HTTP %s %s", r.status_code, detail)
    except Exception:
        _log.exception("docs_survey_reminder_30m: send failed")
    return False


def process_docs_survey_30m_reminders_once() -> int:
    """
    Планировщик: сессии с выбранным слотом, согласием, без отправленного напоминания.
    """
    target = _reminder_target_minutes()
    now = datetime.utcnow()
    db = SessionLocal()
    n = 0
    try:
        rows = db.scalars(
            select(AssessmentSessionRow).where(
                AssessmentSessionRow.docs_survey_scheduled_at.isnot(None),
                AssessmentSessionRow.docs_survey_reminder_30m_sent_at.is_(None),
                AssessmentSessionRow.docs_survey_pd_consent_status == "accepted",
                AssessmentSessionRow.docs_survey_notify_chat_id.isnot(None),
            )
        ).all()
        for row in rows:
            sched = row.docs_survey_scheduled_at
            if sched is None:
                continue
            mins = _minutes_before_scheduled(sched, now)
            if mins <= 0:
                continue
            if not _in_reminder_window(mins, target):
                continue
            if send_reminder_30m_for_session(db, row, target):
                n += 1
    except Exception:
        _log.exception("docs_survey_reminder_30m: ошибка обхода")
        db.rollback()
    finally:
        db.close()
    return n
