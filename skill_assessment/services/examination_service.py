# route: (service examination) | file: skill_assessment/services/examination_service.py

from __future__ import annotations

import secrets
import uuid
from datetime import datetime

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session

from skill_assessment.domain.examination_entities import (
    ConsentStatus,
    ExaminationPhase,
    ExaminationSessionStatus,
)
from skill_assessment.infrastructure.db_models import (
    ExaminationAnswerRow,
    ExaminationQuestionRow,
    ExaminationSessionRow,
    ExaminationTelegramBindingRow,
)
from skill_assessment.schemas.examination_api import (
    ExaminationAnswerBody,
    ExaminationConsentBody,
    ExaminationIntroDoneBody,
    ExaminationProtocolItemOut,
    ExaminationProtocolOut,
    ExaminationQuestionOut,
    ExaminationSessionCreate,
    ExaminationSessionOut,
)
from skill_assessment.services.examination_seed import SCENARIO_REGULATION_V1


def _ordered_questions(db: Session, scenario_id: str) -> list[ExaminationQuestionRow]:
    q = (
        select(ExaminationQuestionRow)
        .where(ExaminationQuestionRow.scenario_id == scenario_id)
        .order_by(ExaminationQuestionRow.seq.asc())
    )
    return list(db.scalars(q).all())


def _question_count(db: Session, scenario_id: str) -> int:
    return len(_ordered_questions(db, scenario_id))


def _session_out(db: Session, row: ExaminationSessionRow, *, include_access_token: bool = False) -> ExaminationSessionOut:
    n = _question_count(db, row.scenario_id)
    return ExaminationSessionOut(
        id=row.id,
        client_id=row.client_id,
        employee_id=row.employee_id,
        scenario_id=row.scenario_id,
        status=ExaminationSessionStatus(row.status),
        phase=ExaminationPhase(row.phase),
        consent_status=ConsentStatus(row.consent_status),
        needs_hr_release=bool(row.needs_hr_release),
        current_question_index=int(row.current_question_index),
        question_count=n,
        access_window_starts_at=row.access_window_starts_at,
        access_window_ends_at=row.access_window_ends_at,
        started_at=row.started_at,
        completed_at=row.completed_at,
        created_at=row.created_at,
        updated_at=row.updated_at,
        access_token=(row.access_token if include_access_token else None),
    )


