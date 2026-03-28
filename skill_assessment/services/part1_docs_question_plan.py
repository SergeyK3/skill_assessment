# route: (Part1 docs) | file: skill_assessment/services/part1_docs_question_plan.py
"""
Пять вопросов чек-листа Part 1: 3 по регламентам (при наличии — с опорой на папку инструкций),
1 по KPI и 1 по ключевому навыку из глобальных матриц каталога (как в сайдбаре: KPI и ключевые навыки).
"""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from skill_assessment.infrastructure.db_models import (
    CompetencyCatalogVersionRow,
    CompetencyMatrixRow,
    KpiCatalogVersionRow,
    KpiMatrixRow,
)
from skill_assessment.integration.hr_core import (
    get_employee,
    get_examination_instructions_folder_url,
    get_examination_question_texts,
)
from skill_assessment.services.examination_question_plan import tail_from_regulation_base

_HINT = (
    "Самооценка: «да» — в полной мере, «частично» — частично, «нет» — нет. "
    "Формулировки привязаны к регламенту должности, KPI и ключевым навыкам из каталога (при наличии данных)."
)


def _active_global_kpi_version_id(db: Session) -> str | None:
    return db.scalar(
        select(KpiCatalogVersionRow.id)
        .where(
            KpiCatalogVersionRow.client_id.is_(None),
            KpiCatalogVersionRow.status == "active",
        )
        .order_by(KpiCatalogVersionRow.created_at.desc())
        .limit(1)
    )


def _active_global_competency_version_id(db: Session) -> str | None:
    return db.scalar(
        select(CompetencyCatalogVersionRow.id)
        .where(
            CompetencyCatalogVersionRow.client_id.is_(None),
            CompetencyCatalogVersionRow.status == "active",
        )
        .order_by(CompetencyCatalogVersionRow.created_at.desc())
        .limit(1)
    )


def _first_kpi_for_position(
    db: Session, version_id: str, position_code: str
) -> tuple[str, str, str] | None:
    row = db.scalar(
        select(KpiMatrixRow)
        .where(
            KpiMatrixRow.version_id == version_id,
            KpiMatrixRow.position_code == position_code,
            KpiMatrixRow.is_active.is_(True),
        )
        .order_by(KpiMatrixRow.kpi_rank, KpiMatrixRow.department_code)
        .limit(1)
    )
    if row is None or row.kpi_definition is None:
        return None
    kd = row.kpi_definition
    title = (kd.title_ru or "").strip() or kd.kpi_code
    return (kd.kpi_code, title, kd.unit or "")


def _first_key_skill_for_position(
    db: Session, version_id: str, position_code: str
) -> tuple[str, str] | None:
    row = db.scalar(
        select(CompetencyMatrixRow)
        .where(
            CompetencyMatrixRow.version_id == version_id,
            CompetencyMatrixRow.position_code == position_code,
            CompetencyMatrixRow.is_active.is_(True),
        )
        .order_by(CompetencyMatrixRow.skill_rank, CompetencyMatrixRow.department_code)
        .limit(1)
    )
    if row is None or row.skill_definition is None:
        return None
    sd = row.skill_definition
    title = (sd.title_ru or "").strip() or sd.skill_code
    return (sd.skill_code, title)


def _three_regulation_texts(
    db: Session, client_id: str, employee_id: str
) -> tuple[str, str, str]:
    folder = (get_examination_instructions_folder_url(db, client_id, employee_id) or "").strip()
    base = get_examination_question_texts(db, client_id, employee_id)
    if base is None:
        base = []
    a, b = tail_from_regulation_base(base)
    if folder:
        r1 = (
            f"В папке должностных инструкций ({folder}) перечислены связанные документы. "
            "Какие из них напрямую касаются вашей должности и как вы сопоставляете их с регламентом подразделения?"
        )
        r2 = (
            "Какой документ из этой папки вы чаще всего используете в работе и почему он важен для выполнения ваших обязанностей?"
        )
        r3 = b
    else:
        r1 = (
            "Назовите ключевые положения внутреннего регламента по вашей должности и связанные документы, "
            "которые вы обязаны знать."
        )
        r2 = a
        r3 = b
    return (r1, r2, r3)


def _fallback_kpi_question() -> str:
    return (
        "Назовите ключевые KPI вашей должности и расскажите, как вы их отслеживаете и какие целевые значения для вас актуальны. "
        "(Если в каталоге KPI для должности заданы показатели — ориентируйтесь на них.)"
    )


def _fallback_skill_question() -> str:
    return (
        "Какие ключевые профессиональные навыки критичны для вашей текущей должности и как вы их поддерживаете в актуальном состоянии? "
        "(Если в матрице ключевых навыков для должности заданы позиции — ориентируйтесь на них.)"
    )


def build_part1_docs_question_dicts(db: Session, client_id: str, employee_id: str) -> list[dict[str, str]]:
    """
    Ровно пять вопросов: q_reg_1…3, q_kpi, q_key_skill.
    KPI и ключевой навык подставляются из глобальных матриц по ``position_code`` сотрудника (из ядра HR), если известен.
    """
    r1, r2, r3 = _three_regulation_texts(db, client_id, employee_id)

    emp = get_employee(db, client_id, employee_id)
    pos_code = (getattr(emp, "position_code", None) or "").strip() if emp else ""

    kpi_text = _fallback_kpi_question()
    skill_text = _fallback_skill_question()

    if pos_code:
        kv = _active_global_kpi_version_id(db)
        if kv:
            picked = _first_kpi_for_position(db, kv, pos_code)
            if picked:
                code, title, unit = picked
                unit_s = f", единица: {unit}" if unit else ""
                kpi_text = (
                    f"По показателю KPI из матрицы для вашей должности: «{title}» (код {code}{unit_s}). "
                    "Оцените своё понимание целевых значений и способа отслеживания: да / частично / нет."
                )
        cv = _active_global_competency_version_id(db)
        if cv:
            sk = _first_key_skill_for_position(db, cv, pos_code)
            if sk:
                scode, stitle = sk
                skill_text = (
                    f"По ключевому навыку из матрицы компетенций для вашей должности: «{stitle}» (код {scode}). "
                    "Оцените, насколько вы опираетесь на регламенты и практику в этой области: да / частично / нет."
                )

    return [
        {"id": "q_reg_1", "text": r1, "hint": _HINT},
        {"id": "q_reg_2", "text": r2, "hint": _HINT},
        {"id": "q_reg_3", "text": r3, "hint": _HINT},
        {"id": "q_kpi", "text": kpi_text, "hint": _HINT},
        {"id": "q_key_skill", "text": skill_text, "hint": _HINT},
    ]
