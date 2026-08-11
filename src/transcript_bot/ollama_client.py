from __future__ import annotations

import re
import json
import unicodedata
from difflib import SequenceMatcher

import httpx

from transcript_bot.config import settings
from transcript_bot.retry import request_with_retry


class OllamaError(RuntimeError):
    """An actionable error raised when the local Ollama service is unavailable."""


class OllamaSchemaError(OllamaError):
    """The model returned a JSON payload that ignored the requested schema.

    Unlike a network/timeout failure, a schema-ignore blob (e.g. echoing the
    transcript under a ``transcript`` key) means the summarizer produced no
    usable content. Callers must NOT silently degrade this into a verbatim
    copy of the input — they should surface the failure instead.
    """


_LABEL_PATTERN = re.compile(r"^(.{1,40}?)[：:]\s*(.+)$")
_WRAPPER_PATTERN = re.compile(r"^(?:#|---|以下|潤稿|整理後|說明|備註|如需)")
_GLOSSARY_CACHE: list[tuple[str, str]] | None = None
_GLOSSARY_MTIME: float | None = None


def _load_glossary() -> list[tuple[str, str]]:
    """Load the Taiwan localization glossary as (wrong, correct) pairs.

    Format: one mapping per line, ``wrong => correct`` or ``wrong->correct``.
    Lines starting with ``#`` or blank are ignored. Results are cached and
    refreshed when the file mtime changes, so editing the glossary takes effect
    on the next polish without a restart. Returns ``[]`` when no glossary is
    configured or the file is absent, so polishing stays a safe no-op.
    """
    global _GLOSSARY_CACHE, _GLOSSARY_MTIME
    path = getattr(settings, "glossary_file", None)
    if path is None:
        return []
    try:
        mtime = path.stat().st_mtime
    except OSError:
        return []
    if _GLOSSARY_CACHE is not None and _GLOSSARY_MTIME == mtime:
        return _GLOSSARY_CACHE
    pairs: list[tuple[str, str]] = []
    if not path.is_file():
        _GLOSSARY_CACHE, _GLOSSARY_MTIME = [], mtime
        return pairs
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        for sep in ("=>", "->", "→"):
            if sep in line:
                wrong, correct = line.split(sep, 1)
                wrong, correct = wrong.strip(), correct.strip()
                if wrong and correct:
                    pairs.append((wrong, correct))
                break
    _GLOSSARY_CACHE, _GLOSSARY_MTIME = pairs, mtime
    return pairs


def apply_glossary(text: str) -> str:
    """Deterministically rewrite known ASR misreadings / brand terms in ``text``."""
    for wrong, correct in _load_glossary():
        if wrong in text:
            text = text.replace(wrong, correct)
    return text
