# route: (Part1 docs survey) | file: skill_assessment/services/part1_docs_checklist.py
"""Опрос по служебным / должностным документам (Part 1): чек-лист по тому же плану, что экзамен по регламенту (ядро HR), до перехода к кейсу."""

from __future__ import annotations

import json
import logging
import os
import secrets
from datetime import datetime, timezone

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session

from skill_assessment.domain.entities import AssessmentSessionStatus, SessionPhase
from skill_assessment.infrastructure.db_models import AssessmentSessionRow
from skill_assessment.schemas.api import Part1DocsChecklistOut, Part1DocsChecklistSave, Part1DocsQuestionOut
from skill_assessment.services.part1_docs_question_plan import build_part1_docs_question_dicts

# Версия набора вопросов: v3 — 3×регламент + KPI + ключевой навык из матриц каталога.
PART1_DOCS_VERSION = "v3"

_log = logging.getLogger(__name__)

ALLOWED_VALUES = frozenset({"yes", "no", "partial"})

# Путь UI (относительно хоста приложения); query `token` подставляет клиент.
PART1_DOCS_EMPLOYEE_UI_PATH = "/api/skill-assessment/ui/part1-docs-checklist"


def ensure_part1_docs_access_token(db: Session, row: AssessmentSessionRow) -> str:
    """Гарантирует одноразовый токен для личной страницы чек-листа (идемпотентно)."""
    existing = getattr(row, "part1_docs_access_token", None)
    if existing and str(existing).strip():
        return str(existing).strip()
    row.part1_docs_access_token = secrets.token_urlsafe(32)
    db.commit()
    db.refresh(row)
    return str(row.part1_docs_access_token)


def get_session_row_by_part1_docs_token(db: Session, token: str) -> AssessmentSessionRow | None:
    t = (token or "").strip()
    if len(t) < 16:
        return None
    return db.scalar(select(AssessmentSessionRow).where(AssessmentSessionRow.part1_docs_access_token == t))


def build_part1_docs_employee_page_absolute_url(db: Session, session_id: str) -> str | None:
    """Полный URL страницы чек-листа для сотрудника; None без ``SKILL_ASSESSMENT_PUBLIC_BASE_URL``."""
    base = os.getenv("SKILL_ASSESSMENT_PUBLIC_BASE_URL", "").strip()
    if not base:
        return None
    row = db.get(AssessmentSessionRow, session_id)
    if row is None:
        return None
    tok = ensure_part1_docs_access_token(db, row)
    return f"{base.rstrip('/')}{PART1_DOCS_EMPLOYEE_UI_PATH}?token={tok}"


def telegram_part1_docs_checklist_message_line(db: Session, session_id: str) -> str | None:
    """Строка для Telegram с абсолютной ссылкой на чек-лист; None если базовый URL не задан."""
    url = build_part1_docs_employee_page_absolute_url(db, session_id)
    if not url:
        return None
    return "Чек-лист по служебным документам (откройте в браузере):\n" + url


def _row_for_public_token(db: Session, token: str) -> AssessmentSessionRow:
    row = get_session_row_by_part1_docs_token(db, token)
    if row is None:
        raise HTTPException(status_code=404, detail="part1_docs_token_invalid")
    if row.status == AssessmentSessionStatus.CANCELLED.value:
        raise HTTPException(status_code=404, detail="session_cancelled")
    if row.status == AssessmentSessionStatus.DRAFT.value:
        raise HTTPException(status_code=400, detail="session_not_started")
    return row


def get_part1_docs_checklist_by_token(db: Session, token: str) -> Part1DocsChecklistOut:
    row = _row_for_public_token(db, token)
    return get_part1_docs_checklist(db, row.id)


def save_part1_docs_checklist_by_token(db: Session, token: str, body: Part1DocsChecklistSave) -> Part1DocsChecklistOut:
    row = _row_for_public_token(db, token)
    payload = _parse_payload(getattr(row, "part1_docs_checklist_json", None))
    if payload.get("completed"):
        return get_part1_docs_checklist(db, row.id)
    return save_part1_docs_checklist(db, row.id, body)


def is_docs_checklist_completed(row: AssessmentSessionRow) -> bool:
    """Флаг для списка сессий / roster: опрос по документам завершён."""
    payload = _parse_payload(getattr(row, "part1_docs_checklist_json", None))
    return bool(payload.get("completed"))


