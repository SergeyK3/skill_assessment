# route: (startup seed) | file: skill_assessment/services/taxonomy_seed.py
"""Демо-таксономия при пустых таблицах."""

from __future__ import annotations

import json
import uuid

from sqlalchemy import select
from sqlalchemy.orm import Session

from skill_assessment.infrastructure.db_models import SkillDomainRow, SkillRow


def ensure_demo_taxonomy(db: Session) -> None:
    if db.scalar(select(SkillDomainRow.id).limit(1)) is not None:
        return

    d_id = str(uuid.uuid4())
    domain = SkillDomainRow(id=d_id, code="COMM", title="Коммуникации")
    db.add(domain)
    db.add(
        SkillRow(
            id=str(uuid.uuid4()),
            domain_id=d_id,
            code="PRESENTATION",
            title="Презентация",
        )
    )
    db.add(
        SkillRow(
            id=str(uuid.uuid4()),
            domain_id=d_id,
            code="FEEDBACK",
            title="Обратная связь",
        )
    )
