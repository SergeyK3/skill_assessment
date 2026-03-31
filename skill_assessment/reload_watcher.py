# route: (dev) | file: skill_assessment/reload_watcher.py
"""Каталоги для uvicorn ``reload_dirs``: иначе при cwd ≠ репозитория правки не перезапускают процесс."""

from __future__ import annotations

from pathlib import Path

from skill_assessment.bootstrap import _discover_typical_infra_root_dir
from skill_assessment.env import load_plugin_env


def uvicorn_reload_dir_list() -> list[str]:
    """
    Возвращает абсолютные пути: корень установки плагина + ``app/`` ядра (если найдено).

    Без этого ``uvicorn … --reload`` часто смотрит только на текущий cwd, и изменения
    в sibling ``typical_infrastructure`` или в пакете при другом cwd не дают reload.
    """
    load_plugin_env(override=False)
    out: list[str] = []
    # skill_assessment/reload_watcher.py → родитель пакета → корень дистрибутива (рядом с pyproject.toml)
    plugin_root = Path(__file__).resolve().parent.parent
    if plugin_root.is_dir():
        out.append(str(plugin_root))
    infra = _discover_typical_infra_root_dir()
    if infra is not None:
        app_dir = infra / "app"
        if app_dir.is_dir():
            out.append(str(app_dir))
    return out
