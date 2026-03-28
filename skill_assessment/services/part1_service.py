# route: (service) | file: skill_assessment/services/part1_service.py
"""Part 1: реплики интервью (текст после STT / реплика LLM до TTS)."""

from __future__ import annotations

import uuid

from fastapi import HTTPException
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from skill_assessment.domain.entities import AssessmentSessionStatus, Part1TurnRole, SessionPhase
from skill_assessment.infrastructure.db_models import AssessmentSessionRow, SessionPart1TurnRow
from skill_assessment.schemas.api import Part1TurnOut, Part1TurnsAppend
from skill_assessment.services.llm_post_stt_blacklist import assert_user_text_allowed_after_stt


def turn_row_to_out(row: SessionPart1TurnRow) -> Part1TurnOut:
    return Part1TurnOut(
        id=row.id,
        session_id=row.session_id,
        seq=row.seq,
        role=Part1TurnRole(row.role),
        text=row.content,
        created_at=row.created_at,
    )


def list_part1_turns(db: Session, session_id: str) -> list[Part1TurnOut]:
    session = db.get(AssessmentSessionRow, session_id)
    if session is None:
        raise HTTPException(status_code=404, detail="session_not_found")
    rows = db.scalars(
        select(SessionPart1TurnRow)
        .where(SessionPart1TurnRow.session_id == session_id)
        .order_by(SessionPart1TurnRow.seq)
    ).all()
    return [turn_row_to_out(r) for r in rows]


def append_part1_turns(db: Session, session_id: str, body: Part1TurnsAppend) -> list[Part1TurnOut]:
    row = db.get(AssessmentSessionRow, session_id)
    if row is None:
        raise HTTPException(status_code=404, detail="session_not_found")
    if row.status not in (AssessmentSessionStatus.IN_PROGRESS.value, AssessmentSessionStatus.DRAFT.value):
        raise HTTPException(status_code=400, detail="session_not_active_for_part1")

    max_seq = db.scalar(
        select(func.coalesce(func.max(SessionPart1TurnRow.seq), 0)).where(
            SessionPart1TurnRow.session_id == session_id
        )
    )
    seq = int(max_seq) + 1
    created: list[SessionPart1TurnRow] = []
    for t in body.turns:
        txt = t.text.strip()
        if not txt:
            raise HTTPException(status_code=400, detail="part1_empty_turn")
        if t.role == Part1TurnRole.USER:
            assert_user_text_allowed_after_stt(db, txt)
        tr = SessionPart1TurnRow(
            id=str(uuid.uuid4()),
            session_id=session_id,
            seq=seq,
            role=t.role.value,
            content=txt,
        )
        db.add(tr)
        created.append(tr)
        seq += 1

    if row.status == AssessmentSessionStatus.IN_PROGRESS.value:
        row.phase = SessionPhase.PART1.value

    db.commit()
    for tr in created:
        db.refresh(tr)
    return [turn_row_to_out(tr) for tr in created]
