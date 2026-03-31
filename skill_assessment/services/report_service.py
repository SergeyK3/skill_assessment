# route: (service) | file: skill_assessment/services/report_service.py
"""Отчёт по сессии: JSON для фронта и HTML в духе demo/future-scenario."""

from __future__ import annotations

import html
import os
import re
from datetime import datetime, timezone

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session

from skill_assessment.domain.entities import EvidenceKind, Part1TurnRole
from skill_assessment.domain.examination_entities import ExaminationSessionStatus
from skill_assessment.infrastructure.db_models import (
    AssessmentSessionRow,
    ExaminationSessionRow,
    SessionPart1TurnRow,
    SkillAssessmentResultRow,
    SkillDomainRow,
    SkillRow,
)
from skill_assessment.integration.hr_core import (
    EmployeeSnapshot,
    department_function_code_label_ru,
    employee_display_label,
    get_employee,
    get_examination_kpi_labels,
)
from skill_assessment.schemas.api import (
    PublicReportSessionOut,
    ReportEmployeeHeaderOut,
    ReportSkillRow,
    SessionReportHrOut,
    SessionReportPublicOut,
    SkillDevelopmentRecommendationOut,
)
from skill_assessment.services import part1_docs_checklist as part1_docs_svc
from skill_assessment.services.assessment_service import _evidence_from_json, _session_out
from skill_assessment.services import examination_service as examination_svc
from skill_assessment.services.part1_service import turn_row_to_out
from skill_assessment.services import part2_case as part2_case_svc


_LEVEL_TITLE_RU: dict[int, str] = {
    0: "Не демонстрирует",
    1: "Демонстрирует частично",
    2: "Демонстрирует правильно в типовой ситуации",
    3: "Демонстрирует правильно в сложной ситуации",
}


def _level_label(level: int) -> str:
    return _LEVEL_TITLE_RU.get(level, str(level))


def _level_from_pct(pct: int | None) -> int | None:
    if pct is None:
        return None
    value = max(0, min(100, int(pct)))
    if value >= 90:
        return 3
    if value >= 65:
        return 2
    if value >= 30:
        return 1
    return 0


def _latest_by_skill(rows: list[SkillAssessmentResultRow], evidence_kind: EvidenceKind | None = None) -> dict[str, SkillAssessmentResultRow]:
    by: dict[str, SkillAssessmentResultRow] = {}
    for r in sorted(rows, key=lambda x: (x.updated_at, x.created_at)):
        if evidence_kind is not None:
            notes = _evidence_from_json(r.evidence_json)
            if evidence_kind == EvidenceKind.MANAGER:
                if EvidenceKind.MANAGER not in notes:
                    continue
            elif not notes.get(evidence_kind):
                continue
        by[r.skill_id] = r
    return by


def _public_session_out(full_session) -> PublicReportSessionOut:
    return PublicReportSessionOut(
        id=full_session.id,
        client_id=full_session.client_id,
        employee_id=full_session.employee_id,
        status=full_session.status,
        phase=full_session.phase,
        started_at=full_session.started_at,
        completed_at=full_session.completed_at,
        created_at=full_session.created_at,
        updated_at=full_session.updated_at,
    )


def _part1_turn_score(part1_turns: list, checklist_answers: dict[str, str]) -> tuple[int | None, int | None, str]:
    answer_map = {"yes": 3.0, "partial": 1.0, "no": 0.0}
    values = [answer_map[a.strip().lower()] for a in checklist_answers.values() if a.strip().lower() in answer_map]
    user_turns = [t for t in part1_turns if t.role == Part1TurnRole.USER]
    turn_score: float | None = None
    if user_turns:
        avg_words = sum(len((t.text or "").split()) for t in user_turns) / max(1, len(user_turns))
        if avg_words >= 18 and len(user_turns) >= 2:
            turn_score = 3.0
        elif avg_words >= 8:
            turn_score = 2.0
        elif avg_words > 0:
            turn_score = 1.0
        else:
            turn_score = 0.0
    parts = [x for x in (sum(values) / len(values) if values else None, turn_score) if x is not None]
    if not parts:
        return None, None, "не проводилось (Part 1 — голос/STT позже)"
    level = max(0, min(3, int(round(sum(parts) / len(parts)))))
    pct = int(round((level / 3.0) * 100))
    yes_count = sum(1 for a in checklist_answers.values() if a == "yes")
    partial_count = sum(1 for a in checklist_answers.values() if a == "partial")
    no_count = sum(1 for a in checklist_answers.values() if a == "no")
    summary = (
        f"Устное интервью: {len(part1_turns)} реплик, "
        f"чек-лист yes/partial/no = {yes_count}/{partial_count}/{no_count}, "
        f"оценка {level}/3 ({pct}%)."
    )
    return level, pct, summary


def _recommendation_actions(skill_title: str) -> list[str]:
    title = (skill_title or "").lower()
    if "переговор" in title or "сделк" in title:
        return [
            "Раз в неделю разбирать с руководителем 2-3 сложных кейса переговоров и фиксировать лучшие формулировки.",
            "Перед звонком готовить границы уступок, BATNA и сценарий эскалации.",
            "После каждого отказа кратко фиксировать, на каком аргументе переговоры просели.",
        ]
    if "возраж" in title or "потребност" in title:
        return [
            "Использовать список уточняющих вопросов для выявления реальной потребности клиента.",
            "Отрабатывать типовые возражения в ролевых мини-тренировках 2 раза в неделю.",
            "Вести заметки по повторяющимся возражениям и готовить на них короткие ответы-скрипты.",
        ]
    if "crm" in title or "воронк" in title:
        return [
            "Обновлять CRM сразу после каждого контакта, не откладывая до конца дня.",
            "Раз в день проверять пустые поля, причины отказа и зависшие сделки.",
            "Попросить руководителя провести точечный аудит 10 карточек и сверить стандарты заполнения.",
        ]
    if "презентац" in title or "ценност" in title:
        return [
            "Собрать 3 коротких питча под разные типы клиентов и прогонять их вслух.",
            "В каждом звонке отдельно формулировать ценность продукта через боль клиента.",
            "Записывать успешные формулировки из разговоров сильных коллег и переносить их в свой шаблон.",
        ]
    if "план" in title or "прогноз" in title:
        return [
            "Раз в неделю сверять прогноз с фактической воронкой и отмечать причины расхождений.",
            "Декомпозировать месячный план на недельные контрольные точки.",
            "Фиксировать ранние сигналы риска по крупным сделкам и заранее готовить план действий.",
        ]
    return [
        "Выделить 2 рабочие ситуации в неделю, где навык нужно применять осознанно и с разбором результата.",
        "Разбирать с руководителем примеры сильного и слабого поведения по этому навыку.",
        "Фиксировать прогресс короткими заметками после реальных задач, а не только по итогам оценки.",
    ]


