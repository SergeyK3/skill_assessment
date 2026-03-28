# route: (telegram + examination) | file: skill_assessment/services/telegram_examination.py
"""
Обработка сообщений Telegram в сценарии экзамена (после привязки chat_id к сотруднику).
"""

from __future__ import annotations

import os
import re
from urllib.parse import quote

from fastapi import HTTPException
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from skill_assessment.schemas.examination_api import ExaminationProtocolOut
from skill_assessment.services.telegram_docs_survey import _telegram_chat_ids_equal
from skill_assessment.services.docs_survey_time import utc_naive_to_local_display

from skill_assessment.domain.examination_entities import ExaminationPhase
from skill_assessment.infrastructure.db_models import AssessmentSessionRow
from skill_assessment.schemas.examination_api import (
    ExaminationAnswerBody,
    ExaminationConsentBody,
    ExaminationIntroDoneBody,
)
from skill_assessment.services import examination_service as ex

CONSENT_PROMPT = (
    "Согласие на обработку персональных данных и результатов проверки (экзамен по регламентам).\n"
    "Продолжая, вы подтверждаете ознакомление с политикой организации.\n\n"
    "Ответьте одним сообщением (текстом или голосом):\n"
    "• «да» или «согласен» — принять\n"
    "• «нет» или «отказ» — отказаться (потребуется помощь HR)"
)

# Когда в Part1 уже зафиксировано согласие на ПДн по опросу — не повторяем тот же блок; отдельно подтверждаем участие в этапе «регламенты».
CONSENT_PROMPT_AFTER_PART1_PD = (
    "Проверка по внутренним регламентам.\n\n"
    "Напишите или скажите голосом «да», чтобы начать вопросы, или «нет» (потребуется помощь HR)."
)

INTRO_PROMPT = (
    "Экзамен по внутренним регламентам и должностным инструкциям.\n"
    "Ориентировочное время — до 30 минут, вопросы по очереди.\n\n"
    "Когда будете готовы, напишите «готов» / «да» или отправьте короткое голосовое с тем же смыслом."
)


def _part1_pd_consent_accepted(db: Session, client_id: str, employee_id: str) -> bool:
    """Есть ли у сотрудника сессия оценки с уже принятым согласием ПДн (опрос по документам)."""
    if not employee_id:
        return False
    sid = db.scalar(
        select(AssessmentSessionRow.id)
        .where(
            AssessmentSessionRow.client_id == client_id,
            func.lower(AssessmentSessionRow.employee_id) == func.lower(employee_id),
            AssessmentSessionRow.docs_survey_pd_consent_status == "accepted",
        )
        .limit(1)
    )
    return sid is not None


def _resolve_from_docs_survey_chat(db: Session, chat_id: str) -> tuple[str, str] | None:
    """
    Без таблицы привязок: тот же chat_id, что уже сохранён в сессии оценки при уведомлениях Part1
    (``docs_survey_notify_chat_id``) — сотрудник и организация уже известны.
    """
    tid = str(chat_id).strip()
    rows = db.scalars(
        select(AssessmentSessionRow)
        .where(AssessmentSessionRow.docs_survey_notify_chat_id.isnot(None))
        .where(AssessmentSessionRow.employee_id.isnot(None))
        .order_by(AssessmentSessionRow.updated_at.desc())
        .limit(400)
    ).all()
    for r in rows:
        stored = (r.docs_survey_notify_chat_id or "").strip()
        if stored and _telegram_chat_ids_equal(stored, tid):
            cid = (r.client_id or "").strip()
            eid = (r.employee_id or "").strip()
            if cid and eid:
                return cid, eid
    return None


def _resolve_binding(db: Session, chat_id: str) -> tuple[str, str] | None:
    row = ex.get_telegram_binding(db, chat_id)
    if row is not None:
        return row.client_id, row.employee_id
    dev_c = os.getenv("TELEGRAM_DEV_CLIENT_ID", "").strip()
    dev_e = os.getenv("TELEGRAM_DEV_EMPLOYEE_ID", "").strip()
    if dev_c and dev_e:
        return dev_c, dev_e
    return _resolve_from_docs_survey_chat(db, chat_id)


def _is_yes(text: str) -> bool | None:
    """True = да, False = нет, None = непонятно."""
    t = text.strip().lower()
    if not t:
        return None
    if t in ("да", "yes", "ok", "ага", "+", "согласен", "согласна", "принимаю", "готов", "готова"):
        return True
    if t in ("нет", "no", "отказ", "отказываюсь", "-", "не согласен", "не согласна"):
        return False
    if re.match(r"^да[\s!.]*$", t) or t.startswith("да "):
        return True
    if re.match(r"^нет[\s!.]*$", t) or t.startswith("нет "):
        return False
    return None


def _is_ready(text: str) -> bool:
    t = text.strip().lower()
    return t in ("готов", "готова", "да", "yes", "ok", "начать", "поехали", "давай")


