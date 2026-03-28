"""Таймаут между ответами на экзамене по регламентам: прервать сессию и уведомить HR."""

from __future__ import annotations

import logging

from sqlalchemy import select

from app.db import SessionLocal
from skill_assessment.domain.examination_entities import ExaminationPhase
from skill_assessment.infrastructure.db_models import ExaminationSessionRow
from skill_assessment.services import examination_service as ex

_log = logging.getLogger(__name__)


def process_examination_answer_timeouts_once() -> int:
    db = SessionLocal()
    n = 0
    try:
        rows = db.scalars(
            select(ExaminationSessionRow).where(
                ExaminationSessionRow.phase == ExaminationPhase.QUESTIONS.value,
            )
        ).all()
        for row in rows:
            if ex.ensure_not_answer_timed_out(db, row, notify_hr=True).phase == ExaminationPhase.INTERRUPTED_TIMEOUT.value:
                n += 1
        if n:
            _log.info("examination_answer_timeout: interrupted %s session(s)", n)
    except Exception:
        _log.exception("examination_answer_timeout: processing failed")
        db.rollback()
    finally:
        db.close()
    return n