def _development_recommendations(rows: list[ReportSkillRow]) -> list[SkillDevelopmentRecommendationOut]:
    candidates = [
        r
        for r in rows
        if r.level <= 1
        or (r.part3_level is not None and r.part3_level <= 1)
        or (r.part2_level is not None and r.part2_level <= 1)
    ]
    if not candidates:
        candidates = sorted(rows, key=lambda x: (x.level, x.part3_level or 99, x.part2_level or 99))[:2]
    else:
        candidates = sorted(candidates, key=lambda x: (x.level, x.part3_level or 99, x.part2_level or 99))[:3]
    out: list[SkillDevelopmentRecommendationOut] = []
    for row in candidates:
        if row.part3_level is not None and row.part2_level is not None and row.part3_level < row.part2_level:
            reason = "Руководитель оценивает этот навык ниже, чем он проявился в кейсовом блоке."
        elif row.level <= 1:
            reason = "Итоговый уровень по навыку пока ниже уверенного рабочего стандарта."
        else:
            reason = "Навык пока выглядит нестабильным и требует закрепления в реальных задачах."
        out.append(
            SkillDevelopmentRecommendationOut(
                skill_code=row.skill_code,
                skill_title=row.skill_title,
                current_level=row.level,
                reason=reason,
                actions=_recommendation_actions(row.skill_title),
            )
        )
    return out


def _render_text_block(text: str) -> str:
    parts = [p.strip() for p in str(text or "").split("\n\n") if p.strip()]
    if not parts:
        return "—"
    return "".join(f"<p style=\"margin:0.35rem 0;\">{html.escape(part).replace(chr(10), '<br/>')}</p>" for part in parts)


# Коды навыков в скобках в тексте кейса (для отчёта показываем только названия).
_CASE_SKILL_CODE_PARENS_RE = re.compile(
    r"\s*\(\s*(?:код(?:ы)?\s*)?(?:C_[A-Za-z0-9_]+(?:\s*,\s*C_[A-Za-z0-9_]+)*)\s*\)",
    re.IGNORECASE,
)


def _strip_competence_codes_from_case_text(text: str) -> str:
    """Убирает из текста кейса пометки вида (C_…), (код C_…) — в отчёте остаются формулировки навыков без кодов."""
    if not text or not str(text).strip():
        return str(text or "")
    s = _CASE_SKILL_CODE_PARENS_RE.sub("", str(text))
    s = re.sub(r";(\s*;)+", ";", s)
    s = re.sub(r"\s{2,}", " ", s)
    s = re.sub(r"\s+([,.;:])", r"\1", s)
    s = re.sub(r"\s+\.", ".", s)
    return s.strip()


_CASE_TEXT_EXCLUDED_SECTION_TITLES: frozenset[str] = frozenset(
    {
        "Что должен сделать сотрудник",
        "Какие ограничения и документы нужно учесть",
        "Что будет считаться сильным ответом",
    }
)

# Хвост «Ситуации» с формулировкой про пробелы опроса — в отчёте не показываем (шаблон/старые кейсы).
_SITUATION_TAIL_ADDITIONAL_RE = re.compile(r"(?i)\s+Дополнительно нужно закрыть\b")
_SITUATION_LINE_STARTS_ADDITIONAL_RE = re.compile(r"(?i)^Дополнительно нужно закрыть\b")
# Заголовок инструкции оказался внутри «Ситуации» (модель без «:» или без перевода строки).
_INSTRUCTIONAL_HEADING_LINE_RE = re.compile(
    r"(?i)^\s*(Что\s+должен\s+сделать\s+сотрудник|Какие\s+ограничения\s+и\s+документы\s+нужно\s+учесть|"
    r"Что\s+будет\s+считаться\s+сильным\s+ответом)\s*:?\s*$"
)
# Типичное начало нумерованного блока из шаблона кейса.
_NUMBERED_INSTRUCTION_FIRST_RE = re.compile(r"(?i)^\s*1\.\s+Описать последовательность")
# Инструкционные секции втиснуты в ту же строку, что и конец «Ситуации».
_INLINE_INSTRUCTIONAL_BLOCK_RE = re.compile(
    r"(?i)\s+(Что\s+должен\s+сделать\s+сотрудник|Какие\s+ограничения\s+и\s+документы\s+нужно\s+учесть|"
    r"Что\s+будет\s+считаться\s+сильным\s+ответом)\s*:?\s*"
)

# (префикс строки → каноническое имя секции без двоеточия); более длинные префиксы раньше.
_CASE_SECTION_SPECS: list[tuple[str, str]] = sorted(
    [
        ("Заголовок:", "Заголовок"),
        ("Ситуация:", "Ситуация"),
        ("Что должен сделать сотрудник:", "Что должен сделать сотрудник"),
        ("Что должен сделать сотрудник", "Что должен сделать сотрудник"),
        ("Какие ограничения и документы нужно учесть:", "Какие ограничения и документы нужно учесть"),
        ("Какие ограничения и документы нужно учесть", "Какие ограничения и документы нужно учесть"),
        ("Что будет считаться сильным ответом:", "Что будет считаться сильным ответом"),
        ("Что будет считаться сильным ответом", "Что будет считаться сильным ответом"),
    ],
    key=lambda x: len(x[0]),
    reverse=True,
)


_MARKERS_INJECT_NEWLINE_BEFORE: tuple[str, ...] = tuple(
    sorted(
        (
            "Что должен сделать сотрудник:",
            "Какие ограничения и документы нужно учесть:",
            "Что будет считаться сильным ответом:",
            "Что должен сделать сотрудник",
            "Какие ограничения и документы нужно учесть",
            "Что будет считаться сильным ответом",
            "Ситуация:",
            "Заголовок:",
        ),
        key=len,
        reverse=True,
    )
)


