from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock, patch
import unittest

import httpx
from pathlib import Path

from transcript_bot.ollama_client import (
    OllamaError,
    _repair_display_text,
    apply_glossary,
    polish_with_ollama,
)


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


if __name__ == "__main__":
    unittest.main()
