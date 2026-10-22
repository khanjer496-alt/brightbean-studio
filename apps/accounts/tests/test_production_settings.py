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


def _production(*, print_setting="SIGNUP_MODE", **env):
    """Import production settings with the given env vars; returns the CompletedProcess."""
    base_env = {k: v for k, v in os.environ.items() if k not in {"SIGNUP_MODE", "REGISTRATION_OPEN"}}
    return subprocess.run(
        [sys.executable, "-c", f"import config.settings.production as s; print(s.{print_setting})"],
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