def _inject_newlines_before_embedded_case_markers(raw: str) -> str:
    """Модели часто пишут «…Кейс 1/2 Ситуация:…» в одной строке — без переноса секции «Ситуация» не распознаётся."""
    s = raw
    for mk in _MARKERS_INJECT_NEWLINE_BEFORE:
        s = re.sub(rf"([^\n\r])({re.escape(mk)})", r"\1\n\2", s)
    return s


def _match_case_section_line(line: str) -> tuple[str, str] | None:
    """Если строка — начало секции кейса, вернуть (каноническое_название, хвост_после_заголовка)."""
    for prefix, canonical in _CASE_SECTION_SPECS:
        if line.startswith(prefix):
            tail = line[len(prefix) :].strip()
            return canonical, tail
    return None


def _trim_situation_instructional_tail(lines: list[str]) -> list[str]:
    """Убирает из «Ситуации» блок от «Дополнительно нужно закрыть…» и все следующие строки до конца секции."""
    out: list[str] = []
    drop_rest = False
    for line in lines:
        if drop_rest:
            continue
        st = line.strip()
        if st and _INSTRUCTIONAL_HEADING_LINE_RE.match(st):
            break
        if _NUMBERED_INSTRUCTION_FIRST_RE.match(line):
            break
        inl = _INLINE_INSTRUCTIONAL_BLOCK_RE.search(line)
        if inl:
            head = line[: inl.start()].rstrip()
            if head:
                out.append(head)
            drop_rest = True
            continue
        if st and _SITUATION_LINE_STARTS_ADDITIONAL_RE.match(st):
            drop_rest = True
            continue
        m = _SITUATION_TAIL_ADDITIONAL_RE.search(line)
        if m:
            head = line[: m.start()].rstrip()
            if head:
                out.append(head)
            drop_rest = True
            continue
        out.append(line)
    return out


def _case_sections_for_report(text: str) -> tuple[list[tuple[str, list[str]]], str]:
    """Разбор текста кейса: коды навыков убраны, служебные секции и хвост «Дополнительно» в «Ситуации» отброшены."""
    raw = _strip_competence_codes_from_case_text(str(text or "").replace("\r\n", "\n"))
    raw = _inject_newlines_before_embedded_case_markers(raw)
    sections: list[tuple[str, list[str]]] = []
    current_title = ""
    current_lines: list[str] = []
    for raw_line in raw.split("\n"):
        line = raw_line.strip()
        if not line:
            continue
        sec = _match_case_section_line(line)
        if sec is not None:
            if current_title or current_lines:
                sections.append((current_title, current_lines))
            current_title, tail = sec
            current_lines = [tail] if tail else []
            continue
        current_lines.append(line)
    if current_title or current_lines:
        sections.append((current_title, current_lines))
    sections = [(t, ls) for t, ls in sections if t not in _CASE_TEXT_EXCLUDED_SECTION_TITLES]
    sections = [
        (t, _trim_situation_instructional_tail(ls) if t == "Ситуация" else ls)
        for t, ls in sections
    ]
    sections = [(t, ls) for t, ls in sections if ls]
    return sections, raw


def _plain_text_from_case_sections(sections: list[tuple[str, list[str]]]) -> str:
    parts: list[str] = []
    for title, lines in sections:
        body = "\n".join(lines)
        parts.append(f"{title}:\n{body}" if title else body)
    return "\n\n".join(parts)


def _sanitize_case_body_text_for_report(text: str) -> str:
    """Текст кейса для JSON отчёта: без кодов в скобках, без инструкций и без хвоста «Дополнительно»."""
    sections, raw = _case_sections_for_report(text)
    if not sections:
        return raw
    return _plain_text_from_case_sections(sections)


def _org_unit_label(db: Session, client_id: str, org_unit_id: str | None) -> str | None:
    if not org_unit_id:
        return None
    try:
        from app.hr import get_org_unit  # type: ignore[import-not-found]

        ou = get_org_unit(db, client_id, org_unit_id)
        if ou is None:
            return None
        # Сначала человекочитаемые поля; ``code`` — только если нет названия (часто совпадает с department_code).
        for attr in ("name", "title", "label", "display_name"):
            v = getattr(ou, attr, None)
            if isinstance(v, str) and v.strip():
                return v.strip()
        v = getattr(ou, "code", None)
        if isinstance(v, str) and v.strip():
            return v.strip()
    except ImportError:
        return None
    except Exception:
        return None
    return None


def _report_department_label(org_unit_name: str | None, department_code: str | None) -> str | None:
    """Подпись «Подразделение» в отчёте: русское название для кодов вроде SALES, иначе строка из ядра."""
    code = (department_code or "").strip().upper() or None
    ru_from_code = department_function_code_label_ru(code) if code else None
    raw = (org_unit_name or "").strip() or None
    if raw:
        if code and raw.upper() == code and ru_from_code:
            return ru_from_code
        if raw.isupper() and " " not in raw and len(raw) <= 32:
            mapped = department_function_code_label_ru(raw)
            if mapped:
                return mapped
        return raw
    return ru_from_code


def _employee_fio_for_report(emp: EmployeeSnapshot | None) -> str | None:
    if emp is None:
        return None
    parts = [p.strip() for p in (emp.last_name, emp.first_name, emp.middle_name) if p and p.strip()]
    if parts:
        return " ".join(parts)
    if emp.display_name and emp.display_name.strip():
        return emp.display_name.strip()
    return None


def _part1_stt_note() -> str:
    raw = os.getenv("SKILL_ASSESSMENT_STT_PROVIDER", "").strip()
    if raw:
        return f"Провайдер STT (устные ответы Part 1): {raw}."
    return (
        "Провайдер STT не задан (переменная SKILL_ASSESSMENT_STT_PROVIDER пуста); "
        "текст ответов сотрудника в Part 1 может поступать напрямую с клиента или из Telegram без внешнего STT."
    )


