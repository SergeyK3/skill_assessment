# route: (examination) | file: skill_assessment/services/examination_question_plan.py
"""
План вопросов экзамена по ТЗ: цель должности, продукт, KPI; затем два вопроса по папке
должностных инструкций или по регламенту (на основе ответа ядра ``get_examination_question_texts``).
"""

from __future__ import annotations

import hashlib
import os
from random import Random

from sqlalchemy.orm import Session

from skill_assessment.integration.hr_core import (
    get_examination_instructions_folder_url,
    get_examination_question_texts,
)

_MANDATORY_VARIANTS = (
    (
        "Каково назначение или цель вашей должности в организации и подразделении? "
        "Сформулируйте своими словами.",
        "В чём, по-вашему, основная цель вашей должности в подразделении и компании?",
        "Как вы формулируете ключевую миссию своей роли в организации?",
    ),
    (
        "Какой ценный конечный продукт вы создаёте или обеспечиваете в своей работе? Приведите короткий пример.",
        "Какой конкретный результат вашей работы наиболее ценен для бизнеса? Приведите пример из практики.",
        "Что можно назвать вашим основным рабочим продуктом для компании и чем он измерим?",
    ),
    (
        "Назовите ключевые KPI вашей должности и расскажите, как вы их отслеживаете и какие целевые значения для вас актуальны.",
        "Какие KPI для вашей роли вы считаете приоритетными, как контролируете их динамику и к каким значениям стремитесь?",
        "По каким показателям вы оцениваете эффективность своей работы и как регулярно проверяете выполнение плана?",
    ),
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


def _stable_seed_int(*parts: str) -> int:
    src = "|".join([str(p or "").strip() for p in parts])
    h = hashlib.sha256(src.encode("utf-8")).hexdigest()
    return int(h[:16], 16)


def _pick_variant(variants: tuple[str, ...], *, seed: int, slot: int) -> str:
    if not variants:
        return ""
    rnd = Random(seed + slot * 1009)
    idx = rnd.randrange(0, len(variants))
    return variants[idx]


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


def _regulation_topic(raw: str) -> str:
    """
    Вытащить тему из текста регламента/вопроса без «готового ответа».

    Пример: "Цель по регламенту: обеспечивать ... без потерь" -> "цель по регламенту".
    """
    t = " ".join((raw or "").strip().split())
    if not t:
        return "ключевые положения регламента"
    for sep in (":", "—", ";"):
        if sep in t:
            t = t.split(sep, 1)[0].strip()
            break
    if len(t) > 140:
        t = t[:140].rstrip(" ,.;:")
    return t or "ключевые положения регламента"


def compose_examination_question_plan(
    db: Session, client_id: str, employee_id: str, *, seed_key: str | None = None
) -> list[str] | None:
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
    seed = _stable_seed_int("exam_q_plan_v2", client_id, employee_id, seed_key or "")
    mandatory = [
        _pick_variant(_MANDATORY_VARIANTS[0], seed=seed, slot=1),
        _pick_variant(_MANDATORY_VARIANTS[1], seed=seed, slot=2),
        _pick_variant(_MANDATORY_VARIANTS[2], seed=seed, slot=3),
    ]
    if folder:
        tail_a_variants = (
            f"В папке должностных инструкций ({folder}) перечислены связанные документы. "
            "Какие из них напрямую касаются вашей должности и как вы сопоставляете их с регламентом подразделения?",
            f"Посмотрите на состав документов в папке инструкций ({folder}). "
            "Какие документы для вашей роли обязательны в первую очередь и почему?",
            f"В рабочей папке инструкций ({folder}) есть несколько нормативных материалов. "
            "Как вы определяете, какой из них применять в конкретной ситуации?",
        )
        tail_b_variants = (
            "Какой документ из этой папки вы чаще всего используете в работе и почему он важен для KPI?",
            "На какой документ из папки вы опираетесь чаще всего и как он влияет на качество решений?",
            "Приведите пример, когда документ из папки инструкций помог вам выполнить KPI или избежать ошибки.",
        )
        tail = (
            _pick_variant(tail_a_variants, seed=seed, slot=10),
            _pick_variant(tail_b_variants, seed=seed, slot=11),
        )
    else:
        if len(base) >= 2:
            rnd = Random(seed + 707)
            idxs = list(range(len(base)))
            rnd.shuffle(idxs)
            topic_a = _regulation_topic(base[idxs[0]])
            topic_b = _regulation_topic(base[idxs[1]])
            a = (
                f"Опишите своими словами, как вы применяете в работе следующий блок регламента: «{topic_a}». "
                "Какие действия обязательны и как вы проверяете качество выполнения?"
            )
            b = (
                f"Разберите практический пример по теме «{topic_b}»: "
                "как действуете по шагам и какие риски контролируете?"
            )
        else:
            a, b = _tail_from_regulation_base(base)
        tail = (a, b)

    out = mandatory + list(tail)
    return clip_question_texts(out)


def fallback_examination_question_texts() -> list[str]:
    """
    Пять текстов вопросов, когда ядро HR не вернуло список (``None``) или регламент отсутствует (``[]``).

    Используется для Part 1 чек-листа по документам, чтобы сотрудник всё равно прошёл тот же каркас (цель, продукт, KPI + два по регламенту).
    """
    a, b = _tail_from_regulation_base([])
    seed = _stable_seed_int("fallback_exam_q_plan_v2")
    mandatory = [
        _pick_variant(_MANDATORY_VARIANTS[0], seed=seed, slot=1),
        _pick_variant(_MANDATORY_VARIANTS[1], seed=seed, slot=2),
        _pick_variant(_MANDATORY_VARIANTS[2], seed=seed, slot=3),
    ]
    return clip_question_texts(mandatory + [a, b])
