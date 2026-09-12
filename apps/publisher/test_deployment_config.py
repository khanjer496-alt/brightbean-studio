import os
import subprocess
import sys

from django.test import SimpleTestCase


class ProductionCredentialConfigTest(SimpleTestCase):
    def load_settings(self, **overrides):
        env = {
            **os.environ,
            "SECRET_KEY": "local-test-only-" + "a" * 48,
            "ENCRYPTION_KEY_SALT": "local-test-only-" + "b" * 48,
            "DATABASE_URL": "sqlite:///:memory:",
            "STORAGE_BACKEND": "local",
            **overrides,
        }
        return subprocess.run(
            [sys.executable, "-c", "import config.settings.production"],
            env=env,
            capture_output=True,
            text=True,
            timeout=15,
            check=False,
        )

    def test_explicit_generated_format_is_accepted(self):
        self.assertEqual(self.load_settings().returncode, 0)

    def test_missing_salt_short_secret_and_template_values_are_rejected(self):
        for overrides in (
            {"ENCRYPTION_KEY_SALT": ""},
            {"SECRET_KEY": "short"},
            {"SECRET_KEY": "GENERATE_WITH_OPENSSL_RAND_BASE64_48"},
            {"ENCRYPTION_KEY_SALT": "GENERATE_WITH_OPENSSL_RAND_BASE64_48"},
        ):
            with self.subTest(field=next(iter(overrides))):
                result = self.load_settings(**overrides)
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("must contain a generated secret", result.stderr)


class PublisherHeartbeatTest(SimpleTestCase):
    def test_only_completed_cycles_refresh_worker_health(self):
        from unittest.mock import patch

        from apps.publisher.tasks import run_publish_cycle

        with (
            patch("apps.publisher.engine.PublishEngine.poll_and_publish", return_value=0),
            patch("apps.publisher.tasks.Path.touch") as touch,
        ):
            run_publish_cycle.now()
        touch.assert_called_once()
        with (
            patch(
                "apps.publisher.engine.PublishEngine.poll_and_publish", side_effect=RuntimeError("test cycle failed")
            ),
            patch("apps.publisher.tasks.Path.touch") as touch,
            self.assertRaises(RuntimeError),
        ):
            run_publish_cycle.now()
        touch.assert_not_called()
