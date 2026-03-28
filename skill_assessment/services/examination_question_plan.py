# route: (examination) | file: skill_assessment/services/examination_question_plan.py
"""
План вопросов экзамена по ТЗ: цель должности, продукт, KPI; затем два вопроса по папке
должностных инструкций или по регламенту (на основе ответа ядра ``get_examination_question_texts``).
"""

from __future__ import annotations

import os

from sqlalchemy.orm import Session

from skill_assessment.integration.hr_core import (
    get_examination_instructions_folder_url,
    get_examination_question_texts,
)

_MANDATORY = (
    "Каково назначение или цель вашей должности в организации и подразделении? "
    "Сформулируйте своими словами.",
    "Какой ценный конечный продукт вы создаёте или обеспечиваете в своей работе? Приведите короткий пример.",
    "Назовите ключевые KPI вашей должности и расскажите, как вы их отслеживаете и какие целевые значения для вас актуальны.",
)


def examination_question_limit() -> int | None:
    """
    Необязательный потолок числа вопросов (экзамен и связанные экраны).
    Переменная: ``SKILL_ASSESSMENT_EXAM_QUESTION_LIMIT`` (1–32), пусто — без обрезки сверху.
    """
    raw = (os.getenv("SKILL_ASSESSMENT_EXAM_QUESTION_LIMIT") or "").strip()
    if not raw:
        return None
    try:
        n = int(raw)
    except ValueError:
        return None
    return max(1, min(32, n))


def clip_question_texts(texts: list[str]) -> list[str]:
    """Обрезка списка вопросов по лимиту окружения (или до 32 по умолчанию)."""
    lim = examination_question_limit()
    if lim is not None:
        return texts[:lim]
    return texts[:32]


def tail_from_regulation_base(base: list[str]) -> tuple[str, str]:
    """Два вопроса по регламенту (для Part 1 и экзамена)."""
    return _tail_from_regulation_base(base)


def _tail_from_regulation_base(base: list[str]) -> tuple[str, str]:
    """Два вопроса по регламенту из списка ядра или общие формулировки."""
    if len(base) >= 2:
        return (base[-2], base[-1])
    if len(base) == 1:
        return (
            base[0],
            "Где вы находите актуальные версии нормативных документов организации и как проверяете, что версия действующая?",
        )
    return (
        "Назовите ключевые положения внутреннего регламента по вашей должности и связанные документы, "
        "которые вы обязаны знать.",
        "Где вы находите актуальные версии нормативных документов организации и как проверяете, что версия действующая?",
    )


def compose_examination_question_plan(db: Session, client_id: str, employee_id: str) -> list[str] | None:
    """
    Семантика как у ``get_examination_question_texts``:

    - ``None`` — ядро не подключено / нет данных → сценарий ``regulation_v1`` из сида.
    - ``[]`` — регламент не найден → блокировка экзамена.
    - Иначе — не менее пяти вопросов (3 обязательных + 2 по папке инструкций или по регламенту).
    """
    base = get_examination_question_texts(db, client_id, employee_id)
    if base is None:
        return None
    if len(base) == 0:
        return []

    folder = (get_examination_instructions_folder_url(db, client_id, employee_id) or "").strip()
    if folder:
        tail = (
            f"В папке должностных инструкций ({folder}) перечислены связанные документы. "
            "Какие из них напрямую касаются вашей должности и как вы сопоставляете их с регламентом подразделения?",
            "Какой документ из этой папки вы чаще всего используете в работе и почему он важен для KPI?",
        )
    else:
        a, b = _tail_from_regulation_base(base)
        tail = (a, b)

    out = list(_MANDATORY) + list(tail)
    return clip_question_texts(out)


def fallback_examination_question_texts() -> list[str]:
    """
    Пять текстов вопросов, когда ядро HR не вернуло список (``None``) или регламент отсутствует (``[]``).

    Используется для Part 1 чек-листа по документам, чтобы сотрудник всё равно прошёл тот же каркас (цель, продукт, KPI + два по регламенту).
    """
    a, b = _tail_from_regulation_base([])
    return clip_question_texts(list(_MANDATORY) + [a, b])
