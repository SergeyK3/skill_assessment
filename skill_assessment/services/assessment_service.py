# route: (service layer) | file: skill_assessment/services/assessment_service.py
"""Создание сессий и фиксация результатов."""

from __future__ import annotations

import json
import uuid
from datetime import datetime

from fastapi import HTTPException
from sqlalchemy import and_, func, or_, select
from sqlalchemy.orm import Session

from skill_assessment.domain.entities import AssessmentSessionStatus, EvidenceKind, ProficiencyLevel, SessionPhase
from skill_assessment.infrastructure.db_models import (
    AssessmentSessionRow,
    SkillAssessmentResultRow,
    SkillDomainRow,
    SkillRow,
)
from skill_assessment.schemas.api import (
    AssessmentSessionOut,
    CaseTextOut,
    ManagerRatingsBulk,
    SessionCancelBody,
    SessionCreate,
    SessionPhaseUpdate,
    SkillAssessmentResultOut,
    SkillDomainOut,
    SkillOut,
    SkillResultCreate,
)


def _session_out(row: AssessmentSessionRow) -> AssessmentSessionOut:
    ph = getattr(row, "phase", None) or SessionPhase.DRAFT.value
    return AssessmentSessionOut(
        id=row.id,
        client_id=row.client_id,
        employee_id=row.employee_id,
        status=AssessmentSessionStatus(row.status),
        phase=SessionPhase(ph),
        started_at=row.started_at,
        completed_at=row.completed_at,
        created_at=row.created_at,
        updated_at=row.updated_at,
        docs_survey_pd_consent_status=getattr(row, "docs_survey_pd_consent_status", None),
        docs_survey_pd_consent_at=getattr(row, "docs_survey_pd_consent_at", None),
        docs_survey_scheduled_at=getattr(row, "docs_survey_scheduled_at", None),
        docs_survey_readiness_answer=getattr(row, "docs_survey_readiness_answer", None),
        docs_survey_reminder_30m_sent_at=getattr(row, "docs_survey_reminder_30m_sent_at", None),
    )


def _evidence_to_json(notes: dict[EvidenceKind, str | None]) -> str | None:
    if not notes:
        return None
    d = {k.value: v for k, v in notes.items()}
    return json.dumps(d, ensure_ascii=False)


def _evidence_from_json(raw: str | None) -> dict[EvidenceKind, str | None]:
    if not raw:
        return {}
    data = json.loads(raw)
    out: dict[EvidenceKind, str | None] = {}
    for k, v in data.items():
        try:
            ek = EvidenceKind(k)
            out[ek] = v
        except ValueError:
            continue
    return out


