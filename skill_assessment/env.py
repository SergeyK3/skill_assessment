"""Общие helper-функции для загрузки `.env` без расхождения по стилю."""

from __future__ import annotations

from pathlib import Path

from dotenv import load_dotenv

PLUGIN_ROOT = Path(__file__).resolve().parent.parent
PLUGIN_ENV_FILE = PLUGIN_ROOT / ".env"


def load_env_file(path: str | Path, *, override: bool = False) -> bool:
    """Загрузить env-файл, если он существует; вернуть ``True`` при наличии файла."""
    p = Path(path)
    if not p.is_file():
        return False
    load_dotenv(p, override=override)
    return True


def load_plugin_env(*, override: bool = False) -> bool:
    """
    Подхватить `.env` пакета skill_assessment.

    По умолчанию ``override=False``: переменные процесса (в т.ч. pytest/monkeypatch)
    имеют приоритет над файлом.
    """
    return load_env_file(PLUGIN_ENV_FILE, override=override)
