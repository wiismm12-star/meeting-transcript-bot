from __future__ import annotations

import unittest
from pydantic import ValidationError

from transcript_bot.config import Settings


class SettingsValidationTests(unittest.TestCase):
    def test_missing_required_keys_lists_the_required_environment_variables(self) -> None:
        settings = Settings(_env_file=None)

        with self.assertRaisesRegex(RuntimeError, "TELEGRAM_BOT_TOKEN, DEEPGRAM_API_KEY"):
            settings.validate_runtime()

    def test_invalid_transcription_provider_has_an_actionable_error(self) -> None:
        settings = Settings(
            _env_file=None,
            telegram_bot_token="test-token",
            deepgram_api_key="test-key",
            transcribe_provider="unsupported",
        )

        with self.assertRaisesRegex(RuntimeError, "TRANSCRIBE_PROVIDER"):
            settings.validate_runtime()

    def test_optional_telegram_api_id_accepts_blank(self):
        for value in ("", "  ", None):
            with self.subTest(value=value):
                self.assertIsNone(Settings(_env_file=None, telegram_api_id=value).telegram_api_id)
        self.assertEqual(Settings(_env_file=None, telegram_api_id="123").telegram_api_id, 123)

    def test_optional_telegram_api_id_rejects_invalid_nonblank(self):
        with self.assertRaises(ValidationError):
            Settings(_env_file=None, telegram_api_id="invalid")
