# route: (examination) | file: skill_assessment/services/examination_protocol_delivery.py
"""Отложенная отправка протокола экзамена в Telegram (канал, сотрудник, руководитель)."""

from __future__ import annotations

import logging
import os
import threading

from skill_assessment.adapters.telegram_outbound import get_telegram_outbound
from skill_assessment.infrastructure.db_models import ExaminationSessionRow
from skill_assessment.integration.hr_core import get_employee
from skill_assessment.schemas.examination_api import ExaminationProtocolOut
from skill_assessment.services import examination_service as ex
from skill_assessment.services.exam_protocol_recipients import resolve_manager_telegram_chat_for_protocol

_log = logging.getLogger(__name__)

PROTOCOL_DELAY_SEC = int(os.getenv("TELEGRAM_EXAM_PROTOCOL_DELAY_SEC", "300"))


def public_examination_protocol_url(session_id: str) -> str:
    base = (os.getenv("SKILL_ASSESSMENT_PUBLIC_BASE_URL") or "http://127.0.0.1:8000").rstrip("/")
    return f"{base}/api/skill-assessment/examination/sessions/{session_id}/protocol/html"


def _format_telegram_digest(proto: ExaminationProtocolOut, session_id: str, employee_label: str) -> str:
    report_href = proto.related_report_url or proto.related_report_path
    lines: list[str] = [
        "Протокол опроса по внутренним регламентам готов.",
        f"Сессия: {session_id[:8]}…",
        f"Сотрудник: {employee_label}",
    ]
    if report_href:
        lines.extend(
            [
                f"Общий протокол оценки: {report_href}",
                "В этом протоколе затем появятся кейсы (Part 2) и оценка руководителя (Part 3).",
            ]
        )
        if (proto.part2_summary or "").strip():
            lines.append(f"Этап 2: {proto.part2_summary}")
    lines.extend(
        [
            f"Протокол опроса по регламентам (HTML): {public_examination_protocol_url(session_id)}",
            "",
        ]
    )
    if proto.average_score_4 is not None and proto.average_score_percent is not None:
        lines.append(
            f"Итог: {proto.average_score_4:.2f} из 4 · {proto.average_score_percent:.1f}% (шкала % 50–100)."
        )
        lines.append("")
    lines.append("Кратко:")
    for it in proto.items[:12]:
        t = (it.transcript_text or "").strip().replace("\n", " ")
        if len(t) > 220:
            t = t[:217] + "…"
        lines.append(f"— В{it.seq + 1}: {t or '—'}")
    body = "\n".join(lines)
    if len(body) > 3800:
        body = body[:3700] + "\n\n… (полный текст по ссылке выше)"
    return body


def deliver_examination_protocol_telegram(session_id: str) -> None:
    """Синхронная отправка после задержки; вызывается из фонового потока с отдельной сессией БД."""
    from app.db import SessionLocal

    db = SessionLocal()
    try:
        proto = ex.build_protocol(db, session_id)
        row = db.get(ExaminationSessionRow, session_id)
        if row is None:
            _log.warning("examination_protocol_delivery: session gone %s", session_id[:8])
            return

        emp = get_employee(db, row.client_id, row.employee_id)
        employee_label = (
            emp.display_name if emp and emp.display_name else f"id:{row.employee_id}"
        )

        text = _format_telegram_digest(proto, session_id, employee_label)
        token = (os.getenv("TELEGRAM_BOT_TOKEN") or "").strip()
        if not token or len(token) < 10:
            _log.warning("examination_protocol_delivery: TELEGRAM_BOT_TOKEN not set")
            return

        outbound = get_telegram_outbound()
        chat_targets: list[tuple[str, str]] = []

        ch = (os.getenv("TELEGRAM_EXAM_PROTOCOL_CHANNEL_ID") or "").strip()
        if ch:
            chat_targets.append((ch, "channel"))

        bind = ex.get_telegram_binding_for_employee(db, row.client_id, row.employee_id)
        emp_chat = None
        if bind:
            emp_chat = str(bind.telegram_chat_id).strip()
        elif emp and emp.telegram_chat_id:
            emp_chat = str(emp.telegram_chat_id).strip()
        if emp_chat:
            chat_targets.append((emp_chat, "employee"))

        mgr_chat = resolve_manager_telegram_chat_for_protocol(db, row.client_id, row.employee_id)
        if mgr_chat:
            chat_targets.append((mgr_chat, "manager"))

        seen: set[str] = set()
        for cid, _role in chat_targets:
            if cid in seen:
                continue
            seen.add(cid)
            r = outbound.send_message(token=token, chat_id=cid, text=text, reply_markup=None)
            if not r.ok:
                _log.warning(
                    "examination_protocol_delivery: send failed chat=%s… %s",
                    cid[:12],
                    r.description,
                )
    except Exception:
        _log.exception("examination_protocol_delivery: failed for session %s", session_id[:8])
    finally:
        db.close()


def schedule_examination_protocol_delivery(session_id: str) -> None:
    """Через ~5 мин (настраивается) отправить протокол в Telegram."""

    def _run() -> None:
        try:
            deliver_examination_protocol_telegram(session_id)
        except Exception:
            _log.exception("examination_protocol_delivery: timer callback")

    delay = max(30, PROTOCOL_DELAY_SEC)
    t = threading.Timer(float(delay), _run)
    t.daemon = True
    t.start()
    _log.info(
        "examination_protocol_delivery: scheduled in %ss for session %s…",
        delay,
        session_id[:8],
    )