def _related_completed_examination_part1_metrics(
    db: Session, row: AssessmentSessionRow
) -> tuple[float | None, int | None, str | None, list[dict[str, object]]]:
    """Возвращает итог связанного Telegram-опроса по регламентам для использования в Part 1 общего отчёта."""
    if not row.employee_id:
        return None, None, None, []
    created_at = getattr(row, "created_at", None)
    exam_rows = db.scalars(
        select(ExaminationSessionRow)
        .where(
            ExaminationSessionRow.client_id == row.client_id,
            ExaminationSessionRow.employee_id == row.employee_id,
            ExaminationSessionRow.status == ExaminationSessionStatus.COMPLETED.value,
        )
        .order_by(ExaminationSessionRow.created_at.desc())
        .limit(10)
    ).all()
    for exam_row in exam_rows:
        if created_at is not None and exam_row.created_at is not None and exam_row.created_at < created_at:
            continue
        try:
            proto = examination_svc.build_protocol(db, exam_row.id)
        except Exception:
            continue
        avg4 = getattr(proto, "average_score_4", None)
        avg_pct = getattr(proto, "average_score_percent", None)
        q_count = len(getattr(proto, "items", None) or [])
        items = [
            {
                "question_text": str(getattr(item, "question_text", "") or ""),
                "transcript_text": str(getattr(item, "transcript_text", "") or ""),
                "score_percent": getattr(item, "score_percent", None),
            }
            for item in (getattr(proto, "items", None) or [])
        ]
        if avg4 is None and avg_pct is None:
            continue
        summary = (
            f"Опрос по регламентам: {q_count} вопросов, итог {avg4:.2f}/4 ({avg_pct:.1f}%)."
            if avg4 is not None and avg_pct is not None
            else "Опрос по регламентам завершён."
        )
        pct_int = None if avg_pct is None else max(0, min(100, int(round(float(avg_pct)))))
        return avg4, pct_int, summary, items
    return None, None, None, []


def _level_to_pct(level: int | None) -> int | None:
    if level is None:
        return None
    return int(round((max(0, min(3, int(level))) / 3.0) * 100))


def _normalize_manager_evidence(text: str | None) -> str:
    if text is None:
        return "—"
    t = str(text).strip()
    if not t:
        return "—"
    for suffix in (" (Part 3)", "(Part 3)"):
        t = t.replace(suffix, "")
    t = t.strip()
    if t in ("Оценка руководителя", "Оценка руководителя."):
        return "—"
    return t or "—"


def _render_case_text(text: str) -> str:
    sections, raw = _case_sections_for_report(text)
    if not sections:
        return _render_text_block(raw)
    rendered: list[str] = []
    for title, lines in sections:
        rendered.append(f'<div style="margin:0.55rem 0 0.65rem 0;"><strong>{html.escape(title)}</strong>')
        ordered = all(line[:2].isdigit() or (len(line) > 1 and line[0].isdigit() and line[1] == ".") for line in lines if line)
        bullets = all(line.startswith("-") for line in lines if line)
        if ordered or bullets:
            tag = "ol" if ordered else "ul"
            items = "".join(f"<li>{html.escape(line.lstrip('-').strip())}</li>" for line in lines if line)
            rendered.append(f"<{tag} style=\"margin:0.35rem 0 0 1.2rem;\">{items}</{tag}>")
        else:
            rendered.append("".join(f"<p style=\"margin:0.35rem 0;\">{html.escape(line)}</p>" for line in lines if line))
        rendered.append("</div>")
    return "".join(rendered)


def _build_report_payload(db: Session, session_id: str) -> dict[str, object]:
    srow = db.get(AssessmentSessionRow, session_id)
    if srow is None:
        raise HTTPException(status_code=404, detail="session_not_found")

    res_rows = db.scalars(
        select(SkillAssessmentResultRow).where(SkillAssessmentResultRow.session_id == session_id)
    ).all()
    latest_overall = _latest_by_skill(list(res_rows))
    latest_case = _latest_by_skill(list(res_rows), EvidenceKind.CASE)
    latest_manager = _latest_by_skill(list(res_rows), EvidenceKind.MANAGER)

    report_rows: list[ReportSkillRow] = []
    skill_ids = sorted(set(latest_overall.keys()) | set(latest_case.keys()) | set(latest_manager.keys()))
    for skill_id in skill_ids:
        rr = latest_overall.get(skill_id) or latest_manager.get(skill_id) or latest_case.get(skill_id)
        if rr is None:
            continue
        sk = db.get(SkillRow, skill_id)
        if sk is None:
            continue
        dom = db.get(SkillDomainRow, sk.domain_id)
        domain_title = dom.title if dom else ""
        case_row = latest_case.get(skill_id)
        manager_row = latest_manager.get(skill_id)
        overall_notes = _evidence_from_json(rr.evidence_json)
        case_notes = _evidence_from_json(case_row.evidence_json) if case_row is not None else {}
        manager_notes = _evidence_from_json(manager_row.evidence_json) if manager_row is not None else {}
        lv = int(rr.level)
        report_rows.append(
            ReportSkillRow(
                skill_id=sk.id,
                skill_code=sk.code,
                skill_title=sk.title,
                domain_title=domain_title,
                level=lv,
                level_label_ru=_level_label(lv),
                part1_level=None,
                part2_level=int(case_row.level) if case_row is not None else None,
                part3_level=int(manager_row.level) if manager_row is not None else None,
                evidence_case=case_notes.get(EvidenceKind.CASE),
                evidence_manager=manager_notes.get(EvidenceKind.MANAGER),
                evidence_metric=overall_notes.get(EvidenceKind.METRIC),
            )
        )

    report_rows.sort(key=lambda x: (x.domain_title, x.skill_title))

    turn_rows = db.scalars(
        select(SessionPart1TurnRow)
        .where(SessionPart1TurnRow.session_id == session_id)
        .order_by(SessionPart1TurnRow.seq)
    ).all()
    part1_turns = [turn_row_to_out(r) for r in turn_rows]
    checklist = part1_docs_svc.get_part1_docs_checklist(db, session_id)
    part1_level, part1_pct, p1_summary = _part1_turn_score(part1_turns, checklist.answers)
    part1_exam_score_4, part1_exam_pct, part1_exam_summary, part1_exam_items = _related_completed_examination_part1_metrics(db, srow)
    if part1_exam_summary and (part1_level is None or part1_pct is None):
        part1_pct = part1_exam_pct
        part1_level = _level_from_pct(part1_exam_pct)
        p1_summary = part1_exam_summary

    emp_snap = get_employee(db, srow.client_id, srow.employee_id)
    emp_label = employee_display_label(emp_snap)
    org_name = _org_unit_label(db, srow.client_id, emp_snap.org_unit_id if emp_snap else None)
    dept_code = emp_snap.department_code if emp_snap else None
    dept_label = _report_department_label(org_name, dept_code)
    employee_header = ReportEmployeeHeaderOut(
        fio=_employee_fio_for_report(emp_snap),
        position_label=emp_snap.position_label if emp_snap else None,
        department_label=dept_label,
        position_code=emp_snap.position_code if emp_snap else None,
        department_code=emp_snap.department_code if emp_snap else None,
    )
    matrix_codes = sorted({r.skill_code for r in report_rows if r.skill_code})
    kpi_labels = get_examination_kpi_labels(db, srow.client_id, srow.employee_id or "") or []
    try:
        part2 = part2_case_svc.get_session_cases(db, session_id)
    except Exception:
        part2 = None

    for report_row in report_rows:
        report_row.part1_level = part1_level

    part2_overall_level = None
    if part2 is not None:
        part2_overall_level = (
            part2.ai_commission_consensus.overall_level_0_3
            if part2.ai_commission_consensus is not None and part2.ai_commission_consensus.overall_level_0_3 is not None
            else _level_from_pct(part2.overall_pct)
        )

    part2_cases_for_report = (
        [c.model_copy(update={"text": _sanitize_case_body_text_for_report(c.text or "")}) for c in part2.cases]
        if part2 is not None
        else []
    )

    return {
        "session": _session_out(db, srow),
        "generated_at": datetime.now(timezone.utc),
        "employee_label": emp_label,
        "employee_header": employee_header,
        "report_part1_stt_note": _part1_stt_note(),
        "report_matrix_skill_codes": matrix_codes,
        "report_examination_kpi_codes": list(kpi_labels),
        "part1_summary": p1_summary,
        "part1_exam_score_4": part1_exam_score_4,
        "part1_overall_level": part1_level,
        "part1_overall_pct": part1_pct,
        "part1_exam_items": part1_exam_items,
        "part1_turns": part1_turns,
        "part2_summary": part2_case_svc.get_part2_summary(srow),
        "part2_case_count": part2.case_count if part2 is not None else 0,
        "part2_allotted_minutes": part2.allotted_minutes if part2 is not None else 0,
        "part2_solved_cases": part2.solved_cases if part2 is not None else 0,
        "part2_overall_pct": part2.overall_pct if part2 is not None else 0,
        "part2_overall_level": part2_overall_level,
        "part2_completed_at": part2.completed_at if part2 is not None else None,
        "part2_ai_commission_consensus": part2.ai_commission_consensus if part2 is not None else None,
        "part2_cases": part2_cases_for_report,
        "development_recommendations": _development_recommendations(report_rows),
        "rows": report_rows,
        "part2_llm_costs": (part2.llm_costs if part2 is not None else None),
    }


