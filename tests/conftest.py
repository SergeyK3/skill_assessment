"""
Подключаем корень typical_infrastructure в sys.path, чтобы пакет ``app`` находился при pytest.

Структура каталогов::

    Stage3HR/
      typical_infrastructure/   <- PYTHONPATH
      repo/skill_assessment/tests/
"""

from __future__ import annotations

import sys
from pathlib import Path


def pytest_configure() -> None:
    here = Path(__file__).resolve()
    repo = here.parents[2]
    core = repo.parent / "typical_infrastructure"
    if core.is_dir():
        sys.path.insert(0, str(core))
    else:
        raise RuntimeError(
            f"Не найден каталог ядра: {core}. Запускайте pytest из среды, где рядом лежит typical_infrastructure."
        )
