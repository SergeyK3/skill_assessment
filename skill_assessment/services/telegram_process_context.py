"""Процессный контекст Telegram (phase 2 orchestration)."""

from __future__ import annotations

import uuid

from sqlalchemy import select
from sqlalchemy.orm import Session

from skill_assessment.infrastructure.db_models import TelegramProcessContextRow

FLOW_EXAM = "exam"
FLOW_PART2 = "part2_cases"
FLOW_IDLE = "idle"


def get_process_context(db: Session, telegram_chat_id: str) -> TelegramProcessContextRow | None:
    tid = str(telegram_chat_id).strip()
    if not tid:
        return None
    return db.scalar(
        select(TelegramProcessContextRow).where(TelegramProcessContextRow.telegram_chat_id == tid).limit(1)
    )


def set_process_context(
    db: Session,
    telegram_chat_id: str,
    *,
    flow: str,
    client_id: str | None = None,
    employee_id: str | None = None,
    active_session_id: str | None = None,
) -> TelegramProcessContextRow:
    tid = str(telegram_chat_id).strip()
    if not tid:
        raise ValueError("telegram_chat_id_required")
    row = get_process_context(db, tid)
    if row is None:
        row = TelegramProcessContextRow(
            id=str(uuid.uuid4()),
            telegram_chat_id=tid,
            client_id=(client_id or "").strip() or None,
            employee_id=(employee_id or "").strip() or None,
            active_flow=str(flow or FLOW_IDLE).strip() or FLOW_IDLE,
            active_session_id=(active_session_id or "").strip() or None,
        )
        db.add(row)
    else:
        row.active_flow = str(flow or FLOW_IDLE).strip() or FLOW_IDLE
        row.client_id = (client_id or "").strip() or row.client_id
        row.employee_id = (employee_id or "").strip() or row.employee_id
        row.active_session_id = (active_session_id or "").strip() or None
    db.commit()
    db.refresh(row)
    return row
