"""HTTP routes for skill assessment."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from app.db import get_db
from skill_assessment.domain.entities import (
    AssessmentSession,
    Skill,
    SkillAssessmentResult,
    SkillDomain,
)
from skill_assessment.schemas.api import (
    AssessmentSessionOut,
    SessionCreate,
    SkillAssessmentResultOut,
    SkillDomainOut,
    SkillOut,
    SkillResultCreate,
)
from skill_assessment.services import assessment_service as svc

router = APIRouter(prefix="/skill-assessment", tags=["skill-assessment"])


@router.get("/health")
def skill_assessment_health() -> dict:
    """Liveness for the skill-assessment plugin."""
    return {"status": "ok", "module": "skill_assessment"}


@router.get("/domain/json-schema")
def domain_json_schema() -> dict:
    """JSON Schema черновых сущностей (контракт домена)."""
    return {
        "SkillDomain": SkillDomain.model_json_schema(),
        "Skill": Skill.model_json_schema(),
        "AssessmentSession": AssessmentSession.model_json_schema(),
        "SkillAssessmentResult": SkillAssessmentResult.model_json_schema(),
    }


@router.get("/taxonomy/domains", response_model=list[SkillDomainOut])
def get_domains(db: Annotated[Session, Depends(get_db)]) -> list[SkillDomainOut]:
    return svc.list_domains(db)


@router.get("/taxonomy/skills", response_model=list[SkillOut])
def get_skills(
    db: Annotated[Session, Depends(get_db)],
    domain_id: str | None = Query(default=None, description="Фильтр по домену"),
) -> list[SkillOut]:
    return svc.list_skills(db, domain_id)


@router.post("/sessions", response_model=AssessmentSessionOut)
def post_session(
    body: SessionCreate,
    db: Annotated[Session, Depends(get_db)],
) -> AssessmentSessionOut:
    return svc.create_session(db, body)


@router.get("/sessions", response_model=list[AssessmentSessionOut])
def get_sessions(
    db: Annotated[Session, Depends(get_db)],
    client_id: str | None = Query(default=None),
    limit: int = Query(default=50, ge=1, le=200),
) -> list[AssessmentSessionOut]:
    return svc.list_sessions(db, client_id, limit)


@router.get("/sessions/{session_id}", response_model=AssessmentSessionOut)
def get_session(
    session_id: str,
    db: Annotated[Session, Depends(get_db)],
) -> AssessmentSessionOut:
    return svc.get_session(db, session_id)


@router.post("/sessions/{session_id}/start", response_model=AssessmentSessionOut)
def post_session_start(
    session_id: str,
    db: Annotated[Session, Depends(get_db)],
) -> AssessmentSessionOut:
    return svc.start_session(db, session_id)


@router.post("/sessions/{session_id}/complete", response_model=AssessmentSessionOut)
def post_session_complete(
    session_id: str,
    db: Annotated[Session, Depends(get_db)],
) -> AssessmentSessionOut:
    return svc.complete_session(db, session_id)


@router.post("/sessions/{session_id}/results", response_model=SkillAssessmentResultOut)
def post_result(
    session_id: str,
    body: SkillResultCreate,
    db: Annotated[Session, Depends(get_db)],
) -> SkillAssessmentResultOut:
    return svc.add_result(db, session_id, body)


@router.get("/sessions/{session_id}/results", response_model=list[SkillAssessmentResultOut])
def get_results(
    session_id: str,
    db: Annotated[Session, Depends(get_db)],
) -> list[SkillAssessmentResultOut]:
    return svc.list_results(db, session_id)
