# route: (service) | file: skill_assessment/services/llm_post_stt_blacklist.py
"""Чёрный список для текста после STT: блокирует цепочку с LLM (оценка, протокол и т.д.)."""

from __future__ import annotations

import logging
import re
import uuid

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session

from skill_assessment.infrastructure.db_models import LlmPostSttBlacklistRow

_log = logging.getLogger(__name__)

_MAX_REGEX_LEN = 2000
_MAX_PATTERN_ROWS = 500


def _norm(s: str) -> str:
    return (s or "").strip()


def transcript_matches_blacklist(db: Session, text: str) -> tuple[bool, LlmPostSttBlacklistRow | None]:
    """
    Возвращает (True, запись), если активное правило сработало.
    Подстрока: без учёта регистра. Regex: флаги re.IGNORECASE | re.DOTALL при необходимости.
    """
    raw = _norm(text)
    if not raw:
        return False, None

    rows = db.scalars(
        select(LlmPostSttBlacklistRow)
        .where(LlmPostSttBlacklistRow.is_active.is_(True))
        .order_by(LlmPostSttBlacklistRow.created_at.asc())
        .limit(_MAX_PATTERN_ROWS)
    ).all()

    for row in rows:
        mode = (row.match_mode or "substring").strip().lower()
        pat = row.pattern or ""
        if not pat.strip():
            continue
        try:
            if mode == "regex":
                if len(pat) > _MAX_REGEX_LEN:
                    continue
                if re.search(pat, raw, flags=re.IGNORECASE | re.DOTALL):
                    return True, row
            else:
                if pat.lower() in raw.lower():
                    return True, row
        except re.error as e:
            _log.warning("llm_post_stt_blacklist: invalid regex id=%s: %s", row.id[:8], e)
            continue
    return False, None


def assert_user_text_allowed_after_stt(db: Session, text: str) -> None:
    """Перед сохранением реплики ``user`` и вызовом LLM: 422 если транскрипт в чёрном списке."""
    blocked, row = transcript_matches_blacklist(db, text)
    if not blocked or row is None:
        return
    raise HTTPException(
        status_code=422,
        detail={
            "code": "llm_post_stt_blacklisted",
            "pattern_id": row.id,
            "match_mode": row.match_mode,
            "description": row.description,
        },
    )


def list_blacklist(db: Session) -> list[LlmPostSttBlacklistRow]:
    return list(
        db.scalars(select(LlmPostSttBlacklistRow).order_by(LlmPostSttBlacklistRow.created_at.desc())).all()
    )


def create_blacklist_row(
    db: Session,
    *,
    pattern: str,
    match_mode: str = "substring",
    description: str | None = None,
    is_active: bool = True,
) -> LlmPostSttBlacklistRow:
    p = _norm(pattern)
    if not p:
        raise HTTPException(status_code=400, detail="blacklist_pattern_empty")
    mode = (match_mode or "substring").strip().lower()
    if mode not in ("substring", "regex"):
        raise HTTPException(status_code=400, detail="blacklist_invalid_match_mode")
    if mode == "regex" and len(p) > _MAX_REGEX_LEN:
        raise HTTPException(status_code=400, detail="blacklist_regex_too_long")
    if mode == "regex":
        try:
            re.compile(p)
        except re.error as e:
            raise HTTPException(status_code=400, detail=f"blacklist_invalid_regex:{e}") from e

    row = LlmPostSttBlacklistRow(
        id=str(uuid.uuid4()),
        pattern=p,
        match_mode=mode,
        is_active=is_active,
        description=(description.strip() if description else None) or None,
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    return row


def delete_blacklist_row(db: Session, row_id: str) -> None:
    row = db.get(LlmPostSttBlacklistRow, row_id)
    if row is None:
        raise HTTPException(status_code=404, detail="blacklist_row_not_found")
    db.delete(row)
    db.commit()


def set_blacklist_active(db: Session, row_id: str, is_active: bool) -> LlmPostSttBlacklistRow:
    row = db.get(LlmPostSttBlacklistRow, row_id)
    if row is None:
        raise HTTPException(status_code=404, detail="blacklist_row_not_found")
    row.is_active = is_active
    db.commit()
    db.refresh(row)
    return row
