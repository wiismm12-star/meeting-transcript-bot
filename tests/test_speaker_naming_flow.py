from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
import unittest

from transcript_bot.bot import PENDING_EXPORTS, PendingExport, _ask_for_speaker_name, _handle_pending_speaker_name
from transcript_bot.database import SpeakerSample


class SpeakerNamingFlowTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        PENDING_EXPORTS.clear()
        self.samples = (
            SpeakerSample("Speaker 1", "第一位主講人的代表發言。"),
            SpeakerSample("Speaker 2", "第二位主講人的代表發言。"),
        )

    def tearDown(self) -> None:
        PENDING_EXPORTS.clear()

    async def test_prompt_asks_for_one_name_without_assignment_syntax(self) -> None:
        message = SimpleNamespace(reply_text=AsyncMock())
        pending_export = PendingExport("meeting-1", "txt", self.samples)

        with patch("transcript_bot.bot.get_speaker_aliases", return_value={}):
            await _ask_for_speaker_name(message, pending_export, 1001)

        prompt = message.reply_text.await_args.args[0]
        self.assertIn("請直接回覆名稱即可", prompt)
        self.assertNotIn("=", prompt)
        keyboard = message.reply_text.await_args.kwargs["reply_markup"]
        self.assertEqual(keyboard.inline_keyboard[0][0].text, "跳過此人")
        self.assertTrue(keyboard.inline_keyboard[0][0].callback_data.startswith("speaker:skip:"))

    async def test_direct_name_saves_current_speaker_then_asks_next_one(self) -> None:
        message = SimpleNamespace(reply_text=AsyncMock())
        pending_export = PendingExport("meeting-1", "txt", self.samples)
        PENDING_EXPORTS[1001] = pending_export

        with (
            patch("transcript_bot.bot.upsert_speaker_aliases") as save_alias,
            patch("transcript_bot.bot._ask_for_speaker_name", new=AsyncMock()) as ask_next,
        ):
            await _handle_pending_speaker_name(message, SimpleNamespace(), pending_export, "派翠克", 1001)

        save_alias.assert_called_once_with(
            unittest.mock.ANY,
            "meeting-1",
            1001,
            {"Speaker 1": "派翠克"},
        )
        self.assertEqual(PENDING_EXPORTS[1001].current_index, 1)
        ask_next.assert_awaited_once()

    async def test_skip_variants_do_not_save_as_a_speaker_name(self) -> None:
        message = SimpleNamespace(reply_text=AsyncMock())
        pending_export = PendingExport("meeting-1", "txt", self.samples)
        PENDING_EXPORTS[1001] = pending_export

        with (
            patch("transcript_bot.bot.upsert_speaker_aliases") as save_alias,
            patch("transcript_bot.bot._ask_for_speaker_name", new=AsyncMock()),
        ):
            await _handle_pending_speaker_name(message, SimpleNamespace(), pending_export, "掠過", 1001)

        save_alias.assert_not_called()


if __name__ == "__main__":
    unittest.main()