def build_session_report(db: Session, session_id: str) -> SessionReportHrOut:
    return SessionReportHrOut(**_build_report_payload(db, session_id))


def build_public_session_report(db: Session, token: str) -> SessionReportPublicOut:
    row = part1_docs_svc.get_session_row_by_part1_docs_token(db, token)
    if row is None:
        raise HTTPException(status_code=404, detail="report_token_invalid")
    if row.status == "cancelled":
        raise HTTPException(status_code=404, detail="session_cancelled")
    payload = _build_report_payload(db, row.id)
    session = payload.pop("session")
    payload.pop("part2_llm_costs", None)
    return SessionReportPublicOut(session=_public_session_out(session), **payload)


def render_session_report_html(rep: SessionReportPublicOut | SessionReportHrOut) -> str:
    """HTML с теми же классами, что demo_future_scenario.html (layout.css)."""
    sess = rep.session
    title = "Отчёт по сессии оценки навыков"
    sub = (
        f"Сессия: <code>{html.escape(sess.id)}</code> · клиент: <strong>{html.escape(sess.client_id)}</strong> · "
        f"фаза: <strong>{html.escape(sess.phase.value)}</strong> · статус: <strong>{html.escape(sess.status.value)}</strong>"
    )

    hdr = getattr(rep, "employee_header", None)
    meta_rows: list[str] = []
    if hdr is not None:
        fio_cell = html.escape(hdr.fio) if hdr.fio else html.escape(rep.employee_label or "—")
        meta_rows.append(f'<tr><th scope="row">ФИО</th><td>{fio_cell}</td></tr>')
        meta_rows.append(
            f'<tr><th scope="row">Должность</th><td>{html.escape(hdr.position_label or "—")}</td></tr>'
        )
        meta_rows.append(
            f'<tr><th scope="row">Подразделение</th><td>{html.escape(hdr.department_label or "—")}</td></tr>'
        )
        meta_rows.append(
            f'<tr><th scope="row">Код должности</th><td><code>{html.escape(hdr.position_code or "—")}</code></td></tr>'
        )
        meta_rows.append(
            f'<tr><th scope="row">Код подразделения</th><td><code>{html.escape(hdr.department_code or "—")}</code></td></tr>'
        )
    stt_note = html.escape(getattr(rep, "report_part1_stt_note", "") or "")
    kpis = getattr(rep, "report_examination_kpi_codes", None) or []
    kpi_cell = ", ".join(html.escape(k) for k in kpis) if kpis else "—"
    meta_rows.append(f'<tr><th scope="row">STT</th><td>{stt_note or "—"}</td></tr>')
    meta_rows.append(f'<tr><th scope="row">KPI (регламент / экзамен)</th><td>{kpi_cell}</td></tr>')
    meta_table = (
        '<table class="report-employee" style="width:100%;font-size:0.88rem;margin:0.75rem 0 0 0;">'
        "<tbody>"
        + "".join(meta_rows)
        + "</tbody></table>"
    )

    blocks: list[str] = []
    part1_title = (
        "Часть 1 — опрос по регламентам"
        if not rep.part1_turns and getattr(rep, "part1_exam_score_4", None) is not None
        else "Часть 1 — устное интервью (LLM)"
    )
    if rep.part1_turns:
        dialog_parts: list[str] = []
        for t in rep.part1_turns:
            if t.role == Part1TurnRole.LLM:
                dialog_parts.append(
                    f'<div class="q"><strong>LLM:</strong> {html.escape(t.text)}</div>'
                )
            else:
                dialog_parts.append(
                    f'<div class="a"><strong>Ответ (после STT):</strong> {html.escape(t.text)}</div>'
                )
        dialog_html = '<div class="dialog">' + "".join(dialog_parts) + "</div>"
        blocks.append(
            f'<div class="block"><h2>{html.escape(part1_title)}</h2>'
            f'<p class="meta" style="margin-top:0;">{html.escape(rep.part1_summary)}</p>'
            f"{dialog_html}</div>"
        )
    else:
        exam_items_html = ""
        if getattr(rep, "part1_exam_items", None):
            rows = []
            for idx, item in enumerate(rep.part1_exam_items, start=1):
                qtxt = html.escape(item.question_text or "—")
                atxt = _render_text_block(item.transcript_text or "—")
                pct = "—" if item.score_percent is None else f"{float(item.score_percent):.1f}%"
                rows.append(
                    '<div style="border:1px solid var(--border); border-radius:8px; padding:0.85rem; margin-top:0.75rem;">'
                    + f'<p style="margin:0 0 0.4rem 0;"><strong>Вопрос {idx}:</strong> {qtxt}</p>'
                    + f'<p class="meta" style="margin:0 0 0.45rem 0;"><strong>Оценка ответа:</strong> {pct}</p>'
                    + f'<div class="ans"><strong>Ответ сотрудника:</strong>{atxt}</div>'
                    + '</div>'
                )
            exam_items_html = "".join(rows)
        blocks.append(
            f'<div class="block"><h2>{html.escape(part1_title)}</h2>'
            f'<p class="meta" style="margin-top:0;">{html.escape(rep.part1_summary)}</p>'
            + exam_items_html
            + '</div>'
        )

    part2_extra = []
    if rep.part2_case_count:
        part2_extra.append(
            f'<p class="meta" style="margin-top:0.4rem;">Кейсов: <strong>{rep.part2_case_count}</strong> · '
            f'время: <strong>{rep.part2_allotted_minutes} мин.</strong> · '
            f'результат ИИ: <strong>{rep.part2_solved_cases}/{rep.part2_case_count} = {rep.part2_overall_pct}%</strong>'
            + (
                f' · балл блока: <strong>{rep.part2_overall_level}/3</strong></p>'
                if rep.part2_overall_level is not None
                else "</p>"
            )
        )
    if rep.part2_cases:
        case_blocks = []
        for idx, c in enumerate(rep.part2_cases, start=1):
            result = "ещё не оценено"
            if c.passed is True:
                result = "решён"
            elif c.passed is False:
                result = "не решён"
            covered = ", ".join([s.skill_title for s in c.covered_skills if s.skill_title]) or c.skill_title
            score_html = (
                f' · Оценка: <strong>{c.case_level_0_3}/3 · {c.case_pct_0_100}%</strong>'
                if c.case_level_0_3 is not None or c.case_pct_0_100 is not None
                else ""
            )
            skills_html = (
                '<ul style="margin:0.6rem 0 0 1rem;">'
                + "".join(
                    "<li>"
                    + html.escape(skill.skill_title)
                    + ": "
                    + html.escape(str(skill.level_0_3 if skill.level_0_3 is not None else "—"))
                    + "/3, "
                    + html.escape(str(skill.pct_0_100 if skill.pct_0_100 is not None else "—"))
                    + "%</li>"
                    for skill in c.skill_evaluations
                )
                + "</ul>"
                if c.skill_evaluations
                else ""
            )
            case_blocks.append(
                '<div style="border:1px solid var(--border); border-radius:8px; padding:0.85rem; margin-top:0.85rem;">'
                + f'<h3 style="margin:0 0 0.4rem 0; font-size:0.95rem;">Кейс {idx}: {html.escape(covered)}</h3>'
                + f'<p class="meta" style="margin:0 0 0.5rem 0;">Источник: <code>{html.escape(c.source)}</code> · '
                + f'Результат: <strong>{html.escape(result)}</strong>'
                + score_html
                + "</p>"
                + f'<div class="ans" style="margin-bottom:0.6rem;"><strong>Текст кейса:</strong>{_render_case_text(c.text)}</div>'
                + f'<div class="ans" style="margin-bottom:0.6rem;"><strong>Решение сотрудника:</strong>{_render_text_block(c.answer or "—")}</div>'
                + f'<p class="meta" style="margin:0;">Комментарий ИИ: {html.escape(c.evaluation_note or "—")}</p>'
                + skills_html
                + '</div>'
            )
        part2_extra.append("".join(case_blocks))
    if rep.part2_ai_commission_consensus is not None:
        consensus = rep.part2_ai_commission_consensus
        part2_extra.append(
            '<div style="border:1px solid var(--border); border-radius:8px; padding:0.85rem; margin-top:0.85rem;">'
            '<h3 style="margin:0 0 0.4rem 0; font-size:0.95rem;">Итог AI-комиссии</h3>'
            + (
                f'<p class="meta" style="margin:0 0 0.45rem 0;">Уровень: <strong>{consensus.overall_level_0_3}/3</strong> · '
                f'процент: <strong>{consensus.overall_pct_0_100}%</strong></p>'
                if consensus.overall_level_0_3 is not None or consensus.overall_pct_0_100 is not None
                else ""
            )
            + f'<p class="meta" style="margin:0 0 0.45rem 0;"><strong>Обоснование оценки:</strong> {html.escape(consensus.summary or "—")}</p>'
            + (
                '<p class="meta" style="margin:0 0 0.45rem 0;"><strong>Сильные стороны:</strong> '
                + html.escape(", ".join(consensus.strengths))
                + "</p>"
                if consensus.strengths
                else ""
            )
            + (
                '<p class="meta" style="margin:0 0 0.45rem 0;"><strong>Риски:</strong> '
                + html.escape(", ".join(consensus.risks))
                + "</p>"
                if consensus.risks
                else ""
            )
            + (
                f'<p class="meta" style="margin:0;"><strong>Рекомендация:</strong> {html.escape(consensus.recommendation)}</p>'
                if consensus.recommendation
                else ""
            )
            + "</div>"
        )
    if hasattr(rep, "part2_llm_costs") and getattr(rep, "part2_llm_costs", None) is not None:
        costs = rep.part2_llm_costs
        rows_cost = []
        for item in costs.steps:
            rows_cost.append(
                "<tr>"
                f"<td>{html.escape(item.label)}</td>"
                f"<td>{html.escape(str(item.calls))}</td>"
                f"<td>{html.escape(str(item.input_tokens))}</td>"
                f"<td>{html.escape(str(item.output_tokens))}</td>"
                f"<td>{html.escape(str(item.total_tokens))}</td>"
                f"<td>{html.escape(f'{item.cost_usd:.6f}')}</td>"
                f"<td>{html.escape(f'{item.cost_rub:.4f}')}</td>"
                "</tr>"
            )
        part2_extra.append(
            '<div style="border:1px solid var(--border); border-radius:8px; padding:0.85rem; margin-top:0.85rem;">'
            '<h3 style="margin:0 0 0.4rem 0; font-size:0.95rem;">Стоимость LLM-вызовов</h3>'
            + (
                f'<p class="meta" style="margin:0 0 0.45rem 0;">'
                f'Входящие токены: <strong>{costs.total_input_tokens}</strong> · '
                f'Исходящие токены: <strong>{costs.total_output_tokens}</strong> · '
                f'Стоимость: <strong>{costs.total_cost_usd:.6f} USD / {costs.total_cost_rub:.4f} RUB</strong> · '
                f'Курс: <strong>{costs.usd_to_rub_rate:.4f}</strong>'
                f'</p>'
            )
            + '<table><thead><tr><th>Шаг</th><th>Вызовы</th><th>Input</th><th>Output</th><th>Всего токенов</th><th>USD</th><th>RUB</th></tr></thead><tbody>'
            + ("".join(rows_cost) or '<tr><td colspan="7">Нет usage-данных</td></tr>')
            + f'</tbody></table><p class="meta" style="margin:0.55rem 0 0 0;">Итого: <strong>{costs.total_input_tokens}</strong> input · <strong>{costs.total_output_tokens}</strong> output · <strong>{costs.total_cost_usd:.6f} USD / {costs.total_cost_rub:.4f} RUB</strong></p>'
            + "</div>"
        )
    blocks.append(
        f'<div class="block"><h2>Часть 2 — кейсы</h2>'
        f'<p class="meta" style="margin-top:0;">{html.escape(rep.part2_summary)}</p>'
        + "".join(part2_extra)
        + f'<p class="muted">Для JSON-версии блока кейсов используйте '
        f'<code>GET /api/skill-assessment/sessions/{{id}}/part2-cases</code>.</p></div>'
    )

    tbody_mgr = []
    for r in rep.rows:
        tbody_mgr.append(
            "<tr>"
            f"<td>{html.escape(r.skill_title)}</td>"
            f"<td><strong>{r.part3_level if r.part3_level is not None else '—'}</strong></td>"
            "</tr>"
        )

    blocks.append(
        '<div class="block"><h2>Часть 3 — оценка руководителем</h2>'
        '<p class="meta" style="margin-top:0;">Оценка по шкале 0–3 по каждому навыку. Развёрнутый комментарий руководителя приводится отдельно ниже.</p>'
        '<table><thead><tr><th>Навык</th><th>Оценка (0–3)</th></tr></thead><tbody>'
        + ("".join(tbody_mgr) or "<tr><td colspan=\"2\">Нет данных</td></tr>")
        + "</tbody></table></div>"
    )
    overall_comment = (getattr(sess, "manager_overall_comment", None) or "").strip()
    if overall_comment:
        blocks.append(
            '<div class="block"><h2>Общий комментарий руководителя</h2>'
            + f'<p style="white-space:pre-wrap; margin:0;">{html.escape(overall_comment)}</p>'
            + "</div>"
        )

    rec_items = []
    for item in rep.development_recommendations:
        actions_html = "".join(f"<li>{html.escape(action)}</li>" for action in item.actions)
        rec_items.append(
            '<div style="border:1px solid var(--border); border-radius:8px; padding:0.85rem; margin-top:0.85rem;">'
            + f'<h3 style="margin:0 0 0.35rem 0; font-size:0.95rem;">{html.escape(item.skill_title)}'
            + (
                f' <span class="badge">текущий уровень {item.current_level}/3</span>'
                if item.current_level is not None
                else ""
            )
            + "</h3>"
            + f'<p class="meta" style="margin:0 0 0.45rem 0;">{html.escape(item.reason or "Навык требует усиления.")}</p>'
            + f'<ul style="margin:0.35rem 0 0 1.2rem;">{actions_html}</ul>'
            + "</div>"
        )
    blocks.append(
        '<div class="block"><h2>Рекомендации по развитию</h2>'
        '<p class="meta" style="margin-top:0;">Какие навыки стоит усилить в первую очередь и за счёт каких практических действий.</p>'
        + ("".join(rec_items) or '<p class="meta">Критичных пробелов не выявлено.</p>')
        + "</div>"
    )

    tbody_sum = []
    tbody_pct = []
    for r in rep.rows:
        parts = [x for x in (r.part1_level, r.part2_level, r.part3_level) if x is not None]
        overall = f"{sum(parts) / len(parts):.1f}" if parts else "—"
        p1 = "—" if r.part1_level is None else str(r.part1_level)
        p2 = "—" if r.part2_level is None else str(r.part2_level)
        p3 = "—" if r.part3_level is None else str(r.part3_level)
        tbody_sum.append(
            "<tr>"
            f"<td>{html.escape(r.skill_title)}</td>"
            f"<td>{p1}</td><td>{p2}</td><td>{p3}</td>"
            f"<td><strong>{overall}</strong></td>"
            "</tr>"
        )
        pct_parts = [x for x in (rep.part1_overall_pct, _level_to_pct(r.part2_level), _level_to_pct(r.part3_level)) if x is not None]
        overall_pct = f"{sum(pct_parts) / len(pct_parts):.1f}%" if pct_parts else "—"
        p1_pct = "—" if rep.part1_overall_pct is None else f"{rep.part1_overall_pct}%"
        p2_pct = "—" if r.part2_level is None else f"{_level_to_pct(r.part2_level)}%"
        p3_pct = "—" if r.part3_level is None else f"{_level_to_pct(r.part3_level)}%"
        tbody_pct.append(
            "<tr>"
            f"<td>{html.escape(r.skill_title)}</td>"
            f"<td>{p1_pct}</td><td>{p2_pct}</td><td>{p3_pct}</td>"
            f"<td><strong>{overall_pct}</strong></td>"
            "</tr>"
        )

    p1_line = (
        f'<p class="meta" style="margin-top:0;">Итоговая оценка Part 1: <strong>{rep.part1_exam_score_4:.2f}/4</strong>'
        + (
            f' · <strong>{rep.part1_overall_level}/3</strong> после приведения к общей шкале'
            if rep.part1_overall_level is not None
            else ""
        )
        + f" · {rep.part1_overall_pct}%.</p>"
        if rep.part1_exam_score_4 is not None and rep.part1_overall_pct is not None
        else (
            f'<p class="meta" style="margin-top:0;">Итоговая оценка устного интервью (Part 1): <strong>{rep.part1_overall_level}/3</strong> · '
            f"{rep.part1_overall_pct}%.</p>"
            if rep.part1_overall_level is not None and rep.part1_overall_pct is not None
            else '<p class="meta" style="margin-top:0;">Итог Part 1 пока не рассчитан.</p>'
        )
    )
    pct_caption = (
        '<p class="muted">В процентной таблице Part 1 берётся из завершённого опроса по регламентам, '
        'Part 2 и Part 3 переводятся из шкалы 0–3 в проценты от максимума.</p>'
        if rep.part1_overall_pct is not None
        else '<p class="muted">Part 1 пока не рассчитан, поэтому в процентной таблице используются только доступные части.</p>'
    )
    if rep.part2_case_count and rep.part2_overall_level is not None:
        p2_line = (
            f'<p class="meta" style="margin-top:0.35rem;">Итоговая оценка решения кейсов (Part 2): <strong>{rep.part2_overall_level}/3</strong> · '
            f"{rep.part2_overall_pct}%.</p>"
        )
    elif rep.part2_case_count:
        p2_line = (
            f'<p class="meta" style="margin-top:0.35rem;">Итог решения кейсов (Part 2): <strong>{rep.part2_overall_pct}%</strong> '
            f"({rep.part2_solved_cases}/{rep.part2_case_count} кейсов).</p>"
        )
    else:
        p2_line = '<p class="meta" style="margin-top:0.35rem;">Part 2 (кейсы) в этой сессии не проводился.</p>'

    blocks.append(
        '<div class="block"><h2>Сводка (иллюстрация весов)</h2>'
        + p1_line
        + p2_line
        + '<table><thead><tr><th>Навык</th><th>Часть 1 (общая шкала 0–3)</th><th>Часть 2 (кейс)</th><th>Часть 3 (руководитель)</th>'
        '<th>Итог по навыку (среднее по заполненным частям 1–3)</th></tr></thead><tbody>'
        + ("".join(tbody_sum) or '<tr><td colspan="5">Нет результатов</td></tr>')
        + "</tbody></table>"
        + '<p class="muted">Баллы Part 1 для каждого навыка совпадают с итогом Part 1 выше и участвуют в среднем «Итог по навыку» вместе с Part 2 и Part 3, если они заполнены.</p></div>'
    )
    blocks.append(
        '<div class="block"><h2>Сводка по процентной шкале</h2>'
        + '<table><thead><tr><th>Навык</th><th>Часть 1 (%)</th><th>Часть 2 (%)</th><th>Часть 3 (%)</th>'
        '<th>Итог по навыку (%)</th></tr></thead><tbody>'
        + ("".join(tbody_pct) or '<tr><td colspan="5">Нет результатов</td></tr>')
        + "</tbody></table>"
        + pct_caption
        + "</div>"
    )

    body_inner = "\n".join(blocks)
    return f"""<!DOCTYPE html>
<html lang="ru">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>{html.escape(title)}</title>
  <link rel="stylesheet" href="/static/shared/layout.css">
  <style>
    .wrap {{ max-width: 720px; padding: 2rem; }}
    .badge {{ display: inline-block; font-size: 0.72rem; padding: 0.2rem 0.5rem; border-radius: 4px; background: rgba(124,58,237,0.2); color: var(--accent); margin-right: 0.35rem; }}
    .block {{ background: var(--surface); border: 1px solid var(--border); border-radius: 8px; padding: 1rem 1.25rem; margin-top: 1rem; }}
    .block h2 {{ font-size: 1rem; margin: 0 0 0.75rem 0; color: var(--accent); }}
    table {{ width: 100%; border-collapse: collapse; font-size: 0.85rem; margin-top: 0.5rem; }}
    th, td {{ border-bottom: 1px solid var(--border); padding: 0.45rem 0.5rem; text-align: left; }}
    th {{ color: var(--text-muted); font-weight: 500; }}
    .muted {{ font-size: 0.82rem; color: var(--text-muted); margin-top: 0.75rem; }}
    .dialog {{ font-size: 0.9rem; line-height: 1.55; }}
    .dialog .q {{ color: var(--text-muted); margin-top: 0.65rem; }}
    .dialog .a {{ margin: 0.25rem 0 0 1rem; border-left: 2px solid var(--accent); padding-left: 0.75rem; }}
  </style>
</head>
<body>
  <div class="app-layout">
    <aside class="sidebar">
      <div class="sidebar-top">
        <div class="sidebar-brand">Отчёт</div>
        <a href="/skill-assessment" class="sidebar-item">← Skill assessment</a>
        <a href="/docs" class="sidebar-item api-link">API</a>
      </div>
    </aside>
    <main class="main">
      <div class="content wrap">
        <p class="meta" style="margin:0;"><span class="badge">сессия</span> Данные из API skill-assessment</p>
        <h1 style="margin:0.5rem 0 0 0; font-size:1.35rem;">{html.escape(title)}</h1>
        <p class="meta">{sub}</p>
        {meta_table}
        <p class="muted">Сформировано: {html.escape(rep.generated_at.isoformat())}</p>
        {body_inner}
        <p class="meta"><a href="/skill-assessment">← Назад</a></p>
      </div>
    </main>
  </div>
</body>
</html>"""