def _is_done(text: str) -> bool:
    """Завершение просмотра протокола: «готово» и разговорные «готов»/«готова» (как в инструкции к вопросам)."""
    t = text.strip().lower()
    if t in (
        "готово",
        "готов",
        "готова",
        "завершить",
        "ок",
        "ok",
        "да",
        "спасибо",
    ):
        return True
    if re.match(r"^готов[оа]?[\s!.]*$", t):
        return True
    return False


def _fmt_protocol_dt(v) -> str:
    s = utc_naive_to_local_display(v)
    return s if s else "—"


def _format_protocol_for_telegram(proto: ExaminationProtocolOut) -> list[str]:
    lines: list[str] = [
        "Протокол опроса по внутренним регламентам",
        "",
        f"Фамилия: {proto.employee_last_name}",
        f"Имя: {proto.employee_first_name}",
        f"Отчество: {proto.employee_middle_name}",
        f"Должность: {proto.employee_position_label or '—'}",
        f"Подразделение: {proto.employee_department_label or '—'}",
        f"Организация (client_id): {proto.client_id}",
        f"Дата прохождения экзамена: {_fmt_protocol_dt(proto.completed_at)}",
        f"Дата оценки: {_fmt_protocol_dt(proto.evaluated_at)}",
    ]
    if proto.average_score_4 is not None and proto.average_score_percent is not None:
        lines.extend(
            [
                "",
                "Итоговая (интегральная) оценка по экзамену",
                f"{proto.average_score_4:.2f} из 4 баллов · {proto.average_score_percent:.1f}%",
            ]
        )
    messages = ["\n".join(lines)]
    for it in proto.items:
        messages.append(
            "\n".join(
                [
                    f"Вопрос {it.seq + 1}",
                    it.question_text,
                    "",
                    f"Оценка по ответу: {it.score_4} балла (шкала 1–4) · {it.score_percent:.1f}% (шкала 50–100%)",
                    "Ответ сотрудника (транскрипт / текст)",
                    it.transcript_text or "—",
                ]
            )
        )
    messages.append(
        "Чтобы завершить экзамен, напишите или скажите голосом: «готово», «готов» или «да»."
    )
    return messages


