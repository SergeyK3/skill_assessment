# route: (background) | file: skill_assessment/services/docs_survey_consent_timeout.py
"""Таймаут 10 минут на ответ по согласию ПДн: уведомление HR при молчании."""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta, timezone

from sqlalchemy import select
from app.db import SessionLocal

from skill_assessment.domain.entities import AssessmentSessionStatus, SessionPhase
from skill_assessment.infrastructure.db_models import AssessmentSessionRow
from skill_assessment.services.examination_answer_timeout import process_examination_answer_timeouts_once
from skill_assessment.services.docs_survey_hr_notify import notify_hr_docs_survey_consent_issue
from skill_assessment.services.docs_survey_reminder_30m import process_docs_survey_30m_reminders_once

_log = logging.getLogger(__name__)

CONSENT_WAIT_MINUTES = 10
POLL_INTERVAL_SEC = 60


def process_consent_timeouts_once() -> int:
    """
    Помечает сессии с истёкшим ожиданием и уведомляет HR один раз.
    Возвращает число обработанных сессий.
    """
    cutoff = datetime.now(timezone.utc) - timedelta(minutes=CONSENT_WAIT_MINUTES)
    db = SessionLocal()
    n = 0
    try:
        rows = db.scalars(
            select(AssessmentSessionRow).where(
                AssessmentSessionRow.docs_survey_pd_consent_status == "awaiting_first",
                AssessmentSessionRow.docs_survey_consent_prompt_sent_at.isnot(None),
                AssessmentSessionRow.docs_survey_consent_prompt_sent_at < cutoff,
                AssessmentSessionRow.docs_survey_hr_notified_no_consent_at.is_(None),
            )
        ).all()
        for row in rows:
            sent = notify_hr_docs_survey_consent_issue(db, row, "timeout")
            now = datetime.now(timezone.utc)
            row.docs_survey_pd_consent_status = "timed_out"
            row.docs_survey_pd_consent_at = now
            row.status = AssessmentSessionStatus.CANCELLED.value
            row.phase = SessionPhase.PART1.value
            row.completed_at = now
            row.docs_survey_exam_gate_awaiting = False
            if sent:
                row.docs_survey_hr_notified_no_consent_at = now
            db.commit()
            db.refresh(row)
            n += 1
            _log.info(
                "docs_survey_consent_timeout: сессия %s… помечена timed_out (HR notified=%s)",
                row.id[:8],
                sent,
            )
    except Exception:
        _log.exception("docs_survey_consent_timeout: ошибка обработки")
        db.rollback()
    finally:
        db.close()
    return n


async def run_docs_survey_consent_timeout_loop() -> None:
    """Фоновый цикл: таймаут согласия ПДн, таймаут между ответами экзамена и напоминания по слоту."""
    while True:
        try:
            await asyncio.to_thread(process_consent_timeouts_once)
            await asyncio.to_thread(process_examination_answer_timeouts_once)
            await asyncio.to_thread(process_docs_survey_30m_reminders_once)
            await asyncio.sleep(POLL_INTERVAL_SEC)
        except asyncio.CancelledError:
            raise
        except Exception:
            _log.exception("docs_survey_consent_timeout_loop")


def start_consent_timeout_background_task() -> asyncio.Task[None]:
    return asyncio.create_task(
        run_docs_survey_consent_timeout_loop(), name="skill_assessment_docs_survey_consent_timeout"
    )
