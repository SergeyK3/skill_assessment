"""API DTO (Pydantic v2)."""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, Field

from skill_assessment.domain.entities import AssessmentSessionStatus, EvidenceKind, ProficiencyLevel


class SessionCreate(BaseModel):
    client_id: str = Field(..., description="ID организации (clients.id в ядре)")
    employee_id: str | None = Field(default=None, description="Сотрудник, если есть")


class AssessmentSessionOut(BaseModel):
    id: str
    client_id: str
    employee_id: str | None
    status: AssessmentSessionStatus
    started_at: datetime | None
    completed_at: datetime | None
    created_at: datetime
    updated_at: datetime

    model_config = {"from_attributes": False}


class SkillDomainOut(BaseModel):
    id: str
    code: str
    title: str


class SkillOut(BaseModel):
    id: str
    domain_id: str
    code: str
    title: str


class SkillResultCreate(BaseModel):
    skill_id: str
    level: ProficiencyLevel
    evidence_notes: dict[EvidenceKind, str | None] = Field(default_factory=dict)


class SkillAssessmentResultOut(BaseModel):
    id: str
    session_id: str
    skill_id: str
    level: ProficiencyLevel
    evidence_notes: dict[EvidenceKind, str | None]
    created_at: datetime
    updated_at: datetime
