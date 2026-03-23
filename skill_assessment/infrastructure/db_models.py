# route: (ORM / tables sa_*) | file: skill_assessment/infrastructure/db_models.py
"""
ORM-модели skill assessment на общем Base ядра (один SQLite с typical_infrastructure).

Импортируйте этот модуль до загрузки app.main, чтобы create_all увидел таблицы.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Integer, String, Text, UniqueConstraint
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
    phase: Mapped[str] = mapped_column(String(32), nullable=False, default="draft", index=True)
    started_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    #: Куда ушло первое уведомление Part1 (для проверки callback inline-календаря без обязательной POST-привязки).
    docs_survey_notify_chat_id: Mapped[str | None] = mapped_column(String(32), nullable=True)
    #: Выбранный в боте слот опроса по документам (дата+время, локальное время без TZ).
    docs_survey_scheduled_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    #: Согласие на ПДн для Part1: ``awaiting_first`` / ``accepted`` / ``declined`` / ``timed_out`` / ``None``.
    docs_survey_pd_consent_status: Mapped[str | None] = mapped_column(String(16), nullable=True)
    docs_survey_pd_consent_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    #: Когда отправлено первое сообщение с запросом согласия (для таймаута 10 мин).
    docs_survey_consent_prompt_sent_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    #: Когда уже уведомили HR об отказе/молчании (чтобы не слать повторно).
    docs_survey_hr_notified_no_consent_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    #: Напоминание за N минут до слота (готовность) уже отправлено.
    docs_survey_reminder_30m_sent_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    #: Ответ на напоминание готовности: ``ready`` / ``not_ready`` (заглушка под будущую логику).
    docs_survey_readiness_answer: Mapped[str | None] = mapped_column(String(16), nullable=True)
    #: Ожидаем текстовый ответ на «время пришло, готовы к вопросам по регламентам?» (перед экзаменом).
    docs_survey_exam_gate_awaiting: Mapped[bool] = mapped_column(default=False, nullable=False)

    results: Mapped[list[SkillAssessmentResultRow]] = relationship(
        back_populates="session", cascade="all, delete-orphan"
    )
    part1_turns: Mapped[list["SessionPart1TurnRow"]] = relationship(
        back_populates="session", cascade="all, delete-orphan", order_by="SessionPart1TurnRow.seq"
    )


class SessionPart1TurnRow(Base):
    """Реплики Part 1 (устовое интервью): храним текст; аудио — внешний слой STT/TTS."""

    __tablename__ = "sa_session_part1_turns"
    __table_args__ = (UniqueConstraint("session_id", "seq", name="uq_sa_part1_session_seq"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    session_id: Mapped[str] = mapped_column(String(36), ForeignKey("sa_assessment_sessions.id"), nullable=False, index=True)
    seq: Mapped[int] = mapped_column(Integer, nullable=False)
    role: Mapped[str] = mapped_column(String(16), nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, nullable=False)

    session: Mapped[AssessmentSessionRow] = relationship(back_populates="part1_turns")


class SkillAssessmentResultRow(Base, TimestampMixin):
    __tablename__ = "sa_skill_assessment_results"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    session_id: Mapped[str] = mapped_column(String(36), ForeignKey("sa_assessment_sessions.id"), nullable=False, index=True)
    skill_id: Mapped[str] = mapped_column(String(36), ForeignKey("sa_skills.id"), nullable=False, index=True)
    level: Mapped[int] = mapped_column(Integer, nullable=False)
    evidence_json: Mapped[str | None] = mapped_column(Text, nullable=True)

    session: Mapped[AssessmentSessionRow] = relationship(back_populates="results")


class ExaminationQuestionRow(Base, TimestampMixin):
    """Фиксированные вопросы сценария экзамена (MVP: regulation_v1)."""

    __tablename__ = "sa_examination_questions"
    __table_args__ = (UniqueConstraint("scenario_id", "seq", name="uq_sa_exam_question_scenario_seq"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    scenario_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    seq: Mapped[int] = mapped_column(Integer, nullable=False)
    text: Mapped[str] = mapped_column(Text, nullable=False)


class ExaminationSessionRow(Base, TimestampMixin):
    """Сессия экзамена по регламентам (отдельно от sa_assessment_sessions)."""

    __tablename__ = "sa_examination_sessions"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    client_id: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    employee_id: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    scenario_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="scheduled")
    phase: Mapped[str] = mapped_column(String(32), nullable=False, default="consent", index=True)
    consent_status: Mapped[str] = mapped_column(String(32), nullable=False, default="pending")
    needs_hr_release: Mapped[bool] = mapped_column(default=False, nullable=False)
    current_question_index: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    access_window_starts_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    access_window_ends_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    started_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    # Непредсказуемый токен для веба (персональная ссылка без входа в портал); выдаётся при создании.
    access_token: Mapped[str | None] = mapped_column(String(128), nullable=True, unique=True, index=True)

    answers: Mapped[list["ExaminationAnswerRow"]] = relationship(
        back_populates="session", cascade="all, delete-orphan", order_by="ExaminationAnswerRow.created_at"
    )


class ExaminationTelegramBindingRow(Base, TimestampMixin):
    """Привязка Telegram chat_id к сотруднику в ядре (client_id + employee_id)."""

    __tablename__ = "sa_examination_telegram_bindings"
    __table_args__ = (UniqueConstraint("telegram_chat_id", name="uq_sa_exam_tg_chat"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    telegram_chat_id: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    client_id: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    employee_id: Mapped[str] = mapped_column(String(32), nullable=False, index=True)


class ExaminationAnswerRow(Base):
    """Ответ по вопросу (в протоколе — текст / транскрипт)."""

    __tablename__ = "sa_examination_answers"
    __table_args__ = (UniqueConstraint("session_id", "question_id", name="uq_sa_exam_answer_session_question"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    session_id: Mapped[str] = mapped_column(String(36), ForeignKey("sa_examination_sessions.id"), nullable=False, index=True)
    question_id: Mapped[str] = mapped_column(String(36), ForeignKey("sa_examination_questions.id"), nullable=False, index=True)
    transcript_text: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, nullable=False)

    session: Mapped[ExaminationSessionRow] = relationship(back_populates="answers")