def _insert_examination_session_row(
    db: Session,
    *,
    client_id: str,
    employee_id: str,
    scenario_id: str,
    access_window_starts_at,
    access_window_ends_at,
) -> ExaminationSessionRow:
    sid = str(uuid.uuid4())
    row = ExaminationSessionRow(
        id=sid,
        client_id=client_id,
        employee_id=employee_id,
        scenario_id=scenario_id,
        status=ExaminationSessionStatus.SCHEDULED.value,
        phase=ExaminationPhase.CONSENT.value,
        consent_status=ConsentStatus.PENDING.value,
        needs_hr_release=False,
        current_question_index=0,
        access_window_starts_at=access_window_starts_at,
        access_window_ends_at=access_window_ends_at,
        started_at=None,
        completed_at=None,
        access_token=secrets.token_urlsafe(32),
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    return row


def create_examination_session(db: Session, body: ExaminationSessionCreate) -> ExaminationSessionOut:
    if body.scenario_id != SCENARIO_REGULATION_V1:
        raise HTTPException(status_code=400, detail="unsupported_scenario_mvp")
    if _question_count(db, body.scenario_id) == 0:
        raise HTTPException(status_code=500, detail="examination_questions_not_seeded")
    row = _insert_examination_session_row(
        db,
        client_id=body.client_id,
        employee_id=body.employee_id,
        scenario_id=body.scenario_id,
        access_window_starts_at=body.access_window_starts_at,
        access_window_ends_at=body.access_window_ends_at,
    )
    return _session_out(db, row, include_access_token=True)


def upsert_telegram_binding(db: Session, client_id: str, employee_id: str, telegram_chat_id: str) -> dict[str, str]:
    tid = str(telegram_chat_id).strip()
    if not tid:
        raise HTTPException(status_code=400, detail="telegram_chat_id_required")
    row = db.scalars(
        select(ExaminationTelegramBindingRow).where(ExaminationTelegramBindingRow.telegram_chat_id == tid)
    ).first()
    if row is None:
        row = ExaminationTelegramBindingRow(
            id=str(uuid.uuid4()),
            telegram_chat_id=tid,
            client_id=client_id,
            employee_id=employee_id,
        )
        db.add(row)
    else:
        row.client_id = client_id
        row.employee_id = employee_id
    db.commit()
    db.refresh(row)
    return {"id": row.id, "telegram_chat_id": row.telegram_chat_id, "client_id": row.client_id, "employee_id": row.employee_id}


def get_telegram_binding(db: Session, telegram_chat_id: str) -> ExaminationTelegramBindingRow | None:
    tid = str(telegram_chat_id).strip()
    return db.scalars(
        select(ExaminationTelegramBindingRow).where(ExaminationTelegramBindingRow.telegram_chat_id == tid)
    ).first()


def get_telegram_binding_for_employee(db: Session, client_id: str, employee_id: str) -> ExaminationTelegramBindingRow | None:
    """Привязка Telegram к сотруднику (если ранее регистрировали POST …/examination/telegram/bindings)."""
    return db.scalars(
        select(ExaminationTelegramBindingRow).where(
            ExaminationTelegramBindingRow.client_id == client_id,
            ExaminationTelegramBindingRow.employee_id == employee_id,
        )
    ).first()


def get_or_create_active_examination_session(db: Session, client_id: str, employee_id: str) -> ExaminationSessionRow:
    """Активная (не завершённая) сессия или новая."""
    if _question_count(db, SCENARIO_REGULATION_V1) == 0:
        raise HTTPException(status_code=500, detail="examination_questions_not_seeded")
    q = (
        select(ExaminationSessionRow)
        .where(
            ExaminationSessionRow.client_id == client_id,
            ExaminationSessionRow.employee_id == employee_id,
            ExaminationSessionRow.status != ExaminationSessionStatus.COMPLETED.value,
        )
        .order_by(ExaminationSessionRow.created_at.desc())
    )
    row = db.scalars(q).first()
    if row is not None:
        return row
    return _insert_examination_session_row(
        db,
        client_id=client_id,
        employee_id=employee_id,
        scenario_id=SCENARIO_REGULATION_V1,
        access_window_starts_at=None,
        access_window_ends_at=None,
    )


def get_examination_session(db: Session, session_id: str) -> ExaminationSessionOut:
    row = db.get(ExaminationSessionRow, session_id)
    if row is None:
        raise HTTPException(status_code=404, detail="examination_session_not_found")
    return _session_out(db, row, include_access_token=False)


def get_examination_session_by_access_token(db: Session, access_token: str) -> ExaminationSessionOut:
    """Сессия по секрету из персональной ссылки (веб без входа в портал)."""
    t = (access_token or "").strip()
    if not t:
        raise HTTPException(status_code=404, detail="examination_session_not_found")
    row = db.scalars(
        select(ExaminationSessionRow).where(ExaminationSessionRow.access_token == t)
    ).first()
    if row is None:
        raise HTTPException(status_code=404, detail="examination_session_not_found")
    return _session_out(db, row, include_access_token=True)


def list_examination_sessions(
    db: Session,
    client_id: str | None,
    employee_id: str | None,
    limit: int = 50,
) -> list[ExaminationSessionOut]:
    q = select(ExaminationSessionRow).order_by(ExaminationSessionRow.created_at.desc()).limit(limit)
    if client_id:
        q = q.where(ExaminationSessionRow.client_id == client_id)
    if employee_id:
        q = q.where(ExaminationSessionRow.employee_id == employee_id)
    rows = db.scalars(q).all()
    return [_session_out(db, r) for r in rows]


def list_scenario_questions(db: Session, scenario_id: str) -> list[ExaminationQuestionOut]:
    rows = _ordered_questions(db, scenario_id)
    return [
        ExaminationQuestionOut(id=r.id, scenario_id=r.scenario_id, seq=r.seq, text=r.text) for r in rows
    ]


def get_current_question(db: Session, session_id: str) -> ExaminationQuestionOut | None:
    row = db.get(ExaminationSessionRow, session_id)
    if row is None:
        raise HTTPException(status_code=404, detail="examination_session_not_found")
    if ExaminationPhase(row.phase) != ExaminationPhase.QUESTIONS:
        return None
    qs = _ordered_questions(db, row.scenario_id)
    idx = int(row.current_question_index)
    if idx < 0 or idx >= len(qs):
        return None
    r = qs[idx]
    return ExaminationQuestionOut(id=r.id, scenario_id=r.scenario_id, seq=r.seq, text=r.text)


def post_consent(db: Session, session_id: str, body: ExaminationConsentBody) -> ExaminationSessionOut:
    row = db.get(ExaminationSessionRow, session_id)
    if row is None:
        raise HTTPException(status_code=404, detail="examination_session_not_found")
    if ExaminationPhase(row.phase) == ExaminationPhase.BLOCKED_CONSENT:
        raise HTTPException(status_code=403, detail="consent_blocked_needs_hr")
    if ExaminationPhase(row.phase) != ExaminationPhase.CONSENT:
        raise HTTPException(status_code=400, detail="consent_not_expected_in_this_phase")
    if not body.accepted:
        row.consent_status = ConsentStatus.DECLINED.value
        row.phase = ExaminationPhase.BLOCKED_CONSENT.value
        row.needs_hr_release = True
        db.commit()
        db.refresh(row)
        return _session_out(db, row)
    row.consent_status = ConsentStatus.ACCEPTED.value
    row.phase = ExaminationPhase.INTRO.value
    row.status = ExaminationSessionStatus.IN_PROGRESS.value
    if row.started_at is None:
        row.started_at = datetime.utcnow()
    row.needs_hr_release = False
    db.commit()
    db.refresh(row)
    return _session_out(db, row)


def hr_release_consent_block(db: Session, session_id: str) -> ExaminationSessionOut:
    """Снимает блок после отказа от согласия (роль HR — заглушка без auth в MVP)."""
    row = db.get(ExaminationSessionRow, session_id)
    if row is None:
        raise HTTPException(status_code=404, detail="examination_session_not_found")
    if ExaminationPhase(row.phase) != ExaminationPhase.BLOCKED_CONSENT:
        raise HTTPException(status_code=400, detail="hr_release_not_needed")
    row.phase = ExaminationPhase.CONSENT.value
    row.consent_status = ConsentStatus.PENDING.value
    row.needs_hr_release = False
    db.commit()
    db.refresh(row)
    return _session_out(db, row)


def post_intro_done(db: Session, session_id: str, body: ExaminationIntroDoneBody) -> ExaminationSessionOut:
    row = db.get(ExaminationSessionRow, session_id)
    if row is None:
        raise HTTPException(status_code=404, detail="examination_session_not_found")
    if ExaminationPhase(row.phase) != ExaminationPhase.INTRO:
        raise HTTPException(status_code=400, detail="intro_not_expected")
    if not body.ready:
        raise HTTPException(status_code=400, detail="intro_not_ready")
    row.phase = ExaminationPhase.QUESTIONS.value
    row.current_question_index = 0
    db.commit()
    db.refresh(row)
    return _session_out(db, row)


def post_answer(db: Session, session_id: str, body: ExaminationAnswerBody) -> ExaminationSessionOut:
    row = db.get(ExaminationSessionRow, session_id)
    if row is None:
        raise HTTPException(status_code=404, detail="examination_session_not_found")
    if ExaminationPhase(row.phase) != ExaminationPhase.QUESTIONS:
        raise HTTPException(status_code=400, detail="answers_not_expected_in_this_phase")
    qs = _ordered_questions(db, row.scenario_id)
    idx = int(row.current_question_index)
    if idx < 0 or idx >= len(qs):
        raise HTTPException(status_code=400, detail="no_current_question")
    qrow = qs[idx]
    existing = db.scalars(
        select(ExaminationAnswerRow).where(
            ExaminationAnswerRow.session_id == row.id,
            ExaminationAnswerRow.question_id == qrow.id,
        )
    ).first()
    if existing:
        raise HTTPException(status_code=400, detail="answer_already_recorded_use_resume_flow")
    db.add(
        ExaminationAnswerRow(
            id=str(uuid.uuid4()),
            session_id=row.id,
            question_id=qrow.id,
            transcript_text=body.transcript_text,
        )
    )
    idx_next = idx + 1
    row.current_question_index = idx_next
    if idx_next >= len(qs):
        row.phase = ExaminationPhase.PROTOCOL.value
    db.commit()
    db.refresh(row)
    return _session_out(db, row)


def build_protocol(db: Session, session_id: str) -> ExaminationProtocolOut:
    row = db.get(ExaminationSessionRow, session_id)
    if row is None:
        raise HTTPException(status_code=404, detail="examination_session_not_found")
    ph = ExaminationPhase(row.phase)
    if ph not in (ExaminationPhase.PROTOCOL, ExaminationPhase.COMPLETED):
        raise HTTPException(status_code=400, detail="protocol_not_ready")
    qs = _ordered_questions(db, row.scenario_id)
    answers = {
        a.question_id: a
        for a in db.scalars(
            select(ExaminationAnswerRow).where(ExaminationAnswerRow.session_id == row.id)
        ).all()
    }
    items: list[ExaminationProtocolItemOut] = []
    for q in qs:
        ans = answers.get(q.id)
        items.append(
            ExaminationProtocolItemOut(
                question_id=q.id,
                seq=q.seq,
                question_text=q.text,
                transcript_text=ans.transcript_text if ans else "",
            )
        )
    return ExaminationProtocolOut(
        session_id=row.id,
        scenario_id=row.scenario_id,
        employee_id=row.employee_id,
        client_id=row.client_id,
        items=items,
        completed_at=row.completed_at,
    )


def complete_examination_session(db: Session, session_id: str) -> ExaminationSessionOut:
    row = db.get(ExaminationSessionRow, session_id)
    if row is None:
        raise HTTPException(status_code=404, detail="examination_session_not_found")
    if ExaminationPhase(row.phase) != ExaminationPhase.PROTOCOL:
        raise HTTPException(status_code=400, detail="complete_only_from_protocol_phase")
    row.phase = ExaminationPhase.COMPLETED.value
    row.status = ExaminationSessionStatus.COMPLETED.value
    row.completed_at = datetime.utcnow()
    db.commit()
    db.refresh(row)
    return _session_out(db, row)
