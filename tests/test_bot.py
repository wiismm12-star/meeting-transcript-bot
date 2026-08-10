from __future__ import annotations

import unittest

from telegram.error import BadRequest

from transcript_bot.bot import _telegram_download_error_message


class TelegramDownloadErrorTests(unittest.TestCase):
    def test_file_too_big_has_an_actionable_traditional_chinese_message(self) -> None:
        message = _telegram_download_error_message(BadRequest("File is too big"))

        self.assertIn("大小限制", message)
        self.assertIn("本機 Web 工作台", message)

    def test_other_bad_request_does_not_expose_telegram_details(self) -> None:
        message = _telegram_download_error_message(BadRequest("private API detail"))

        self.assertNotIn("private API detail", message)
        self.assertIn("無法從 Telegram 下載", message)


if __name__ == "__main__":
    unittest.main()
