from __future__ import annotations

import re
import json
import unicodedata
from difflib import SequenceMatcher

import httpx

from transcript_bot.config import settings


class OllamaError(RuntimeError):
    """An actionable error raised when the local Ollama service is unavailable."""


_LABEL_PATTERN = re.compile(r"^(.{1,40}?)[：:]\s*(.+)$")
_WRAPPER_PATTERN = re.compile(r"^(?:#|---|以下|潤稿|整理後|說明|備註|如需)")
_SYSTEM_PROMPT = """
You are a conservative Traditional-Chinese transcript proofreader.
Correct only the supplied paragraph: add punctuation, remove unnatural word spacing,
remove filler words, and correct obvious typos. Every sentence MUST end with appropriate punctuation (。！？). Do not add facts, conclusions, examples,
or new sentences. Preserve the paragraph whenever possible. Trim only a clearly broken
prefix or suffix that cannot be made correct without guessing. Output only the revised paragraph,
with no title, explanation, Markdown, quotation, or speaker label.
If the supplied text contains garbled or invalid display characters that cannot be
repaired confidently, output exactly DROP.
""".strip()
_SUMMARY_SYSTEM_PROMPT = """
You are a careful Traditional-Chinese meeting summarizer. Use only facts explicitly
present in the supplied transcript. Return JSON only, with this exact shape:
{"title":"...","overview":"...","highlights":["...","..."]}
Write a concise title (max 26 Chinese characters), a 1-2 sentence overview, and 3-5
concise highlights. Do not copy speaker labels, do not invent decisions, names, dates,
or outcomes, and do not mention uncertain fragments as facts. Use Traditional Chinese.
""".strip()


def polish_with_ollama(raw_transcript: str) -> str:
    """Polish each speaker turn independently so labels and turn order cannot be rewritten."""
    output_lines: list[str] = []
    for raw_line in raw_transcript.splitlines():
        line = raw_line.strip()
        if not line:
            if output_lines and output_lines[-1]:
                output_lines.append("")
            continue

        label_match = _LABEL_PATTERN.match(line)
        if label_match:
            label, content = label_match.groups()
            revised = _polish_paragraph(content)
            if revised:
                output_lines.append(f"{label}：{revised}")
        else:
            revised = _polish_paragraph(line)
            if revised:
                output_lines.append(revised)

    return "\n".join(output_lines).strip()


def summarize_meeting_with_ollama(raw_transcript: str, fallback_title: str = "") -> dict[str, object]:
    """Return a structured, evidence-only summary from the local Ollama model."""
    try:
        response = httpx.post(
            f"{settings.ollama_base_url.rstrip('/')}/api/chat",
            json={
                "model": settings.ollama_text_model,
                "messages": [
                    {"role": "system", "content": _SUMMARY_SYSTEM_PROMPT},
                    {"role": "user", "content": raw_transcript},
                ],
                "stream": False,
                "think": False,
                "format": "json",
                "options": {"temperature": 0.1},
            },
            timeout=180.0,
        )
    except httpx.RequestError as exc:
        raise OllamaError("找不到本機 Ollama 服務，無法產生會議摘要。") from exc

    if response.status_code != 200:
        raise OllamaError("本機 Ollama 無法產生會議摘要。")

    output = str(response.json().get("message", {}).get("content", "")).strip()
    output = re.sub(r"^```(?:json)?\s*|\s*```$", "", output, flags=re.IGNORECASE)
    try:
        payload = json.loads(output)
    except json.JSONDecodeError as exc:
        raise OllamaError("本機 Ollama 回傳的摘要格式無法讀取。") from exc

    title = str(payload.get("title", "")).strip()[:80]
    overview = str(payload.get("overview", "")).strip()
    raw_highlights = payload.get("highlights", [])
    highlights = [str(item).strip() for item in raw_highlights if str(item).strip()] if isinstance(raw_highlights, list) else []
    if not overview or not highlights:
        raise OllamaError("本機 Ollama 未產生足夠的摘要內容。")
    return {"title": title or fallback_title or "會議重點摘要", "overview": overview, "highlights": highlights[:5]}


def _polish_paragraph(source: str) -> str:
    try:
        response = httpx.post(
            f"{settings.ollama_base_url.rstrip('/')}/api/chat",
            json={
                "model": settings.ollama_text_model,
                "messages": [
                    {"role": "system", "content": _SYSTEM_PROMPT},
                    {"role": "user", "content": source},
                ],
                "stream": False,
                "think": False,
                "options": {"temperature": 0.0},
            },
            timeout=180.0,
        )
    except httpx.RequestError as exc:
        raise OllamaError(
            "找不到本機 Ollama 服務。請確認 Ollama 已安裝並正在執行，再重新傳送音檔。"
        ) from exc

    if response.status_code != 200:
        detail = response.text.lower()
        if "not found" in detail or "model" in detail:
            raise OllamaError(
                f"找不到本機潤稿模型 {settings.ollama_text_model}。"
                f"請執行：ollama pull {settings.ollama_text_model}"
            )
        raise OllamaError("本機 Ollama 潤稿失敗，請稍後再試。")

    output = str(response.json().get("message", {}).get("content", "")).strip()
    if output.upper() == "DROP":
        return _trim_unreliable_fragment(source)
    cleaned = _clean_model_output(output)
    if not cleaned or _content_similarity(source, cleaned) < 0.35 or _has_invalid_display(cleaned):
        cleaned = source
    return _trim_unreliable_fragment(cleaned)


def _clean_model_output(output: str) -> str:
    lines = [line.strip(" -\t") for line in output.splitlines() if line.strip()]
    content_lines = [line for line in lines if not _WRAPPER_PATTERN.match(line)]
    return _repair_display_text(" ".join(content_lines).strip())


def _content_similarity(source: str, output: str) -> float:
    source_text = re.sub(r"[\s，。！？、；：]", "", source)
    output_text = re.sub(r"[\s，。！？、；：]", "", output)
    return SequenceMatcher(None, source_text, output_text).ratio()


def _trim_unreliable_fragment(text: str) -> str:
    """Keep a speaker turn while removing only unmistakable ASR debris at its edges."""
    trimmed = text.strip()
    trimmed = re.sub(r"^[的得地]\s*(?=[\u4e00-\u9fff])", "", trimmed)
    trimmed = re.sub(r"[，,、；]\s*[A-Za-z]\s*(?=[\u4e00-\u9fff]).*$", "", trimmed)
    trimmed = re.sub(r"(?<=[\u4e00-\u9fff])\s*[A-Za-z]\s*(?=[\u4e00-\u9fff]).*$", "", trimmed)
    return trimmed.strip(" ，、；")


def _repair_display_text(text: str) -> str:
    """Repair the common UTF-8-as-Latin-1 mojibake pattern when it is unambiguous."""
    normalized = unicodedata.normalize("NFC", text)
    marker_pattern = re.compile(r"[ÃÂâïåæé]")
    if not marker_pattern.search(normalized):
        return normalized

    try:
        candidate = normalized.encode("latin-1").decode("utf-8")
    except UnicodeError:
        return normalized

    if len(marker_pattern.findall(candidate)) < len(marker_pattern.findall(normalized)):
        return candidate
    return normalized


def _has_invalid_display(text: str) -> bool:
    if "\ufffd" in text:
        return True
    return any(unicodedata.category(char) in {"Cc", "Cs"} for char in text)
