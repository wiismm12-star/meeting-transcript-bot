from __future__ import annotations

import unittest

from transcript_bot.formatting import (
    apply_speaker_aliases,
    clean_text,
    normalize_speaker_labels,
    polish_local_transcript,
    render_action_summary,
    render_meeting_minutes,
    render_raw_transcript,
    to_traditional,
)
from transcript_bot.transcription import TranscriptSegment


class FormattingTests(unittest.TestCase):
    def test_removes_artificial_spaces_between_chinese_tokens(self) -> None:
        self.assertEqual(clean_text("風雲 榜 今年 要 舉辦 第18 屆"), "風雲榜今年要舉辦第18屆")

    def test_corrects_confirmed_escalator_recognition_error(self) -> None:
        self.assertEqual(clean_text("請搭乘首扶梯上樓"), "請搭乘手扶梯上樓")

    def test_converts_simplified_chinese_to_traditional_chinese(self) -> None:
        self.assertEqual(
            to_traditional("会议记录：项目已经确认，下周继续跟进。"),
            "會議記錄：專案已經確認，下週繼續跟進。",
        )

    def test_local_polish_removes_fillers_spacing_and_duplicate_lines(self) -> None:
        result = polish_local_transcript(
            "Speaker 1: 呃，這 個 是 測試。。\n\nSpeaker 1: 呃，這 個 是 測試。。"
        )
        self.assertEqual(result, "Speaker 1：這個是測試")

    def test_normalizes_and_merges_source_speaker_labels(self) -> None:
        segments = normalize_speaker_labels(
            [
                TranscriptSegment("42", 0, 1, "第一段"),
                TranscriptSegment("42", 1, 2, "第二段"),
                TranscriptSegment("17", 2, 3, "第三段"),
            ]
        )
        self.assertEqual(
            [(segment.speaker, segment.start, segment.end, segment.text) for segment in segments],
            [("Speaker 1", 0, 2, "第一段第二段"), ("Speaker 2", 2, 3, "第三段")],
        )

    def test_applies_aliases_only_to_speaker_labels(self) -> None:
        text = "Speaker 1：主持人提到 Speaker 2。\n\nSpeaker 2：收到。"
        self.assertEqual(
            apply_speaker_aliases(text, {"Speaker 1": "主持人", "Speaker 2": "來賓"}),
            "主持人：主持人提到 Speaker 2。\n\n來賓：收到。",
        )

    def test_raw_rendering_preserves_unpolished_turn_text(self) -> None:
        result = render_raw_transcript([TranscriptSegment("Speaker 1", 0, 1, "的歌手  t 你")])
        self.assertEqual(result, "Speaker 1：的歌手  t 你")

    def test_meeting_minutes_keeps_speaker_statements_without_inventing_decisions(self) -> None:
        result = render_meeting_minutes("Speaker 1：確認時程。\n\nSpeaker 2：下週回覆。")
        self.assertEqual(
            result,
            "# 會議紀錄\n\n## 發言摘要\n- Speaker 1：確認時程。\n- Speaker 2：下週回覆。",
        )

    def test_action_summary_extracts_only_explicit_action_language(self) -> None:
        result = render_action_summary(
            "Speaker 1：今天討論專案。\n\nSpeaker 2：下週確認測試時程。"
        )
        self.assertEqual(
            result,
            "# 決議事項摘要\n\n## 原文明確提及的事項\n- Speaker 2：下週確認測試時程。",
        )

    def test_action_summary_does_not_infer_a_decision(self) -> None:
        result = render_action_summary("Speaker 1：今天討論專案。")
        self.assertEqual(result, "# 決議事項摘要\n\n未偵測到原文中明確的決議或行動事項。")


if __name__ == "__main__":
    unittest.main()
