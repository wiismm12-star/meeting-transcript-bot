from __future__ import annotations

import unittest

import httpx

from transcript_bot.deepgram import _format_deepgram_error, _parse_utterances, _parse_words


class DeepgramErrorMessageTests(unittest.TestCase):
    def test_authentication_error_uses_traditional_chinese(self) -> None:
        response = httpx.Response(401)

        self.assertEqual(
            _format_deepgram_error(response),
            "Deepgram API 驗證失敗，請確認伺服器端 API 金鑰設定。",
        )

    def test_utterances_parser_keeps_speaker_time_and_chinese_text(self) -> None:
        segments = _parse_utterances(
            {
                "results": {
                    "utterances": [
                        {"speaker": 0, "start": 0.5, "end": 2.25, "transcript": "大家好。"},
                        {"speaker": 2, "start": "2.4", "end": "4.0", "transcript": "我來說明。"},
                        {"speaker": 1, "start": 5, "end": 6, "transcript": "  "},
                    ]
                }
            }
        )
        self.assertEqual(
            [(segment.speaker, segment.start, segment.end, segment.text) for segment in segments],
            [("SPEAKER_0", 0.5, 2.25, "大家好。"), ("SPEAKER_2", 2.4, 4.0, "我來說明。")],
        )

    def test_words_fallback_groups_words_by_deepgram_speaker(self) -> None:
        segments = _parse_words(
            {
                "results": {
                    "channels": [
                        {
                            "alternatives": [
                                {
                                    "words": [
                                        {"speaker": 0, "start": 0, "end": 0.3, "punctuated_word": "大家"},
                                        {"speaker": 0, "start": 0.3, "end": 0.8, "punctuated_word": "好。"},
                                        {"speaker": 1, "start": 1, "end": 1.5, "word": "收到"},
                                    ]
                                }
                            ]
                        }
                    ]
                }
            }
        )
        self.assertEqual(
            [(segment.speaker, segment.start, segment.end, segment.text) for segment in segments],
            [("SPEAKER_0", 0.0, 0.8, "大家 好。"), ("SPEAKER_1", 1.0, 1.5, "收到")],
        )


if __name__ == "__main__":
    unittest.main()
