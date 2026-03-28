"""Общий мост: матрица компетенций -> рабочие SkillRow/result."""

from __future__ import annotations

import uuid
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.orm import Session

from skill_assessment.infrastructure.db_models import (
    AssessmentSessionRow,
    CompetencyCatalogVersionRow,
    CompetencyMatrixRow,
    CompetencySkillDefinitionRow,
    SkillDomainRow,
    SkillRow,
)
from skill_assessment.integration.hr_core import get_employee

_MATRIX_SKILL_DOMAIN_CODE = "competency_matrix"
_MATRIX_SKILL_DOMAIN_TITLE = "Ключевые навыки из матрицы"


@dataclass(frozen=True)
class SessionCompetencySkill:
    public_skill_id: str
    result_skill_id: str | None
    skill_code: str
    skill_title: str
    is_active: bool
    skill_rank: int


def _active_competency_version_id(db: Session, client_id: str) -> str | None:
    for cid in (client_id, None):
        stmt = select(CompetencyCatalogVersionRow.id).where(
            CompetencyCatalogVersionRow.status == "active",
        )
        if cid is None:
            stmt = stmt.where(CompetencyCatalogVersionRow.client_id.is_(None))
        else:
            stmt = stmt.where(CompetencyCatalogVersionRow.client_id == cid)
        version_id = db.scalar(stmt.order_by(CompetencyCatalogVersionRow.created_at.desc()).limit(1))
        if version_id:
            return version_id
    return None


def _candidate_matrix_rows(db: Session, session_row: AssessmentSessionRow) -> list[CompetencyMatrixRow]:
    version_id = _active_competency_version_id(db, session_row.client_id)
    if not version_id:
        return []
    emp = get_employee(db, session_row.client_id, session_row.employee_id)
    position_code = (getattr(emp, "position_code", None) or "").strip()
    department_code = (getattr(emp, "department_code", None) or "").strip()
    if position_code:
        stmt = (
            select(CompetencyMatrixRow)
            .where(
                CompetencyMatrixRow.version_id == version_id,
                CompetencyMatrixRow.position_code == position_code,
            )
            .order_by(CompetencyMatrixRow.skill_rank.asc(), CompetencyMatrixRow.created_at.asc(), CompetencyMatrixRow.id.asc())
        )
        if department_code:
            rows = list(db.scalars(stmt.where(CompetencyMatrixRow.department_code == department_code)).all())
            if rows:
                return rows
        return list(db.scalars(stmt).all())
    return list(
        db.scalars(
            select(CompetencyMatrixRow)
            .where(CompetencyMatrixRow.version_id == version_id)
            .order_by(
                CompetencyMatrixRow.position_code.asc(),
                CompetencyMatrixRow.department_code.asc(),
                CompetencyMatrixRow.skill_rank.asc(),
                CompetencyMatrixRow.created_at.asc(),
                CompetencyMatrixRow.id.asc(),
            )
        ).all()
    )


def _ensure_matrix_skill_domain(db: Session) -> SkillDomainRow:
    row = db.scalar(
        select(SkillDomainRow)
        .where(SkillDomainRow.code == _MATRIX_SKILL_DOMAIN_CODE)
        .order_by(SkillDomainRow.created_at.asc(), SkillDomainRow.id.asc())
        .limit(1)
    )
    if row is not None:
        return row
    row = SkillDomainRow(
        id=str(uuid.uuid4()),
        code=_MATRIX_SKILL_DOMAIN_CODE,
        title=_MATRIX_SKILL_DOMAIN_TITLE,
    )
    db.add(row)
    db.flush()
    return row


def find_mirrored_skill(db: Session, skill_def: CompetencySkillDefinitionRow) -> SkillRow | None:
    title = (skill_def.title_ru or "").strip() or skill_def.skill_code
    return db.scalar(
        select(SkillRow)
        .where(
            SkillRow.code == skill_def.skill_code,
            SkillRow.title == title,
        )
        .order_by(SkillRow.created_at.asc(), SkillRow.id.asc())
        .limit(1)
    )


def ensure_mirrored_skill(db: Session, skill_def: CompetencySkillDefinitionRow) -> SkillRow:
    existing = find_mirrored_skill(db, skill_def)
    if existing is not None:
        return existing
    domain = _ensure_matrix_skill_domain(db)
    title = (skill_def.title_ru or "").strip() or skill_def.skill_code
    row = SkillRow(
        id=str(uuid.uuid4()),
        domain_id=domain.id,
        code=skill_def.skill_code,
        title=title,
    )
    db.add(row)
    db.flush()
    return row


def list_session_competency_skills(
    db: Session,
    session_row: AssessmentSessionRow,
    *,
    include_inactive: bool = False,
    ensure_result_skills: bool = False,
) -> list[SessionCompetencySkill]:
    rows = _candidate_matrix_rows(db, session_row)
    out: list[SessionCompetencySkill] = []
    seen_skill_ids: set[str] = set()
    for matrix_row in rows:
        skill_def = matrix_row.skill_definition
        if skill_def is None:
            continue
        if not include_inactive and not matrix_row.is_active:
            continue
        if skill_def.id in seen_skill_ids:
            continue
        seen_skill_ids.add(skill_def.id)
        mirrored = ensure_mirrored_skill(db, skill_def) if ensure_result_skills else find_mirrored_skill(db, skill_def)
        out.append(
            SessionCompetencySkill(
                public_skill_id=skill_def.id,
                result_skill_id=mirrored.id if mirrored is not None else None,
                skill_code=skill_def.skill_code,
                skill_title=(skill_def.title_ru or "").strip() or skill_def.skill_code,
                is_active=bool(matrix_row.is_active),
                skill_rank=int(matrix_row.skill_rank),
            )
        )
    return out


def session_competency_skill_map(
    db: Session,
    session_row: AssessmentSessionRow,
    *,
    include_inactive: bool = False,
    ensure_result_skills: bool = False,
) -> dict[str, SessionCompetencySkill]:
    return {
        item.public_skill_id: item
        for item in list_session_competency_skills(
            db,
            session_row,
            include_inactive=include_inactive,
            ensure_result_skills=ensure_result_skills,
        )
    }