def _part1_question_dicts_for_session(db: Session, row: AssessmentSessionRow) -> list[dict[str, str]]:
    """
    Пять вопросов: три по регламентам (при наличии ссылки на папку инструкций — с опорой на неё),
    один по KPI и один по ключевому навыку — из глобальных матриц каталога по ``position_code`` сотрудника (ядро HR).
    """
    cid = (row.client_id or "").strip() or "demo_client"
    eid = (row.employee_id or "").strip() or ""
    return build_part1_docs_question_dicts(db, cid, eid)


def _question_outs_for_session(db: Session, row: AssessmentSessionRow) -> list[Part1DocsQuestionOut]:
    return [
        Part1DocsQuestionOut(id=q["id"], text=q["text"], hint=q.get("hint", ""))
        for q in _part1_question_dicts_for_session(db, row)
    ]


def _required_ids_for_session(db: Session, row: AssessmentSessionRow) -> frozenset[str]:
    return frozenset(q["id"] for q in _part1_question_dicts_for_session(db, row))


def _parse_payload(raw: str | None) -> dict:
    if not raw or not str(raw).strip():
        return {}
    try:
        d = json.loads(raw)
        return d if isinstance(d, dict) else {}
    except json.JSONDecodeError:
        return {}


def _dump_payload(data: dict) -> str:
    return json.dumps(data, ensure_ascii=False)


def get_part1_docs_checklist(db: Session, session_id: str) -> Part1DocsChecklistOut:
    row = db.get(AssessmentSessionRow, session_id)
    if row is None:
        raise HTTPException(status_code=404, detail="session_not_found")
    payload = _parse_payload(getattr(row, "part1_docs_checklist_json", None))
    answers = payload.get("answers") if isinstance(payload.get("answers"), dict) else {}
    answers = {str(k): str(v) for k, v in answers.items()}
    completed = bool(payload.get("completed"))
    completed_at = payload.get("completed_at")
    return Part1DocsChecklistOut(
        version=PART1_DOCS_VERSION,
        questions=_question_outs_for_session(db, row),
        answers=answers,
        completed=completed,
        completed_at=completed_at,
        phase=SessionPhase(getattr(row, "phase", None) or SessionPhase.DRAFT.value),
    )


def save_part1_docs_checklist(db: Session, session_id: str, body: Part1DocsChecklistSave) -> Part1DocsChecklistOut:
    row = db.get(AssessmentSessionRow, session_id)
    if row is None:
        raise HTTPException(status_code=404, detail="session_not_found")
    if row.status == AssessmentSessionStatus.CANCELLED.value:
        raise HTTPException(status_code=400, detail="session_cancelled")

    payload = _parse_payload(getattr(row, "part1_docs_checklist_json", None))
    was_completed = bool(payload.get("completed"))
    cur_answers: dict[str, str] = {}
    if isinstance(payload.get("answers"), dict):
        cur_answers = {str(k): str(v) for k, v in payload["answers"].items()}

    incoming = {str(k): str(v).strip().lower() for k, v in body.answers.items()}
    for k, v in incoming.items():
        if v not in ALLOWED_VALUES:
            raise HTTPException(status_code=400, detail=f"part1_docs_invalid_answer:{k}")

    cur_answers.update(incoming)
    payload["answers"] = cur_answers
    payload["version"] = PART1_DOCS_VERSION

    if body.complete:
        req = _required_ids_for_session(db, row)
        missing = req - frozenset(cur_answers.keys())
        if missing:
            raise HTTPException(status_code=400, detail="part1_docs_incomplete:" + ",".join(sorted(missing)))
        for rid in req:
            if cur_answers.get(rid) not in ALLOWED_VALUES:
                raise HTTPException(status_code=400, detail=f"part1_docs_invalid_answer:{rid}")
        payload["completed"] = True
        payload["completed_at"] = datetime.now(timezone.utc).isoformat()
        ph = getattr(row, "phase", None) or SessionPhase.DRAFT.value
        if ph == SessionPhase.PART1.value:
            row.phase = SessionPhase.PART2.value

    row.part1_docs_checklist_json = _dump_payload(payload)
    db.commit()
    db.refresh(row)
    if body.complete and not was_completed and bool(payload.get("completed")):
        try:
            from skill_assessment.services import part2_case as part2_case_svc

            part2_case_svc.send_part2_case_ready_notice(db, session_id)
        except Exception:
            _log.exception("part1_docs_checklist: failed to send part2 case notice for session %s", session_id)
    return get_part1_docs_checklist(db, session_id)
