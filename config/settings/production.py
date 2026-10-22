import os

from django.core.exceptions import ImproperlyConfigured

from .base import *  # noqa: F401, F403
from .base import ENCRYPTION_KEY_SALT, SECRET_KEY, _refuse_signup_redirect_loop

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
for _name, _value in (("SECRET_KEY", SECRET_KEY), ("ENCRYPTION_KEY_SALT", ENCRYPTION_KEY_SALT)):
    _text = _value.decode("utf-8") if isinstance(_value, bytes) else str(_value or "")
    if len(_text) < 32 or any(
        marker in _text.lower()
        for marker in (
            "generate_",
            "placeholder",
            "change-me",
            "changeme",
            "django-insecure",
        )
    ):
        raise ImproperlyConfigured(f"{_name} must contain a generated secret of at least 32 characters")


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
