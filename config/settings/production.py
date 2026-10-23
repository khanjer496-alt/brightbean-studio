import json
import os
import re

from django.core.exceptions import ImproperlyConfigured

from .base import *  # noqa: F401, F403
from .base import ENCRYPTION_KEY_SALT, REDIS_URL, SECRET_KEY, _refuse_signup_redirect_loop

DEBUG = False

# base.py defaults SIGNUP_MODE to "open" so a fresh local checkout can sign up.
# A production deployment is reachable from the whole internet, so unless the
# operator chose a mode (SIGNUP_MODE, or the older REGISTRATION_OPEN flag) only
# invited people may create accounts. Checked after base.py so a value from
# .env counts as chosen.
if "SIGNUP_MODE" not in os.environ and "REGISTRATION_OPEN" not in os.environ:
    SIGNUP_MODE = "invite"
    _refuse_signup_redirect_loop(SIGNUP_MODE)

# Encryption derives from these values, so a placeholder or missing salt would
# compromise both sessions and stored platform credentials before the first user.
# Besides the length and the usual placeholder words, refuse any value that a
# file in this repository ships for these variables: copying .env.example as is
# would otherwise give every such install the same, public, keys.
_SECRET_NAMES = ("SECRET_KEY", "ENCRYPTION_KEY_SALT")


def _shipped_secret_values(base_dir):
    """Values that .env.example, .env.postdelegate.example, app.json and render.yaml give these variables."""
    values = set()
    for dotenv in (".env.example", ".env.postdelegate.example"):
        path = base_dir / dotenv
        if path.exists():
            for line in path.read_text().splitlines():
                name, _, value = line.partition("=")
                if name.strip() in _SECRET_NAMES:
                    values.add(value.strip().strip("\"'"))
    app_json = base_dir / "app.json"
    if app_json.exists():
        app_env = json.loads(app_json.read_text()).get("env", {})
        values.update(str(app_env[name]["value"]) for name in _SECRET_NAMES if "value" in app_env.get(name, {}))
    render_yaml = base_dir / "render.yaml"
    if render_yaml.exists():
        pattern = rf"key:\s*(?:{'|'.join(_SECRET_NAMES)})\s*\n\s*value:\s*['\"]?([^'\"\n]+)"
        values.update(match.strip() for match in re.findall(pattern, render_yaml.read_text()))
    return {value.lower() for value in values if value}


_shipped = _shipped_secret_values(BASE_DIR)  # noqa: F405
for _name, _value in zip(_SECRET_NAMES, (SECRET_KEY, ENCRYPTION_KEY_SALT), strict=True):
    _text = _value.decode("utf-8") if isinstance(_value, bytes) else str(_value or "")
    if (
        len(_text) < 32
        or _text.lower() in _shipped
        or any(
            marker in _text.lower()
            for marker in (
                "generate_",
                "placeholder",
                "change-me",
                "changeme",
                "django-insecure",
                "not-for-production",
            )
        )
    ):
        raise ImproperlyConfigured(
            f"{_name} must contain a generated secret of at least 32 characters, not empty or a value shipped "
            f"in this repository's example config (e.g. python -c 'import secrets; print(secrets.token_urlsafe(48))')"
        )

# Without REDIS_URL the default cache is a LocMemCache inside each process, so
# every rate limit that counts in it (login/signup attempts, django-ratelimit,
# the Agent API throttle) is per gunicorn worker and resets on restart.
# AccountsConfig.ready() logs a warning at startup when this is True.
RATE_LIMIT_CACHE_IS_PROCESS_LOCAL = not REDIS_URL

# Security
SECURE_SSL_REDIRECT = True
SECURE_HSTS_SECONDS = 31536000
SECURE_HSTS_INCLUDE_SUBDOMAINS = True
SECURE_HSTS_PRELOAD = True
SESSION_COOKIE_SECURE = True
CSRF_COOKIE_SECURE = True
SECURE_CONTENT_TYPE_NOSNIFF = True
SECURE_PROXY_SSL_HEADER = ("HTTP_X_FORWARDED_PROTO", "https")
SECURE_REDIRECT_EXEMPT = [r"^health/$"]

# Logging
LOGGING = {
    "version": 1,
    "disable_existing_loggers": False,
    "formatters": {
        "verbose": {
            "format": "{levelname} {asctime} {module} {message}",
            "style": "{",
        },
    },
    "handlers": {
        "console": {
            "class": "logging.StreamHandler",
            "formatter": "verbose",
        },
    },
    "root": {
        "handlers": ["console"],
        "level": "WARNING",
    },
    "loggers": {
        "django": {
            "handlers": ["console"],
            "level": "WARNING",
            "propagate": False,
        },
        "apps": {
            "handlers": ["console"],
            "level": "INFO",
            "propagate": False,
        },
        "gunicorn.error": {
            "handlers": ["console"],
            "level": "INFO",
            "propagate": False,
        },
    },
}