_SYSTEM_PROMPT = """
You are a conservative Traditional-Chinese transcript proofreader for Taiwan (台灣) content.
Correct only the supplied paragraph: add punctuation, remove unnatural word spacing,
remove filler words, and correct obvious typos. Every sentence MUST end with appropriate punctuation (。！？). Do not add facts, conclusions, examples,
or new sentences. Preserve the paragraph whenever possible. Trim only a clearly broken
prefix or suffix that cannot be made correct without guessing. Output only the revised paragraph,
with no title, explanation, Markdown, quotation, or speaker label.

Apply Taiwan (台灣) localization conventions:
- This is Taiwan-context speech. Use Taiwan customary written forms: 捷運 station/line names
  (e.g. 忠孝復興站, 文湖線, 板南線), 公車 not 巴士, 轉乘 not 轉車, 粉絲/留言 kept as-is (do not
  convert to China usage like 评论).
- Spoken dates/numbers → written form: 3月31號 → 3月31日; keep 百大, 年度 etc. as natural.
- Proper nouns, brand names, and event names (e.g. KKBOX, 風雲榜) MUST be preserved exactly as
  the canonical name even if the audio sounds slightly different — do NOT transliterate or
  "fix" them into a different word. A deterministic glossary pass runs after you, so never
  guess a brand name.
- If the supplied text contains garbled or invalid display characters that cannot be
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

# Map step: summarize ONE chunk of a longer transcript. Keeps the model call small
# enough for an 8B model to follow the JSON contract reliably.
_CHUNK_SUMMARY_SYSTEM_PROMPT = """
你是一名嚴謹的繁體中文會議摘要助手。請僅根據提供的「逐字稿片段」，整理其中確實出現的討論重點。
只回傳 JSON，格式嚴格為：{"highlights":["...","..."]}
產出 2-4 條簡潔、客觀的討論重點（每條 1-2 句，使用繁體中文）。
不得抄寫講者標籤，不得杜撰決議、姓名、日期或結論，不得將不確定的片段當作事實。
""".strip()

# Reduce step: collapse the per-chunk highlights into the final summary object.
_MERGE_SUMMARY_SYSTEM_PROMPT = """
你是一名嚴謹的繁體中文會議摘要助手。下面是一場會議「各段落」的討論重點清單（已按順序整理）。
請綜整為最終會議摘要，只回傳 JSON，格式嚴格為：{"title":"...","overview":"...","highlights":["...","..."]}
標題精簡（最多 26 個中文字）；overview 用 1-2 句概括整場會議；highlights 產出 3-5 條去重、不重疊的關鍵重點（使用繁體中文）。
只能使用清單中確實出現的內容，不得杜撰決議、姓名、日期或結論。
""".strip()

# Above this many characters, summarize via map-reduce (chunked) instead of one
# giant prompt. Empirically qwen3:8b stops honouring the JSON schema past ~8K-10K
# Chinese characters and returns a verbatim ``{"transcript": "..."}`` blob.
_SUMMARY_CHUNK_THRESHOLD = 8000
_SUMMARY_CHUNK_SIZE = 6000
_SUMMARY_CHUNK_OVERLAP = 800

def polish_with_ollama(raw_transcript: str) -> str:
    """Polish each speaker turn independently so labels and turn order cannot be rewritten.

    A deterministic glossary pass runs first (so brand/proper nouns survive the
    model unchanged) and again after (to correct any residual ASR misreading the
    model left in), guaranteeing terms like KKBUS→KKBOX are fixed regardless of
    model behaviour.
    """
    raw_transcript = apply_glossary(raw_transcript)
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

    return apply_glossary("\n".join(output_lines).strip())


def summarize_meeting_with_ollama(raw_transcript: str, fallback_title: str = "") -> dict[str, object]:
    """Return a structured, evidence-only summary from the local Ollama model.

    Short transcripts are summarized in a single call. Long transcripts (above
    ``_SUMMARY_CHUNK_THRESHOLD`` characters) are summarized via map-reduce: each
    chunk is compressed into a few highlights, then those are merged into the
    final title/overview/highlights. This keeps every model call small enough for
    an 8B local model to honour the JSON schema reliably.
    """
    transcript = (raw_transcript or "").strip()
    if len(transcript) <= _SUMMARY_CHUNK_THRESHOLD:
        return _summarize_single(transcript, fallback_title)

    chunks = _split_transcript(transcript, _SUMMARY_CHUNK_SIZE, _SUMMARY_CHUNK_OVERLAP)
    partial_highlights: list[str] = []
    for chunk in chunks:
        try:
            chunk_payload = _ollama_chat_json(
                _CHUNK_SUMMARY_SYSTEM_PROMPT,
                chunk,
                timeout=120.0,
            )
        except OllamaSchemaError:
            # The model ignored the schema (e.g. echoed the transcript). That is
            # a real failure, not a transient glitch — never degrade it into a
            # verbatim copy of the input. Surface it so the caller falls back
            # honestly instead of showing a fake "summary".
            raise
        except OllamaError:
            # A transient failure (network/timeout/empty) on one chunk must not
            # sink the whole meeting. Fall back to a verbatim-trim of the chunk
            # so the reduce step still has material.
            chunk_payload = {"highlights": [_shorten_for_merge(chunk)]}
        for item in chunk_payload.get("highlights", []) or []:
            text = str(item).strip()
            if text and text not in partial_highlights:
                partial_highlights.append(text)
    if not partial_highlights:
        raise OllamaError("本機 Ollama 未產生足夠的摘要內容。")

    try:
        merged = _ollama_chat_json(
            _MERGE_SUMMARY_SYSTEM_PROMPT,
            "會議各段落討論重點如下：\n" + "\n".join(f"{i}. {h}" for i, h in enumerate(partial_highlights, 1)),
            timeout=180.0,
        )
    except OllamaError:
        # Merge step failed — degrade gracefully to the raw chunk highlights so
        # the meeting still gets a real (if un-polished) summary, never the
        # verbatim copy-transcript fallback.
        return {"title": fallback_title or "會議重點摘要", "overview": "以下為根據本次逐字稿整理的重點。", "highlights": partial_highlights[:5]}

    title = str(merged.get("title", "")).strip()[:80]
    overview = str(merged.get("overview", "")).strip()
    raw_highlights = merged.get("highlights", []) or partial_highlights
    highlights = [str(item).strip() for item in raw_highlights if str(item).strip()]
    if not overview or not highlights:
        # Reduce step under-delivered — degrade gracefully to the raw chunk list.
        overview = overview or "以下為根據本次逐字稿整理的重點。"
        highlights = partial_highlights
    return {"title": title or fallback_title or "會議重點摘要", "overview": overview, "highlights": highlights[:5]}


def _summarize_single(transcript: str, fallback_title: str) -> dict[str, object]:
    """Single-call summary for short transcripts."""
    payload = _ollama_chat_json(
        _SUMMARY_SYSTEM_PROMPT,
        transcript,
        timeout=180.0,
    )
    title = str(payload.get("title", "")).strip()[:80]
    overview = str(payload.get("overview", "")).strip()
    raw_highlights = payload.get("highlights", [])
    highlights = [str(item).strip() for item in raw_highlights if str(item).strip()] if isinstance(raw_highlights, list) else []
    if not overview or not highlights:
        raise OllamaError("本機 Ollama 未產生足夠的摘要內容。")
    return {"title": title or fallback_title or "會議重點摘要", "overview": overview, "highlights": highlights[:5]}


def _ollama_chat_json(system_prompt: str, user_content: str, timeout: float) -> dict:
    """Post a chat request to Ollama and parse the JSON response.

    Raises :class:`OllamaError` on network failure, non-200, unparseable JSON,
    or a ``{"transcript": ...}``-style blob where the model ignored the schema.
    """
    try:
        response = request_with_retry(lambda: httpx.post(
            f"{settings.ollama_base_url.rstrip('/')}/api/chat",
            json={
                "model": settings.ollama_text_model,
                "messages": [
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_content},
                ],
                "stream": False,
                "think": False,
                "format": "json",
                "options": {"temperature": 0.1},
            },
            timeout=timeout,
        ), action="Ollama 會議摘要")
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

    if not isinstance(payload, dict):
        raise OllamaError("本機 Ollama 回傳的摘要格式無法讀取。")
    # Guard against the schema-ignore blob: qwen3 sometimes echoes the transcript
    # under a "transcript" key and omits the requested fields. This is a schema
    # failure, not a transient error, so callers must surface it (never degrade
    # to a verbatim copy of the input).
    if "transcript" in payload and not any(k in payload for k in ("title", "overview", "highlights")):
        raise OllamaSchemaError("本機 Ollama 未依格式回傳摘要內容。")
    return payload


def _split_transcript(text: str, chunk_size: int, overlap: int) -> list[str]:
    """Split a transcript into overlapping chunks of ~``chunk_size`` characters.

    Splits on blank lines (speaker-turn boundaries) when possible so a chunk does
    not cut a turn mid-sentence; falls back to a hard character cut otherwise.
    """
    if len(text) <= chunk_size:
        return [text]
    # Prefer paragraph (blank-line) boundaries.
    blocks = [b for b in re.split(r"\n\s*\n", text) if b.strip()]
    if not blocks:
        blocks = [text]
    # A single long speaker turn has no blank-line boundary.  Split it at a
    # sentence mark when possible so neither summaries nor action extraction
    # can accidentally send an unbounded request to the local model.
    bounded_blocks: list[str] = []
    for block in blocks:
        remaining = block.strip()
        while len(remaining) > chunk_size:
            split_at = max(remaining.rfind(mark, 0, chunk_size) for mark in "。！？；")
            split_at = split_at + 1 if split_at >= chunk_size // 2 else chunk_size
            bounded_blocks.append(remaining[:split_at].strip())
            remaining = remaining[split_at:].lstrip()
        if remaining:
            bounded_blocks.append(remaining)
    blocks = bounded_blocks
    chunks: list[str] = []
    current: list[str] = []
    current_len = 0
    for block in blocks:
        block_len = len(block)
        if current and current_len + block_len + 2 > chunk_size:
            previous = "\n\n".join(current).strip()
            chunks.append(previous)
            # Carry exactly the requested tail, rather than an entire speaker
            # turn that might itself be larger than the overlap budget.
            tail = previous[-overlap:].lstrip() if overlap else ""
            current = [tail] if tail else []
            current_len = len(tail)
        current.append(block)
        current_len += block_len + 2
    if current:
        chunks.append("\n\n".join(current).strip())
    return [c for c in chunks if c]


def _shorten_for_merge(text: str, limit: int = 120) -> str:
    """Trim a verbatim chunk to a single readable highlight when a chunk call fails."""
    text = text.strip()
    if len(text) <= limit:
        return text
    end = max(text.rfind(m, 0, limit) for m in "。！？；")
    return text[: end + 1 if end > 20 else limit].rstrip("，、；") + "…"


def _polish_paragraph(source: str) -> str:
    try:
        response = request_with_retry(lambda: httpx.post(
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
        ), action="Ollama 潤稿")
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
    return apply_glossary(_trim_unreliable_fragment(cleaned))


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
