"""Settings that make the app unsafe to expose.

Setting ENVIRONMENT=production is the operator saying "this is live". These
checks run at that point and refuse to start, rather than logging a warning
that scrolls past in a container log while the server signs real sessions with
a key published in this repository.
"""

from __future__ import annotations

import pytest

from app.core.config import Settings

GOOD = {
    "environment": "production",
    "debug": False,
    "secret_key": "a-long-random-value-that-is-definitely-over-32-characters",
    "bootstrap_admin_password": "set-by-the-operator",
    "cors_origins": "",
}


def _settings(**overrides) -> Settings:
    return Settings(**{**GOOD, **overrides})


class TestWhatCountsAsUnsafe:
    def test_a_correctly_configured_deployment_has_no_problems(self):
        assert _settings().production_problems() == []

    def test_the_published_signing_key_is_refused(self):
        problems = _settings(
            secret_key="change-me-in-production-please-use-a-long-random-string"
        ).production_problems()
        assert len(problems) == 1
        assert "SECRET_KEY" in problems[0]
        # The message has to say what to do, not just what is wrong.
        assert "secrets.token_urlsafe" in problems[0]

    def test_a_short_signing_key_is_refused(self):
        problems = _settings(secret_key="tooshort").production_problems()
        assert any("SECRET_KEY" in problem for problem in problems)

    def test_the_published_admin_password_is_refused(self):
        problems = _settings(bootstrap_admin_password="ChangeMe!2024").production_problems()
        assert any("BOOTSTRAP_ADMIN_PASSWORD" in problem for problem in problems)

    def test_debug_is_refused(self):
        problems = _settings(debug=True).production_problems()
        assert any("DEBUG" in problem for problem in problems)

    def test_wide_open_cors_is_refused(self):
        """With credentials enabled it makes the server echo back any origin."""
        problems = _settings(cors_origins="*").production_problems()
        assert any("CORS_ORIGINS" in problem for problem in problems)

    def test_named_origins_are_fine(self):
        assert _settings(cors_origins="https://agile.example.org").production_problems() == []

    def test_every_problem_is_reported_at_once(self):
        """One restart should surface all of them, not one per attempt."""
        problems = Settings(
            environment="production",
            debug=True,
            secret_key="change-me",
            bootstrap_admin_password="ChangeMe!2024",
            cors_origins="*",
        ).production_problems()
        assert len(problems) == 4


class TestDefaults:
    def test_cors_is_same_origin_out_of_the_box(self):
        """The bundled dashboard is same-origin; nothing else should be assumed."""
        assert Settings(secret_key="x" * 40).cors_origin_list == []

    def test_development_is_not_policed(self):
        """Local work must stay frictionless; the checks are production-only."""
        development = Settings()
        assert development.is_production is False
        assert development.production_problems()  # would fail, but nothing reads it


class TestTheGuardIsWiredIn:
    @pytest.mark.parametrize("env", ["production", "PRODUCTION", "prod"])
    def test_production_is_recognised_however_it_is_spelled(self, env):
        assert Settings(environment=env, secret_key="x" * 40).is_production is True

    def test_other_environments_are_not_production(self):
        for env in ("development", "staging", "test", ""):
            assert Settings(environment=env, secret_key="x" * 40).is_production is False
