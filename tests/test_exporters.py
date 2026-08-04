from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from docx import Document

from transcript_bot.exporters import write_docx


class DocxExporterTests(unittest.TestCase):
    def test_docx_includes_meeting_metadata_speakers_and_transcript(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "meeting.docx"
            write_docx(
                path,
                "會議逐字稿",
                "主持人：大家好，今天確認時程。\n\n來賓：我會在週五前回覆。",
                meeting_id="meeting-123",
                meeting_date="2026-08-04 10:30:00",
                speakers=["主持人", "來賓"],
            )

            document = Document(path)
            paragraphs = [paragraph.text for paragraph in document.paragraphs]

        self.assertTrue(path.name == "meeting.docx")
        self.assertIn("會議逐字稿", paragraphs)
        self.assertIn("日期：2026-08-04　｜　會議 ID：meeting-123", paragraphs)
        self.assertIn("主講人：主持人、來賓", paragraphs)
        self.assertIn("主持人：大家好，今天確認時程。", paragraphs)
        self.assertIn("來賓：我會在週五前回覆。", paragraphs)
