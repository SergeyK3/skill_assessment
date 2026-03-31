# route: (service) | file: skill_assessment/services/evaluation_reference_table.py
"""Сводная таблица оценок навыков по сессиям (Part 1 + Part 2, без Part 3)."""

from __future__ import annotations

from fastapi import HTTPException
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from skill_assessment.infrastructure.db_models import AssessmentSessionRow
from skill_assessment.schemas.api import SkillEvaluationReferenceListOut, SkillEvaluationReferenceRowOut
from skill_assessment.services import report_service as report_svc


def list_skill_evaluation_reference(
    db: Session,
    client_id: str,
    *,
    session_limit: int = 100,
    session_offset: int = 0,
) -> SkillEvaluationReferenceListOut:
    cid = (client_id or "").strip()
    if not cid:
        return SkillEvaluationReferenceListOut(
            rows=[],
            total_sessions=0,
            session_limit=session_limit,
            session_offset=session_offset,
        )

    count_stmt = select(func.count()).select_from(AssessmentSessionRow).where(AssessmentSessionRow.client_id == cid)
    total_sessions = int(db.scalar(count_stmt) or 0)

    session_ids = list(
        db.scalars(
            select(AssessmentSessionRow.id)
            .where(AssessmentSessionRow.client_id == cid)
            .order_by(AssessmentSessionRow.updated_at.desc())
            .offset(session_offset)
            .limit(session_limit)
        ).all()
    )

    rows: list[SkillEvaluationReferenceRowOut] = []
    for sid in session_ids:
        try:
            rep = report_svc.build_session_report(db, sid)
        except HTTPException:
            continue
        emp = rep.employee_header
        position_label = emp.position_label if emp and emp.position_label else None
        for r in rep.rows:
            vals = [x for x in (r.part1_level, r.part2_level) if x is not None]
            if not vals:
                continue
            avg_f = sum(vals) / len(vals)
            avg_clamped = max(0.0, min(3.0, float(avg_f)))
            pct = int(round(avg_clamped / 3.0 * 100))
            rows.append(
                SkillEvaluationReferenceRowOut(
                    session_id=rep.session.id,
                    client_id=rep.session.client_id,
                    employee_id=rep.session.employee_id,
                    employee_label=rep.employee_label,
                    position_label=position_label,
                    skill_id=r.skill_id,
                    skill_code=r.skill_code,
                    skill_title=r.skill_title,
                    domain_title=r.domain_title,
                    aggregate_level_0_3=round(avg_f, 2),
                    aggregate_pct_0_100=pct,
                    session_completed_at=rep.session.completed_at,
                    session_updated_at=rep.session.updated_at,
                    session_created_at=rep.session.created_at,
                )
            )

    return SkillEvaluationReferenceListOut(
        rows=rows,
        total_sessions=total_sessions,
        session_limit=session_limit,
        session_offset=session_offset,
    )
