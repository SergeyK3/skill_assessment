"""
ORM-модели skill assessment на общем Base ядра (один SQLite с typical_infrastructure).

Импортируйте этот модуль до загрузки app.main, чтобы create_all увидел таблицы.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db import Base


def utcnow() -> datetime:
    return datetime.utcnow()


class TimestampMixin:
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow, nullable=False)


class SkillDomainRow(Base, TimestampMixin):
    __tablename__ = "sa_skill_domains"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    code: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    title: Mapped[str] = mapped_column(String(256), nullable=False)

    skills: Mapped[list[SkillRow]] = relationship(back_populates="domain", cascade="all, delete-orphan")


class SkillRow(Base, TimestampMixin):
    __tablename__ = "sa_skills"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    domain_id: Mapped[str] = mapped_column(String(36), ForeignKey("sa_skill_domains.id"), nullable=False, index=True)
    code: Mapped[str] = mapped_column(String(64), nullable=False)
    title: Mapped[str] = mapped_column(String(256), nullable=False)

    domain: Mapped[SkillDomainRow] = relationship(back_populates="skills")


class AssessmentSessionRow(Base, TimestampMixin):
    __tablename__ = "sa_assessment_sessions"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    client_id: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    employee_id: Mapped[str | None] = mapped_column(String(32), nullable=True, index=True)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="draft")
    started_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    results: Mapped[list[SkillAssessmentResultRow]] = relationship(
        back_populates="session", cascade="all, delete-orphan"
    )


class SkillAssessmentResultRow(Base, TimestampMixin):
    __tablename__ = "sa_skill_assessment_results"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    session_id: Mapped[str] = mapped_column(String(36), ForeignKey("sa_assessment_sessions.id"), nullable=False, index=True)
    skill_id: Mapped[str] = mapped_column(String(36), ForeignKey("sa_skills.id"), nullable=False, index=True)
    level: Mapped[int] = mapped_column(Integer, nullable=False)
    evidence_json: Mapped[str | None] = mapped_column(Text, nullable=True)

    session: Mapped[AssessmentSessionRow] = relationship(back_populates="results")
