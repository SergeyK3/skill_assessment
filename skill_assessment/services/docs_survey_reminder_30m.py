# route: (background) | file: skill_assessment/services/docs_survey_reminder_30m.py
"""Напоминание в Telegram за N минут до слота опроса по документам (готовность: Да/Нет).

``docs_survey_scheduled_at`` в БД — наивное **UTC** (слот в календаре задаётся в локальной зоне
:envvar:`DOCS_SURVEY_LOCAL_TIMEZONE`, см. :mod:`skill_assessment.services.docs_survey_time`).
"""

from __future__ import annotations

import logging
import os
from datetime import datetime, timezone

import httpx
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db import SessionLocal

from skill_assessment.env import load_plugin_env
from skill_assessment.infrastructure.db_models import AssessmentSessionRow
from skill_assessment.services.docs_survey_time import reminder_minutes_before
from skill_assessment.services.telegram_docs_survey_readiness import build_readiness_inline_keyboard

_log = logging.getLogger(__name__)


def _as_utc_aware(dt: datetime) -> datetime:
    """SQLite отдаёт naive datetime; сравниваем с ``now`` в UTC (aware)."""
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _minutes_before_scheduled(scheduled_at: datetime, now: datetime) -> float:
    return (_as_utc_aware(scheduled_at) - _as_utc_aware(now)).total_seconds() / 60.0


def _in_reminder_window(minutes_left: float, target: int) -> bool:
    """Окно ±1 минута вокруг целевого времени (при опросе раз в минуту)."""
    return (target - 1) <= minutes_left <= (target + 1)


def _should_send_reminder_now(minutes_left: float, target: int) -> bool:
    """
    Отправить, если попали в узкое окно **или** догоняем: API/процесс не работали в нужную минуту,
    но до слота ещё от 1 минуты до (target−2) минут (не раньше чем за target+1 мин до слота).
    """
    if minutes_left <= 0:
        return False
    if minutes_left > float(target) + 1.0:
        return False
    if _in_reminder_window(minutes_left, target):
        return True
    if target <= 2:
        return False
    # Догон: пропустили окно [target−1, target+1], но ещё не «последняя минута» перед слотом
    return 1.0 <= minutes_left < float(target) - 1.0


def send_reminder_30m_for_session(db: Session, row: AssessmentSessionRow, minutes_label: int) -> bool:
    """Отправляет напоминание один раз; помечает docs_survey_reminder_30m_sent_at при успехе."""
    load_plugin_env(override=False)
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
            row.docs_survey_reminder_30m_sent_at = datetime.now(timezone.utc)
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
    target = reminder_minutes_before()
    now = datetime.now(timezone.utc)
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
            if not _should_send_reminder_now(mins, target):
                continue
            if send_reminder_30m_for_session(db, row, target):
                n += 1
    except Exception:
        _log.exception("docs_survey_reminder_30m: ошибка обхода")
        db.rollback()
    finally:
        db.close()
    return n
