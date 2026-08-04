from __future__ import annotations

from pathlib import Path

from docx import Document


def write_text(path: Path, text: str) -> None:
    path.write_text(text, encoding="utf-8")


def write_docx(path: Path, title: str, text: str) -> None:
    document = Document()
    document.add_heading(title, level=1)

    for block in text.split("\n\n"):
        paragraph = block.strip()
        if paragraph:
            document.add_paragraph(paragraph)

    document.save(path)
