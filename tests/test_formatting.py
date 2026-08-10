from __future__ import annotations

import unittest

from transcript_bot.formatting import (
    apply_speaker_aliases,
    clean_text,
    normalize_speaker_labels,
    polish_local_transcript,
    render_meeting_minutes,
    render_raw_transcript,
    split_segments_by_sentences,
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
        self.assertEqual(result, "Speaker 1：這個是測試。")

    def test_normalizes_speaker_labels_without_merging_source_utterances(self) -> None:
        segments = normalize_speaker_labels(
            [
                TranscriptSegment("42", 0, 1, "第一段"),
                TranscriptSegment("42", 1, 2, "第二段"),
                TranscriptSegment("17", 2, 3, "第三段"),
            ]
        )
        self.assertEqual(
            [(segment.speaker, segment.start, segment.end, segment.text) for segment in segments],
            [
                ("Speaker 1", 0, 1, "第一段"),
                ("Speaker 1", 1, 2, "第二段"),
                ("Speaker 2", 2, 3, "第三段"),
            ],
        )

    def test_splits_long_unpunctuated_turn_into_readable_chunks(self) -> None:
        text = "這是一段沒有任何標點符號的長篇逐字稿" * 12
        chunks = split_segments_by_sentences([TranscriptSegment("Speaker 1", 0, 24, text)])
        self.assertGreater(len(chunks), 1)
        self.assertTrue(all(len(chunk.text) <= 110 for chunk in chunks))
        self.assertEqual("".join(chunk.text for chunk in chunks), text)
        self.assertEqual(chunks[0].start, 0)
        self.assertEqual(chunks[-1].end, 24)

    def test_splitting_keeps_sentence_punctuation(self) -> None:
        text = ("第一句。第二句，仍是第二句。第三句！" * 8)
        chunks = split_segments_by_sentences([TranscriptSegment("Speaker 1", 0, 24, text)])
        self.assertEqual("".join(chunk.text for chunk in chunks), text)

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

if __name__ == "__main__":
    unittest.main()
