from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
import unittest

from transcript_bot.bot import (
    PENDING_EMAILS,
    PENDING_EXPORTS,
    PendingEmail,
    PendingExport,
    _ask_for_speaker_name,
    _email_skip_keyboard,
    _handle_pending_speaker_name,
    handle_email_action,
)
from transcript_bot.database import SpeakerSample


class SpeakerNamingFlowTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        PENDING_EXPORTS.clear()
        PENDING_EMAILS.clear()
        self.samples = (
            SpeakerSample("Speaker 1", "第一位主講人的代表發言。"),
            SpeakerSample("Speaker 2", "第二位主講人的代表發言。"),
        )

    def tearDown(self) -> None:
        PENDING_EXPORTS.clear()
        PENDING_EMAILS.clear()

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

    def test_email_prompt_has_a_skip_button(self) -> None:
        keyboard = _email_skip_keyboard("meeting-1", "both")
        button = keyboard.inline_keyboard[0][0]
        self.assertEqual(button.text, "略過寄送")
        self.assertEqual(button.callback_data, "email:skip:meeting-1:both")

    def test_export_keyboard_labels_word_document_clearly(self) -> None:
        from transcript_bot.bot import _export_keyboard

        buttons = [button.text for row in _export_keyboard("meeting-1").inline_keyboard for button in row]
        self.assertIn("輸出 Word 檔（DOCX）", buttons)
        self.assertIn("TXT ＋ Word", buttons)

    async def test_email_skip_button_clears_pending_delivery(self) -> None:
        message = SimpleNamespace(reply_text=AsyncMock())
        query = SimpleNamespace(
            data="email:skip:meeting-1:both",
            message=message,
            answer=AsyncMock(),
            edit_message_reply_markup=AsyncMock(),
        )
        PENDING_EMAILS[1001] = PendingEmail("meeting-1", "both")

        await handle_email_action(SimpleNamespace(callback_query=query, effective_user=SimpleNamespace(id=1001)), SimpleNamespace())

        self.assertNotIn(1001, PENDING_EMAILS)
        query.answer.assert_awaited_once()
        query.edit_message_reply_markup.assert_awaited_once_with(reply_markup=None)
        message.reply_text.assert_awaited_once_with("已略過 Email 寄送。")


if __name__ == "__main__":
    unittest.main()
