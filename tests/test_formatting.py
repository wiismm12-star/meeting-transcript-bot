from __future__ import annotations

import unittest

from transcript_bot.formatting import clean_text, render_meeting_minutes, render_raw_transcript
from transcript_bot.transcription import TranscriptSegment


class FormattingTests(unittest.TestCase):
    def test_removes_artificial_spaces_between_chinese_tokens(self) -> None:
        self.assertEqual(clean_text("風雲 榜 今年 要 舉辦 第18 屆"), "風雲榜今年要舉辦第18屆")

    def test_corrects_confirmed_escalator_recognition_error(self) -> None:
        self.assertEqual(clean_text("請搭乘首扶梯上樓"), "請搭乘手扶梯上樓")

    def test_raw_rendering_preserves_unpolished_turn_text(self) -> None:
        result = render_raw_transcript([TranscriptSegment("Speaker 1", 0, 1, "的歌手  t 你")])
        self.assertEqual(result, "Speaker 1：的歌手  t 你")

    def test_meeting_minutes_keeps_speaker_statements_without_inventing_decisions(self) -> None:
        result = render_meeting_minutes("Speaker 1：確認時程。\n\nSpeaker 2：下週回覆。")
        self.assertEqual(
            result,
            "# 會議紀錄\n\n## 發言摘要\n- Speaker 1：確認時程。\n- Speaker 2：下週回覆。",
        )


if __name__ == "__main__":
    unittest.main()