def handle_telegram_message(
    db: Session,
    telegram_chat_id: str,
    text: str | None,
    is_start_command: bool,
) -> list[str]:
    """
    Возвращает список сообщений для отправки пользователю (по одному или несколько абзацев).
    """
    tid = str(telegram_chat_id).strip()
    pair = _resolve_binding(db, tid)
    if pair is None:
        return [
            "Не удалось определить сотрудника по этому чату.\n\n"
            "Обычно отдельная привязка не нужна: пишите из того же Telegram, куда бот присылал напоминания "
            "о слоте опроса по документам — система узнаёт чат автоматически.\n\n"
            "Если вы пишете из другого аккаунта или чата — один раз попросите HR привязать этот Telegram к вашему "
            "профилю (POST /api/skill-assessment/examination/telegram/bindings). "
            "Для локальной разработки без HR: в .env задайте TELEGRAM_DEV_CLIENT_ID и TELEGRAM_DEV_EMPLOYEE_ID."
        ]

    client_id, employee_id = pair
    try:
        row = ex.get_or_create_active_examination_session(db, client_id, employee_id)
    except HTTPException as e:
        return [f"Не удалось открыть сессию экзамена: {e.detail}"]

    phase = ExaminationPhase(row.phase)
    msg = (text or "").strip()

    try:
        if phase == ExaminationPhase.BLOCKED_NO_REGULATION:
            return [
                "Экзамен приостановлен: для вашей должности и подразделения не найден регламент с KPI в системе. "
                "Отдел кадров получил уведомление. После загрузки регламента кадры снимут блок — затем напишите сюда снова."
            ]
        if phase == ExaminationPhase.INTERRUPTED_TIMEOUT:
            return [
                "Экзамен прерван по таймауту: между ответами прошло более 5 минут. "
                "Отдел кадров получил уведомление. Для повторной проверки потребуется новое назначение."
            ]

        if phase == ExaminationPhase.CONSENT:
            if not msg or is_start_command:
                if _part1_pd_consent_accepted(db, client_id, employee_id):
                    return [CONSENT_PROMPT_AFTER_PART1_PD]
                return [CONSENT_PROMPT]
            yn = _is_yes(msg)
            if yn is None:
                if _part1_pd_consent_accepted(db, client_id, employee_id):
                    return ["Не понял ответ. Напишите «да» или «нет»."]
                return ["Не понял ответ. Напишите «да» (согласен) или «нет» (отказ)."]
            out = ex.post_consent(db, row.id, ExaminationConsentBody(accepted=yn))
            if ExaminationPhase(out.phase) == ExaminationPhase.BLOCKED_CONSENT:
                return ["Отказ зафиксирован. Обратитесь в отдел кадров для повторного предложения согласия."]
            # Сразу первый вопрос — без отдельного шага «напишите готов».
            if yn and ExaminationPhase(out.phase) == ExaminationPhase.INTRO:
                ex.post_intro_done(db, row.id, ExaminationIntroDoneBody())
                row = ex.get_examination_session(db, row.id)
                q = ex.get_current_question(db, row.id)
                if q:
                    return [
                        f"Вопрос {q.seq + 1}:\n\n{q.text}\n\n"
                        "Ответ — одним сообщением: текстом или голосом (будет записан в протокол как транскрипт)."
                    ]
                return ["Нет вопросов в сценарии (ошибка конфигурации)."]
            return [INTRO_PROMPT]

        if phase == ExaminationPhase.BLOCKED_CONSENT:
            return [
                "Согласие заблокировано до действий HR. После снятия блока напишите в этот чат — бот предложит шаги снова."
            ]

        if phase == ExaminationPhase.INTRO:
            if not msg or (is_start_command and not _is_ready(msg)):
                return [INTRO_PROMPT]
            if not _is_ready(msg):
                return ["Когда будете готовы начать вопросы, напишите «готов» или отправьте короткое голосовое с тем же смыслом."]
            ex.post_intro_done(db, row.id, ExaminationIntroDoneBody())
            row = ex.get_examination_session(db, row.id)
            q = ex.get_current_question(db, row.id)
            if q:
                return [
                    f"Вопрос {q.seq + 1}:\n\n{q.text}\n\n"
                    "Ответ — одним сообщением: текстом или голосом (будет записан в протокол как транскрипт)."
                ]
            return ["Нет вопросов в сценарии (ошибка конфигурации)."]

        if phase == ExaminationPhase.QUESTIONS:
            qcur = ex.get_current_question(db, row.id)
            if not qcur:
                return ["Внутренняя ошибка: нет текущего вопроса."]
            if not msg:
                return [
                    f"Вопрос {qcur.seq + 1}:\n\n{qcur.text}\n\n"
                    "Ответ — текстом или голосом (одним сообщением)."
                ]
            ex.post_answer(db, row.id, ExaminationAnswerBody(transcript_text=msg))
            row = ex.get_examination_session(db, row.id)
            if ExaminationPhase(row.phase) == ExaminationPhase.PROTOCOL:
                proto = ex.build_protocol(db, row.id)
                return _format_protocol_for_telegram(proto)

            qnext = ex.get_current_question(db, row.id)
            if qnext:
                return [
                    "Ответ записан.\n\n"
                    f"Вопрос {qnext.seq + 1}:\n\n{qnext.text}\n\n"
                    "Ответ — текстом или голосом (одним сообщением)."
                ]
            return ["Ответ записан."]

        if phase == ExaminationPhase.PROTOCOL:
            if _is_done(msg):
                cid = (row.client_id or "").strip()
                eid = (row.employee_id or "").strip()
                base = (os.getenv("SKILL_ASSESSMENT_PUBLIC_BASE_URL") or "http://127.0.0.1:8000").rstrip("/")
                q = "client_id=" + quote(cid, safe="")
                if eid:
                    q += "&employee_id=" + quote(eid, safe="")
                hr_page = f"{base}/api/skill-assessment/ui/exam-protocols?{q}"
                ex.complete_examination_session(db, row.id)
                # Два сообщения: (1) чтобы первая часть не «терялась» в длинном пузыре; (2) отдельное с кликабельной ссылкой.
                return [
                    "Экзамен завершён. Протокол формируется и будет готов примерно через 5 минут.\n\n"
                    "После готовности мы отправим сводку в этот чат, вашему руководителю и в служебный канал "
                    "(если они настроены на сервере).",
                    "Кадры: полный протокол в браузере — раздел «Оценка навыков», страница «Протоколы экзаменов» "
                    "(тот же client_id, что у организации).\n"
                    f"Ссылка для кадров:\n{hr_page}",
                ]
            if not msg:
                return [
                    "Когда закончите просмотр протокола, напишите или скажите голосом «готово», «готов» или «да» — экзамен будет завершён."
                ]
            return [
                "Не понял. Чтобы завершить экзамен после просмотра протокола, напишите или скажите голосом «готово», «готов» или «да»."
            ]

        if phase == ExaminationPhase.COMPLETED:
            return ["Этот экзамен уже завершён. При новом назначении HR откроется новая сессия."]

    except HTTPException as e:
        if e.detail == "examination_interrupted_timeout":
            return [
                "Экзамен прерван по таймауту: между ответами прошло более 5 минут. "
                "Отдел кадров получил уведомление. Для повторной проверки потребуется новое назначение."
            ]
        return [f"Ошибка: {e.detail}"]

    return ["Неизвестное состояние сценария. Напишите в этот чат или обратитесь в поддержку."]
