"""HTTP routes for skill assessment (MVP stub)."""

from __future__ import annotations

from fastapi import APIRouter

from skill_assessment.domain.entities import (
    AssessmentSession,
    Skill,
    SkillAssessmentResult,
    SkillDomain,
)

router = APIRouter(prefix="/skill-assessment", tags=["skill-assessment"])


@router.get("/health")
def skill_assessment_health() -> dict:
    """Liveness for the skill-assessment plugin."""
    return {"status": "ok", "module": "skill_assessment"}


@router.get("/domain/json-schema")
def domain_json_schema() -> dict:
    """JSON Schema черновых сущностей (без БД, для контракта и Swagger)."""
    return {
        "SkillDomain": SkillDomain.model_json_schema(),
        "Skill": Skill.model_json_schema(),
        "AssessmentSession": AssessmentSession.model_json_schema(),
        "SkillAssessmentResult": SkillAssessmentResult.model_json_schema(),
    }
