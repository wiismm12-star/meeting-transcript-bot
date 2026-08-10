from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock, patch
import unittest

import httpx
from pathlib import Path

from transcript_bot.ollama_client import (
    OllamaError,
    _repair_display_text,
    _split_transcript,
    apply_glossary,
    polish_with_ollama,
    summarize_meeting_with_ollama,
)


def _chat_response(content: str) -> MagicMock:
    response = MagicMock(status_code=200)
    response.json.return_value = {"message": {"content": content}}
    return response


class OllamaPolishTests(unittest.TestCase):
    def setUp(self) -> None:
        self.settings = SimpleNamespace(
            ollama_base_url="http://127.0.0.1:11434/",
            ollama_text_model="qwen3:8b",
        )

    def test_sends_a_non_streaming_local_polish_request(self) -> None:
        response = MagicMock(status_code=200)
        response.json.return_value = {"message": {"content": "Original content."}}

        with (
            patch("transcript_bot.ollama_client.settings", self.settings),
            patch("transcript_bot.ollama_client.httpx.post", return_value=response) as post,
        ):
            result = polish_with_ollama("Speaker 1: Original content")

        self.assertEqual(result, "Speaker 1：Original content.")
        self.assertEqual(post.call_args.args[0], "http://127.0.0.1:11434/api/chat")
        payload = post.call_args.kwargs["json"]
        self.assertEqual(payload["model"], "qwen3:8b")
        self.assertEqual(payload["messages"][0]["role"], "system")
        self.assertFalse(payload["stream"])
        self.assertFalse(payload["think"])

    def test_preserves_a_custom_speaker_label_while_polishing_its_content(self) -> None:
        response = MagicMock(status_code=200)
        response.json.return_value = {"message": {"content": "Original, revised content."}}

        with (
            patch("transcript_bot.ollama_client.settings", self.settings),
            patch("transcript_bot.ollama_client.httpx.post", return_value=response),
        ):
            result = polish_with_ollama("Host: Original revised content")

        self.assertEqual(result, "Host：Original, revised content.")

    def test_connection_failure_has_actionable_error(self) -> None:
        with (
            patch("transcript_bot.ollama_client.settings", self.settings),
            patch("transcript_bot.ollama_client.httpx.post", side_effect=httpx.ConnectError("offline")),
        ):
            with self.assertRaises(OllamaError):
                polish_with_ollama("Speaker 1: Original content")

    def test_keeps_source_when_model_reports_uncertainty(self) -> None:
        response = MagicMock(status_code=200)
        response.json.return_value = {"message": {"content": "DROP"}}

        with (
            patch("transcript_bot.ollama_client.settings", self.settings),
            patch("transcript_bot.ollama_client.httpx.post", return_value=response),
        ):
            result = polish_with_ollama("Speaker 1: Original content")

        self.assertEqual(result, "Speaker 1：Original content")

    def test_keeps_source_when_model_invents_unrelated_content(self) -> None:
        response = MagicMock(status_code=200)
        response.json.return_value = {"message": {"content": "Entirely unrelated prose."}}

        with (
            patch("transcript_bot.ollama_client.settings", self.settings),
            patch("transcript_bot.ollama_client.httpx.post", return_value=response),
        ):
            result = polish_with_ollama("Speaker 1: Original content")

        self.assertEqual(result, "Speaker 1：Original content")

    def test_keeps_an_incomplete_sentence_when_model_cannot_improve_it(self) -> None:
        response = MagicMock(status_code=200)
        response.json.return_value = {"message": {"content": "and a fragment"}}

        with (
            patch("transcript_bot.ollama_client.settings", self.settings),
            patch("transcript_bot.ollama_client.httpx.post", return_value=response),
        ):
            result = polish_with_ollama("Speaker 1: and a fragment")

        self.assertEqual(result, "Speaker 1：and a fragment")

    def test_trims_only_a_dangling_prefix_and_ascii_tail(self) -> None:
        dangling = chr(0x7684) + chr(0x6b4c) + chr(0x624b)
        response = MagicMock(status_code=200)
        response.json.return_value = {"message": {"content": dangling + ", t " + chr(0x4f60)}}

        with (
            patch("transcript_bot.ollama_client.settings", self.settings),
            patch("transcript_bot.ollama_client.httpx.post", return_value=response),
        ):
            result = polish_with_ollama("Speaker 1: " + dangling + ", t " + chr(0x4f60))

        self.assertEqual(result, "Speaker 1：" + chr(0x6b4c) + chr(0x624b))

    def test_trims_ascii_tail_without_a_preceding_comma(self) -> None:
        text = chr(0x62cd) + chr(0x7167) + "t" + chr(0x4f60)
        response = MagicMock(status_code=200)
        response.json.return_value = {"message": {"content": text}}

        with (
            patch("transcript_bot.ollama_client.settings", self.settings),
            patch("transcript_bot.ollama_client.httpx.post", return_value=response),
        ):
            result = polish_with_ollama("Speaker 1: " + text)

        self.assertEqual(result, "Speaker 1：" + chr(0x62cd) + chr(0x7167))

    def test_repairs_utf8_decoded_as_latin1_display_text(self) -> None:
        mojibake_colon = b"\xef\xbc\x9a".decode("latin-1")
        self.assertEqual(_repair_display_text(mojibake_colon), chr(0xFF1A))

    def test_glossary_corrects_known_asr_misreading(self) -> None:
        # KKBUS is the ASR misreading of the brand KKBOX in the promo clip.
        with patch("transcript_bot.ollama_client._load_glossary", return_value=[("KKBUS", "KKBOX")]):
            self.assertEqual(apply_glossary("前往KKBUS風雲榜官網"), "前往KKBOX風雲榜官網")

    def test_glossary_runs_after_model_even_if_model_keeps_misreading(self) -> None:
        # The model returns the text unchanged (still containing KKBUS); the
        # post-polish glossary pass must still fix it deterministically.
        response = MagicMock(status_code=200)
        response.json.return_value = {"message": {"content": "到KKBUS風雲榜官網搜尋。"}}

        with (
            patch("transcript_bot.ollama_client.settings", self.settings),
            patch("transcript_bot.ollama_client._load_glossary", return_value=[("KKBUS", "KKBOX")]),
            patch("transcript_bot.ollama_client.httpx.post", return_value=response),
        ):
            result = polish_with_ollama("Speaker 1: 到KKBUS風雲榜官網搜尋")

        self.assertEqual(result, "Speaker 1：到KKBOX風雲榜官網搜尋。")

    def test_glossary_missing_file_is_safe_noop(self) -> None:
        missing = SimpleNamespace(glossary_file=Path("/nonexistent/glossary.txt"))
        with patch("transcript_bot.ollama_client.settings", missing):
            self.assertEqual(apply_glossary("KKBUS 活動"), "KKBUS 活動")


class OllamaSummaryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.settings = SimpleNamespace(
            ollama_base_url="http://127.0.0.1:11434/",
            ollama_text_model="qwen3:8b",
        )

    def test_short_transcript_summarized_in_single_call(self) -> None:
        resp = _chat_response('{"title":"會議","overview":"概述。","highlights":["重點一","重點二"]}')
        with (
            patch("transcript_bot.ollama_client.settings", self.settings),
            patch("transcript_bot.ollama_client.httpx.post", return_value=resp) as post,
        ):
            out = summarize_meeting_with_ollama("Speaker 1：簡短內容。", "預設標題")
        self.assertEqual(out["title"], "會議")
        self.assertEqual(out["overview"], "概述。")
        self.assertEqual(out["highlights"], ["重點一", "重點二"])
        # One call => short path, no map-reduce chunking.
        self.assertEqual(post.call_count, 1)

    def test_schema_ignore_blob_raises_and_avoids_verbatim_fallback(self) -> None:
        # qwen3 over a long prompt sometimes echoes the transcript under a
        # "transcript" key instead of the requested title/overview/highlights.
        resp = _chat_response('{"transcript":"Speaker 1：一大段逐字稿…"}')
        with (
            patch("transcript_bot.ollama_client.settings", self.settings),
            patch("transcript_bot.ollama_client.httpx.post", return_value=resp),
        ):
            with self.assertRaises(OllamaError):
                summarize_meeting_with_ollama("Speaker 1：一大段逐字稿內容。" * 50)

    def test_long_transcript_uses_map_reduce_then_merge(self) -> None:
        # Long input must call the chunk summarizer (>=1) AND the merge step,
        # and must NOT fall back to the verbatim copy-transcript behaviour.
        chunk_resp = _chat_response('{"highlights":["分段重點A","分段重點B"]}')
        merge_resp = _chat_response(
            '{"title":"綜整會議","overview":"整場概述。","highlights":["綜整重點一","綜整重點二","綜整重點三"]}'
        )

        def _side_effect(*args, **kwargs):
            system = kwargs["json"]["messages"][0]["content"]
            if "各段落" in system or "綜整" in system:
                return merge_resp
            return chunk_resp

        long_text = "Speaker 1：討論性別平等的重要議題。\n\n" * 400  # well over threshold
        with (
            patch("transcript_bot.ollama_client.settings", self.settings),
            patch("transcript_bot.ollama_client.httpx.post", side_effect=_side_effect) as post,
        ):
            out = summarize_meeting_with_ollama(long_text, "預設會議")
        # At least one chunk call + one merge call.
        self.assertGreaterEqual(post.call_count, 2)
        self.assertEqual(out["title"], "綜整會議")
        self.assertNotIn("Speaker 1", out["highlights"][0])

    def test_merge_failure_degrades_to_chunk_highlights(self) -> None:
        # If the merge step itself returns a bad payload, the pipeline must still
        # return the per-chunk highlights rather than falling back to the
        # verbatim copy-transcript fallback.
        chunk_resp = _chat_response('{"highlights":["分段重點A","分段重點B"]}')
        bad_merge = _chat_response('{"transcript":"一大段…"}')

        def _side_effect(*args, **kwargs):
            system = kwargs["json"]["messages"][0]["content"]
            if "綜整" in system or "各段落" in system:
                return bad_merge
            return chunk_resp

        long_text = "Speaker 1：討論性別平等的重要議題。\n\n" * 400
        with (
            patch("transcript_bot.ollama_client.settings", self.settings),
            patch("transcript_bot.ollama_client.httpx.post", side_effect=_side_effect),
        ):
            out = summarize_meeting_with_ollama(long_text, "預設會議")
        self.assertEqual(out["highlights"], ["分段重點A", "分段重點B"])
        self.assertEqual(out["title"], "預設會議")

    def test_split_transcript_respects_chunk_size_and_overlap(self) -> None:
        blocks = ["甲" * 3000, "乙" * 3000, "丙" * 3000, "丁" * 3000]
        text = "\n\n".join(blocks)
        chunks = _split_transcript(text, chunk_size=6000, overlap=800)
        self.assertGreater(len(chunks), 1)
        for chunk in chunks:
            # Each chunk should not be wildly larger than chunk_size + overlap.
            self.assertLessEqual(len(chunk), 6000 + 800 + 50)
        # Every block must appear in some chunk.
        joined = "".join(chunks)
        for block in blocks:
            self.assertIn(block, joined)

    def test_split_transcript_bounds_one_very_long_speaker_turn(self) -> None:
        chunks = _split_transcript("Speaker 1：" + ("沒有標點" * 2000), chunk_size=6000, overlap=800)
        self.assertGreater(len(chunks), 1)
        self.assertTrue(all(len(chunk) <= 6800 for chunk in chunks))


if __name__ == "__main__":
    unittest.main()
