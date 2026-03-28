from __future__ import annotations

import os

from skill_assessment.env import load_env_file


def test_load_env_file_does_not_override_process_env_by_default(monkeypatch, tmp_path) -> None:
    env_file = tmp_path / ".env"
    env_file.write_text("SA_TEST_ENV_KEY=from_file\n", encoding="utf-8")
    monkeypatch.setenv("SA_TEST_ENV_KEY", "from_process")

    loaded = load_env_file(env_file)

    assert loaded is True
    assert os.environ["SA_TEST_ENV_KEY"] == "from_process"


def test_load_env_file_can_override_when_requested(monkeypatch, tmp_path) -> None:
    env_file = tmp_path / ".env"
    env_file.write_text("SA_TEST_ENV_KEY=from_file\n", encoding="utf-8")
    monkeypatch.setenv("SA_TEST_ENV_KEY", "from_process")

    loaded = load_env_file(env_file, override=True)

    assert loaded is True
    assert os.environ["SA_TEST_ENV_KEY"] == "from_file"