def create_session(db: Session, body: SessionCreate) -> AssessmentSessionOut:
    sid = str(uuid.uuid4())
    row = AssessmentSessionRow(
        id=sid,
        client_id=body.client_id,
        employee_id=body.employee_id,
        status=AssessmentSessionStatus.DRAFT.value,
        phase=SessionPhase.DRAFT.value,
        started_at=None,
        completed_at=None,
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    return _session_out(row)


def get_session(db: Session, session_id: str) -> AssessmentSessionOut:
    row = db.get(AssessmentSessionRow, session_id)
    if row is None:
        raise HTTPException(status_code=404, detail="session_not_found")
    return _session_out(row)


def list_sessions(
    db: Session,
    *,
    client_id: str | None = None,
    employee_id: str | None = None,
    phase: str | None = None,
    status: str | None = None,
    created_from: datetime | None = None,
    created_to: datetime | None = None,
    pd_consent_status: str | None = None,
    q: str | None = None,
    offset: int = 0,
    limit: int = 50,
) -> tuple[list[AssessmentSessionOut], int]:
    """Фильтры + пагинация; ``pd_consent_status=__empty__`` — только без статуса согласия."""
    conditions: list = []
    if client_id:
        cid = client_id.strip()
        conditions.append(AssessmentSessionRow.client_id == cid)
    if employee_id:
        eid = employee_id.strip()
        # Ядро и SQLite могут хранить UUID в разном регистре — сравниваем без учёта регистра.
        conditions.append(func.lower(AssessmentSessionRow.employee_id) == func.lower(eid))
    if phase:
        conditions.append(AssessmentSessionRow.phase == phase)
    if status:
        conditions.append(AssessmentSessionRow.status == status)
    if created_from is not None:
        conditions.append(AssessmentSessionRow.created_at >= created_from)
    if created_to is not None:
        conditions.append(AssessmentSessionRow.created_at <= created_to)
    if pd_consent_status:
        if pd_consent_status == "__empty__":
            conditions.append(AssessmentSessionRow.docs_survey_pd_consent_status.is_(None))
        else:
            conditions.append(AssessmentSessionRow.docs_survey_pd_consent_status == pd_consent_status)
    if q and q.strip():
        qq = f"%{q.strip()}%"
        conditions.append(
            or_(
                AssessmentSessionRow.client_id.ilike(qq),
                AssessmentSessionRow.id.ilike(qq),
                AssessmentSessionRow.employee_id.ilike(qq),
            )
        )

    count_stmt = select(func.count()).select_from(AssessmentSessionRow)
    list_stmt = select(AssessmentSessionRow).order_by(AssessmentSessionRow.created_at.desc())
    if conditions:
        count_stmt = count_stmt.where(and_(*conditions))
        list_stmt = list_stmt.where(and_(*conditions))
    total = int(db.scalar(count_stmt) or 0)
    rows = db.scalars(list_stmt.offset(offset).limit(limit)).all()
    return [_session_out(r) for r in rows], total


def start_session(db: Session, session_id: str) -> AssessmentSessionOut:
    row = db.get(AssessmentSessionRow, session_id)
    if row is None:
        raise HTTPException(status_code=404, detail="session_not_found")
    if row.status != AssessmentSessionStatus.DRAFT.value:
        raise HTTPException(status_code=400, detail="session_not_draft")
    row.status = AssessmentSessionStatus.IN_PROGRESS.value
    row.phase = SessionPhase.PART1.value
    row.started_at = datetime.utcnow()
    db.commit()
    db.refresh(row)
    return _session_out(row)


def cancel_session(db: Session, session_id: str, _body: SessionCancelBody | None = None) -> AssessmentSessionOut:
    """Отмена назначения: снимает «застрявшую» сессию, чтобы можно было создать новую."""
    row = db.get(AssessmentSessionRow, session_id)
    if row is None:
        raise HTTPException(status_code=404, detail="session_not_found")
    if row.status == AssessmentSessionStatus.COMPLETED.value:
        raise HTTPException(status_code=400, detail="session_already_completed")
    if row.status == AssessmentSessionStatus.CANCELLED.value:
        raise HTTPException(status_code=400, detail="session_already_cancelled")
    row.status = AssessmentSessionStatus.CANCELLED.value
    db.commit()
    db.refresh(row)
    return _session_out(row)


def complete_session(db: Session, session_id: str) -> AssessmentSessionOut:
    row = db.get(AssessmentSessionRow, session_id)
    if row is None:
        raise HTTPException(status_code=404, detail="session_not_found")
    if row.status == AssessmentSessionStatus.CANCELLED.value:
        raise HTTPException(status_code=400, detail="session_cancelled")
    row.status = AssessmentSessionStatus.COMPLETED.value
    row.phase = SessionPhase.COMPLETED.value
    row.completed_at = datetime.utcnow()
    db.commit()
    db.refresh(row)
    return _session_out(row)


def set_session_phase(db: Session, session_id: str, body: SessionPhaseUpdate) -> AssessmentSessionOut:
    row = db.get(AssessmentSessionRow, session_id)
    if row is None:
        raise HTTPException(status_code=404, detail="session_not_found")
    row.phase = body.phase.value
    db.commit()
    db.refresh(row)
    return _session_out(row)


def get_case_stub(db: Session, session_id: str, skill_id: str) -> CaseTextOut:
    session = db.get(AssessmentSessionRow, session_id)
    if session is None:
        raise HTTPException(status_code=404, detail="session_not_found")
    if session.status == AssessmentSessionStatus.CANCELLED.value:
        raise HTTPException(status_code=400, detail="session_cancelled")
    skill = db.get(SkillRow, skill_id)
    if skill is None:
        raise HTTPException(status_code=404, detail="skill_not_found")
    text = _template_case_text(skill)
    return CaseTextOut(
        session_id=session_id,
        skill_id=skill.id,
        skill_code=skill.code,
        skill_title=skill.title,
        text=text,
        source="template",
    )


def _template_case_text(skill: SkillRow) -> str:
    return (
        f"**Ситуация (заглушка).** Навык в фокусе: «{skill.title}» ({skill.code}).\n\n"
        "Ключевой клиент требует дополнительные условия, угрожая уйти к конкуренту; "
        "маржа по сделке на грани минимума, регламент ограничивает уступки без согласования. "
        "До принятия решения остался один рабочий день.\n\n"
        "**Вопрос:** Какие шаги вы предпримете и как оформите решение?\n\n"
        "_Позже текст будет сгенерирован моделью по тем же входам, что в демо "
        "(контекст сессии, регламенты, KPI)._"
    )


def save_manager_ratings(db: Session, session_id: str, body: ManagerRatingsBulk) -> list[SkillAssessmentResultOut]:
    session_row = db.get(AssessmentSessionRow, session_id)
    if session_row is None:
        raise HTTPException(status_code=404, detail="session_not_found")
    if session_row.status == AssessmentSessionStatus.CANCELLED.value:
        raise HTTPException(status_code=400, detail="session_cancelled")
    mgr_note = "Оценка руководителя (Part 3)"
    touched: list[SkillAssessmentResultRow] = []
    for item in body.ratings:
        skill = db.get(SkillRow, item.skill_id)
        if skill is None:
            raise HTTPException(status_code=404, detail="skill_not_found")
        existing = db.scalars(
            select(SkillAssessmentResultRow).where(
                SkillAssessmentResultRow.session_id == session_id,
                SkillAssessmentResultRow.skill_id == item.skill_id,
            )
        ).first()
        if existing:
            notes = _evidence_from_json(existing.evidence_json)
            notes[EvidenceKind.MANAGER] = notes.get(EvidenceKind.MANAGER) or mgr_note
            existing.level = int(item.level.value)
            existing.evidence_json = _evidence_to_json(notes)
            touched.append(existing)
        else:
            rid = str(uuid.uuid4())
            row = SkillAssessmentResultRow(
                id=rid,
                session_id=session_id,
                skill_id=item.skill_id,
                level=int(item.level.value),
                evidence_json=_evidence_to_json({EvidenceKind.MANAGER: mgr_note}),
            )
            db.add(row)
            touched.append(row)
    session_row.phase = SessionPhase.PART3.value
    db.commit()
    for r in touched:
        db.refresh(r)
    return [_result_out(r) for r in touched]


def add_result(db: Session, session_id: str, body: SkillResultCreate) -> SkillAssessmentResultOut:
    session = db.get(AssessmentSessionRow, session_id)
    if session is None:
        raise HTTPException(status_code=404, detail="session_not_found")
    if session.status == AssessmentSessionStatus.CANCELLED.value:
        raise HTTPException(status_code=400, detail="session_cancelled")
    skill = db.get(SkillRow, body.skill_id)
    if skill is None:
        raise HTTPException(status_code=404, detail="skill_not_found")

    rid = str(uuid.uuid4())
    row = SkillAssessmentResultRow(
        id=rid,
        session_id=session_id,
        skill_id=body.skill_id,
        level=int(body.level.value),
        evidence_json=_evidence_to_json(body.evidence_notes),
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    return _result_out(row)


def list_results(db: Session, session_id: str) -> list[SkillAssessmentResultOut]:
    session = db.get(AssessmentSessionRow, session_id)
    if session is None:
        raise HTTPException(status_code=404, detail="session_not_found")
    rows = db.scalars(
        select(SkillAssessmentResultRow).where(SkillAssessmentResultRow.session_id == session_id)
    ).all()
    return [_result_out(r) for r in rows]


def _result_out(row: SkillAssessmentResultRow) -> SkillAssessmentResultOut:
    return SkillAssessmentResultOut(
        id=row.id,
        session_id=row.session_id,
        skill_id=row.skill_id,
        level=ProficiencyLevel(row.level),
        evidence_notes=_evidence_from_json(row.evidence_json),
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


def list_domains(db: Session) -> list[SkillDomainOut]:
    rows = db.scalars(select(SkillDomainRow).order_by(SkillDomainRow.code)).all()
    return [SkillDomainOut(id=r.id, code=r.code, title=r.title) for r in rows]


def list_skills(db: Session, domain_id: str | None) -> list[SkillOut]:
    q = select(SkillRow)
    if domain_id:
        q = q.where(SkillRow.domain_id == domain_id)
    rows = db.scalars(q).all()
    return [SkillOut(id=r.id, domain_id=r.domain_id, code=r.code, title=r.title) for r in rows]
