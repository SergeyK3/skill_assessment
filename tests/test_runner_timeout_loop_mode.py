from __future__ import annotations


class _FakeDb:
    def commit(self) -> None:
        return None

    def close(self) -> None:
        return None


def test_run_plugin_startup_disables_timeout_loop_when_worker_mode(monkeypatch) -> None:
    from skill_assessment import runner

    calls = {"timeout": 0, "polling": 0}

    monkeypatch.setenv("TELEGRAM_ENABLE_POLLING", "1")
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "123456789:token")
    monkeypatch.setenv("TELEGRAM_POLLING_RUN_IN_UVICORN", "0")

    monkeypatch.setattr(runner, "load_plugin_env", lambda override=False: None)
    monkeypatch.setattr(runner, "apply_skill_assessment_database_migrations", lambda: None)
    monkeypatch.setattr(runner, "ensure_demo_taxonomy", lambda db: None)
    monkeypatch.setattr(runner, "ensure_examination_questions", lambda db: None)
    monkeypatch.setattr(runner, "ensure_competency_matrix_seed", lambda db: None)
    monkeypatch.setattr(runner, "ensure_kpi_matrix_seed", lambda db: None)
    monkeypatch.setattr(runner, "SessionLocal", lambda: _FakeDb())

    import skill_assessment.services.docs_survey_consent_timeout as timeout_mod
    import skill_assessment.integration.telegram_poller as poller_mod

    monkeypatch.setattr(
        timeout_mod,
        "start_consent_timeout_background_task",
        lambda: calls.__setitem__("timeout", calls["timeout"] + 1),
    )
    monkeypatch.setattr(
        poller_mod,
        "start_background_polling",
        lambda token: calls.__setitem__("polling", calls["polling"] + 1),
    )

    runner.run_plugin_startup()
    assert calls["timeout"] == 0
    assert calls["polling"] == 0


def test_run_plugin_startup_runs_timeout_loop_when_embedded_polling(monkeypatch) -> None:
    from skill_assessment import runner

    calls = {"timeout": 0, "polling": 0}

    monkeypatch.setenv("TELEGRAM_ENABLE_POLLING", "1")
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "123456789:token")
    monkeypatch.setenv("TELEGRAM_POLLING_RUN_IN_UVICORN", "1")

    monkeypatch.setattr(runner, "load_plugin_env", lambda override=False: None)
    monkeypatch.setattr(runner, "apply_skill_assessment_database_migrations", lambda: None)
    monkeypatch.setattr(runner, "ensure_demo_taxonomy", lambda db: None)
    monkeypatch.setattr(runner, "ensure_examination_questions", lambda db: None)
    monkeypatch.setattr(runner, "ensure_competency_matrix_seed", lambda db: None)
    monkeypatch.setattr(runner, "ensure_kpi_matrix_seed", lambda db: None)
    monkeypatch.setattr(runner, "SessionLocal", lambda: _FakeDb())

    import skill_assessment.services.docs_survey_consent_timeout as timeout_mod
    import skill_assessment.integration.telegram_poller as poller_mod

    monkeypatch.setattr(
        timeout_mod,
        "start_consent_timeout_background_task",
        lambda: calls.__setitem__("timeout", calls["timeout"] + 1),
    )
    monkeypatch.setattr(
        poller_mod,
        "start_background_polling",
        lambda token: calls.__setitem__("polling", calls["polling"] + 1),
    )

    runner.run_plugin_startup()
    assert calls["timeout"] == 1
    assert calls["polling"] == 1
