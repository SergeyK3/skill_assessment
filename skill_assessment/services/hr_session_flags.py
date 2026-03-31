# route: (HR UI) | file: skill_assessment/services/hr_session_flags.py
"""Вычисляемые признаки для HR (без отдельных колонок в БД)."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import TYPE_CHECKING

from sqlalchemy import select

from skill_assessment.domain.examination_entities import ExaminationPhase
from skill_assessment.domain.entities import AssessmentSessionStatus
from skill_assessment.infrastructure.db_models import AssessmentSessionRow, ExaminationSessionRow
from skill_assessment.services.part1_docs_checklist import is_docs_checklist_completed

if TYPE_CHECKING:
    from sqlalchemy.orm import Session


def _has_exam_progress(db: "Session | None", row: AssessmentSessionRow) -> bool:
    """
    Есть ли у этого назначения связанная сессия экзамена по регламентам, созданная уже
    после назначения/слота. Это и есть признак, что сотрудник фактически продвинулся дальше
    по процессу и метка «неявка» не нужна.
    """
    if db is None or not row.employee_id:
        return False
    created_at = getattr(row, "created_at", None)
    q = (
        select(ExaminationSessionRow)
        .where(
            ExaminationSessionRow.client_id == row.client_id,
            ExaminationSessionRow.employee_id == row.employee_id,
        )
        .order_by(ExaminationSessionRow.created_at.desc())
        .limit(10)
    )
    rows = db.scalars(q).all()
    for ex in rows:
        if created_at is not None and ex.created_at is not None and ex.created_at < created_at:
            continue
        ph = (getattr(ex, "phase", None) or "").strip()
        if ph in (
            ExaminationPhase.CONSENT.value,
            ExaminationPhase.INTRO.value,
            ExaminationPhase.QUESTIONS.value,
            ExaminationPhase.PROTOCOL.value,
            ExaminationPhase.COMPLETED.value,
            ExaminationPhase.BLOCKED_CONSENT.value,
            ExaminationPhase.BLOCKED_NO_REGULATION.value,
            ExaminationPhase.INTERRUPTED_TIMEOUT.value,
        ):
            return True
    return False


def latest_exam_status_label(db: "Session | None", row: AssessmentSessionRow) -> str | None:
    if db is None or not row.employee_id:
        return None
    created_at = getattr(row, "created_at", None)
    q = (
        select(ExaminationSessionRow)
        .where(
            ExaminationSessionRow.client_id == row.client_id,
            ExaminationSessionRow.employee_id == row.employee_id,
        )
        .order_by(ExaminationSessionRow.created_at.desc())
        .limit(10)
    )
    rows = db.scalars(q).all()
    for ex in rows:
        if created_at is not None and ex.created_at is not None and ex.created_at < created_at:
            continue
        ph = (getattr(ex, "phase", None) or "").strip()
        if ph == ExaminationPhase.COMPLETED.value:
            return "Завершён"
        if ph == ExaminationPhase.INTERRUPTED_TIMEOUT.value:
            return "Прерван: таймаут"
        if ph == ExaminationPhase.BLOCKED_NO_REGULATION.value:
            return "Нет регламента"
        if ph == ExaminationPhase.PROTOCOL.value:
            return "Протокол готов"
        if ph == ExaminationPhase.QUESTIONS.value:
            return "Идёт опрос"
        if ph == ExaminationPhase.INTRO.value:
            return "Опрос: вступление"
        if ph == ExaminationPhase.CONSENT.value:
            return "Опрос: согласие"
        if ph == ExaminationPhase.BLOCKED_CONSENT.value:
            return "Опрос: отказ от согласия"
    return None


def is_hr_no_show(row: AssessmentSessionRow, db: "Session | None" = None) -> bool:
    """
    «Неявка» для списка HR: таймаут согласия ПДн или пропущенный слот опроса по документам,
    если сотрудник так и не явился на этап Part 1.

    Для **отменённых** сессий метка сохраняется (например, отменили после таймаута ПДн или пропуска слота).
    Для **завершённых** — не ставим. Если сотрудник завершил чек-лист документов или у него
    уже появилась связанная экзаменационная сессия, это тоже не «неявка», даже если слот уже в прошлом.
    """
    st = (row.status or "").strip()
    if st == AssessmentSessionStatus.COMPLETED.value:
        return False

    if getattr(row, "docs_survey_pd_consent_status", None) == "timed_out":
        return True

    sched = getattr(row, "docs_survey_scheduled_at", None)
    if sched is None:
        return False

    now_naive = datetime.now(timezone.utc).replace(tzinfo=None)
    if sched >= now_naive:
        return False

    if st not in (AssessmentSessionStatus.IN_PROGRESS.value, AssessmentSessionStatus.CANCELLED.value):
        return False

    ph = (getattr(row, "phase", None) or "").strip()
    if ph in ("part2", "part3", "report"):
        return False
    if is_docs_checklist_completed(row):
        return False
    if _has_exam_progress(db, row):
        return False
    if ph == "part1":
        return True

    return False
