from __future__ import annotations

import unittest

import httpx

from transcript_bot.deepgram import _format_deepgram_error


class DeepgramErrorMessageTests(unittest.TestCase):
    def test_authentication_error_uses_traditional_chinese(self) -> None:
        response = httpx.Response(401)

        self.assertEqual(
            _format_deepgram_error(response),
            "Deepgram API 驗證失敗，請確認伺服器端 API 金鑰設定。",
        )


if __name__ == "__main__":
    unittest.main()
