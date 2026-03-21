"""Создание сессий и фиксация результатов."""

from __future__ import annotations

import json
import uuid
from datetime import datetime

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session

from skill_assessment.domain.entities import AssessmentSessionStatus, EvidenceKind, ProficiencyLevel
from skill_assessment.infrastructure.db_models import (
    AssessmentSessionRow,
    SkillAssessmentResultRow,
    SkillDomainRow,
    SkillRow,
)
from skill_assessment.schemas.api import (
    AssessmentSessionOut,
    SessionCreate,
    SkillAssessmentResultOut,
    SkillDomainOut,
    SkillOut,
    SkillResultCreate,
)


def _session_out(row: AssessmentSessionRow) -> AssessmentSessionOut:
    return AssessmentSessionOut(
        id=row.id,
        client_id=row.client_id,
        employee_id=row.employee_id,
        status=AssessmentSessionStatus(row.status),
        started_at=row.started_at,
        completed_at=row.completed_at,
        created_at=row.created_at,
        updated_at=row.updated_at,
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


def list_sessions(db: Session, client_id: str | None, limit: int = 50) -> list[AssessmentSessionOut]:
    q = select(AssessmentSessionRow).order_by(AssessmentSessionRow.created_at.desc()).limit(limit)
    if client_id:
        q = q.where(AssessmentSessionRow.client_id == client_id)
    rows = db.scalars(q).all()
    return [_session_out(r) for r in rows]


def start_session(db: Session, session_id: str) -> AssessmentSessionOut:
    row = db.get(AssessmentSessionRow, session_id)
    if row is None:
        raise HTTPException(status_code=404, detail="session_not_found")
    if row.status != AssessmentSessionStatus.DRAFT.value:
        raise HTTPException(status_code=400, detail="session_not_draft")
    row.status = AssessmentSessionStatus.IN_PROGRESS.value
    row.started_at = datetime.utcnow()
    db.commit()
    db.refresh(row)
    return _session_out(row)


def complete_session(db: Session, session_id: str) -> AssessmentSessionOut:
    row = db.get(AssessmentSessionRow, session_id)
    if row is None:
        raise HTTPException(status_code=404, detail="session_not_found")
    row.status = AssessmentSessionStatus.COMPLETED.value
    row.completed_at = datetime.utcnow()
    db.commit()
    db.refresh(row)
    return _session_out(row)


def add_result(db: Session, session_id: str, body: SkillResultCreate) -> SkillAssessmentResultOut:
    session = db.get(AssessmentSessionRow, session_id)
    if session is None:
        raise HTTPException(status_code=404, detail="session_not_found")
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
