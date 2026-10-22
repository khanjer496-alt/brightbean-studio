"""What config.settings.production refuses, and what it defaults to.

Each case imports the settings in a fresh interpreter, because they are
evaluated once at import and the suite itself runs on config.settings.test.
"""

import os
import subprocess
import sys

import pytest
from django.conf import settings as django_settings

STRONG_SECRET = "k" * 24 + "Zq8vT3nW-e2Lr9Yp"
STRONG_SALT = "s" * 24 + "Hx4mB7cJ-u6Ka1Gd"


def _production(*, show="s.SIGNUP_MODE", **env):
    """Import production settings as ``s`` with the given env vars and print ``show``."""
    base_env = {k: v for k, v in os.environ.items() if k not in {"SIGNUP_MODE", "REGISTRATION_OPEN"}}
    return subprocess.run(
        [sys.executable, "-c", f"import config.settings.production as s; print({show})"],
        cwd=django_settings.BASE_DIR,
        env={
            **base_env,
            "SECRET_KEY": STRONG_SECRET,
            "ENCRYPTION_KEY_SALT": STRONG_SALT,
            "APP_URL": "https://studio.example.com",
            "ALLOWED_HOSTS": "studio.example.com",
            **env,
        },
        capture_output=True,
        text=True,
        timeout=60,
    )


class TestSignupModeDefault:
    def test_invite_when_nothing_is_set(self):
        result = _production()

        assert result.returncode == 0, result.stderr
        assert result.stdout.strip() == "invite"

    @pytest.mark.parametrize("mode", ["open", "closed"])
    def test_explicit_mode_is_kept(self, mode):
        result = _production(SIGNUP_MODE=mode)

        assert result.returncode == 0, result.stderr
        assert result.stdout.strip() == mode

    def test_explicit_registration_open_is_kept(self):
        result = _production(REGISTRATION_OPEN="true")

        assert result.returncode == 0, result.stderr
        assert result.stdout.strip() == "open"

    def test_default_still_guards_the_redirect_loop(self):
        result = _production(CUSTOM_SIGNUP_URL="/accounts/signup/")

        assert result.returncode != 0
        assert "CUSTOM_SIGNUP_URL points at Studio's own signup page" in result.stderr


class TestRateLimitCacheFlag:
    def test_set_without_redis(self):
        result = _production(show="s.RATE_LIMIT_CACHE_IS_PROCESS_LOCAL", REDIS_URL="")

        assert result.returncode == 0, result.stderr
        assert result.stdout.strip() == "True"

    def test_clear_with_redis(self):
        result = _production(show="s.RATE_LIMIT_CACHE_IS_PROCESS_LOCAL", REDIS_URL="redis://localhost:6379/0")

        assert result.returncode == 0, result.stderr
        assert result.stdout.strip() == "False"


class TestSecrets:
    @pytest.mark.parametrize("name", ["SECRET_KEY", "ENCRYPTION_KEY_SALT"])
    @pytest.mark.parametrize(
        "value",
        [
            "",
            "short-but-random-Zq8vT3nW",
            # Shipped in .env.example, .env.postdelegate.example and test.py/CI.
            "change-me-to-a-random-string",
            "GENERATE_WITH_OPENSSL_RAND_BASE64_48",
            "test-secret-key-not-for-production",
        ],
    )
    def test_refuses_to_start(self, name, value):
        result = _production(**{name: value})

        assert result.returncode != 0
        assert f"ImproperlyConfigured: {name} must contain a generated secret" in result.stderr

    def test_starts_with_generated_values(self):
        result = _production(show="s.DEBUG")

        assert result.returncode == 0, result.stderr

    def test_reads_every_shipped_example_value(self, tmp_path):
        """Any value these files give the variables is refused, however long and random it looks."""
        (tmp_path / ".env.example").write_text("SECRET_KEY=from-env-example\nOTHER=x\n")
        (tmp_path / ".env.postdelegate.example").write_text('ENCRYPTION_KEY_SALT="from-postdelegate-example"\n')
        (tmp_path / "app.json").write_text(
            '{"env": {"SECRET_KEY": {"value": "From-App-Json"}, "ENCRYPTION_KEY_SALT": {"generator": "secret"}}}'
        )
        (tmp_path / "render.yaml").write_text(
            "envVars:\n  - key: ENCRYPTION_KEY_SALT\n    value: from-render-yaml\n  - key: SECRET_KEY\n    generateValue: true\n"
        )

        result = _production(show=f"sorted(s._shipped_secret_values(__import__('pathlib').Path({str(tmp_path)!r})))")

        assert result.returncode == 0, result.stderr
        assert result.stdout.strip() == str(
            ["from-app-json", "from-env-example", "from-postdelegate-example", "from-render-yaml"]
        )

    def test_the_real_examples_are_read(self):
        result = _production(show="sorted(s._shipped)")

        assert result.returncode == 0, result.stderr
        assert "change-me-to-a-random-string" in result.stdout
        assert "generate_with_openssl_rand_base64_48" in result.stdout
