from __future__ import annotations

import re
from collections import OrderedDict
from dataclasses import dataclass

from openai import OpenAI
from opencc import OpenCC

from transcript_bot.config import settings
from transcript_bot.ollama_client import polish_with_ollama
from transcript_bot.transcription import TranscriptSegment


_OPENCC = OpenCC("s2twp")
_FILLER_PATTERN = re.compile(
    r"(?<!\w)(?:呃+|嗯+|啊+|喔+|哦+|欸+|誒+|囈+|呃呃|嗯嗯|那個|這個)(?!\w)"
)
_LEADING_FILLER_PATTERN = re.compile(r"(?<=[:：])\s*(?:呃+|嗯+|啊+|喔+|哦+|欸+|誒+)\s*[，、]?\s*")
_CJK_TOKEN_SPACE_PATTERN = re.compile(r"(?<=[\u4e00-\u9fff0-9])\s+(?=[\u4e00-\u9fff0-9])")
_CONFIRMED_ASR_CORRECTIONS = {
    "首扶梯": "手扶梯",
    "中校復興": "忠孝復興",
    "中正復興": "忠孝復興",
}
_ACTION_KEYWORDS = ("決定", "決議", "確認", "同意", "負責", "完成", "安排", "提交", "回覆", "跟進", "下一步")


@dataclass(frozen=True)
class MeetingSummary:
    title: str
    overview: str
    highlights: list[str]


def normalize_speaker_labels(segments: list[TranscriptSegment]) -> list[TranscriptSegment]:
    """Give source speaker ids stable display labels without losing turn boundaries.

    Providers such as Deepgram return short utterances even when one person
    speaks continuously.  Those timestamps are what the web editor uses for
    readable rows and audio seeking, so adjacent same-speaker utterances must
    remain separate.
    """
    speaker_map: OrderedDict[str, str] = OrderedDict()
    normalized: list[TranscriptSegment] = []

    for segment in segments:
        if segment.speaker not in speaker_map:
            speaker_map[segment.speaker] = f"Speaker {len(speaker_map) + 1}"
        normalized.append(
            TranscriptSegment(
                speaker=speaker_map[segment.speaker],
                start=segment.start,
                end=segment.end,
                text=clean_text(segment.text),
            )
        )

    return normalized


def apply_speaker_aliases(text: str, aliases: dict[str, str]) -> str:
    output = text
    for source, target in aliases.items():
        output = output.replace(f"{source}：", f"{target}：")
        output = output.replace(f"{source}:", f"{target}：")
    return output


def render_plain_transcript(segments: list[TranscriptSegment]) -> str:
    lines = []
    for segment in segments:
        text = clean_text(segment.text)
        if text:
            lines.append(f"{segment.speaker}：{text}")
    return polish_local_transcript("\n\n".join(lines))


def render_raw_transcript(segments: list[TranscriptSegment]) -> str:
    """Render stored speaker turns without LLM polishing or fragment trimming."""
    return "\n\n".join(
        f"{segment.speaker}：{segment.text.strip()}"
        for segment in segments
        if segment.text.strip()
    )


def render_meeting_minutes(cleaned_transcript: str) -> str:
    """Create a conservative minutes view without inferring decisions or action items."""
    entries = [line.strip() for line in cleaned_transcript.splitlines() if line.strip()]
    if not entries:
        return "# 會議紀錄\n\n目前沒有可整理的發言內容。"

    bullets = "\n".join(f"- {line}" for line in entries)
    return f"# 會議紀錄\n\n## 發言摘要\n{bullets}"


def build_fallback_meeting_summary(transcript: str, meeting_title: str = "") -> MeetingSummary:
    """Provide a readable local fallback when the optional summarizer is unavailable."""
    highlights: list[str] = []
    seen: set[str] = set()
    for raw_line in transcript.splitlines():
        match = re.match(r"^.+?[：:]\s*(.+)$", raw_line.strip())
        content = (match.group(1) if match else raw_line).strip()
        if not content or content in seen:
            continue
        seen.add(content)
        highlights.append(_shorten_summary_item(content))
        if len(highlights) == 5:
            break

    if not highlights:
        return MeetingSummary("會議重點摘要", "目前沒有足夠的逐字稿內容可供整理。", [])

    title = meeting_title.strip() if meeting_title.strip() and meeting_title.strip() != "未命名會議" else "會議重點摘要"
    return MeetingSummary(title, "以下為根據本次逐字稿整理的重點。", highlights)


def _shorten_summary_item(text: str, limit: int = 110) -> str:
    if len(text) <= limit:
        return text
    sentence_end = max(text.rfind(mark, 0, limit) for mark in "。！？；")
    return text[: sentence_end + 1 if sentence_end > 20 else limit].rstrip("，、；") + "…"


def render_action_summary(cleaned_transcript: str) -> str:
    """Extract only explicit decision or action language; never infer a new conclusion."""
    items: list[str] = []
    seen: set[str] = set()

    for line in cleaned_transcript.splitlines():
        line = line.strip()
        if not line:
            continue
        match = re.match(r"^(.+?)[：:]\s*(.+)$", line)
        if not match:
            continue
        speaker, content = match.groups()
        for sentence in re.split(r"(?<=[。！？；])", content):
            sentence = sentence.strip()
            if sentence and any(keyword in sentence for keyword in _ACTION_KEYWORDS):
                item = f"{speaker}：{sentence}"
                if item not in seen:
                    seen.add(item)
                    items.append(item)

    if not items:
        return "# 決議事項摘要\n\n未偵測到原文中明確的決議或行動事項。"
    return "# 決議事項摘要\n\n## 原文明確提及的事項\n" + "\n".join(f"- {item}" for item in items)


def polish_local_transcript(text: str) -> str:
    text = to_traditional(text)
    polished_lines: list[str] = []
    previous_line = ""

    for raw_line in text.splitlines():
        line = clean_text(raw_line)
        if not line:
            continue
        line = _normalize_speaker_colon(line)
        if line == previous_line:
            continue
        polished_lines.append(line)
        previous_line = line

    return "\n\n".join(polished_lines).strip()


def clean_text(text: str) -> str:
    text = _FILLER_PATTERN.sub("", text)
    text = _LEADING_FILLER_PATTERN.sub("", text)
    text = to_traditional(text)
    for source, replacement in _CONFIRMED_ASR_CORRECTIONS.items():
        text = text.replace(source, replacement)
    text = re.sub(r"^(Speaker\s+\d+)(?:\s*[，:：])+", r"\1：", text, flags=re.IGNORECASE)
    text = _CJK_TOKEN_SPACE_PATTERN.sub("", text)
    text = text.replace("\u3000", " ")
    text = re.sub(r"\s+", " ", text)
    text = _FILLER_PATTERN.sub("", text)
    # Removing a leading filler can expose a comma after a speaker label.
    # Normalize once more so "Speaker 1: 呃，內容" becomes "Speaker 1：內容".
    text = re.sub(r"^(Speaker\s+\d+)(?:\s*[，:：])+", r"\1：", text, flags=re.IGNORECASE)
    text = re.sub(r"\s*([，。！？、；：])\s*", r"\1", text)
    text = re.sub(r"([，。！？、；：]){2,}", r"\1", text)
    text = re.sub(r"\b(\w+)(?:\s+\1\b)+", r"\1", text, flags=re.IGNORECASE)
    text = re.sub(r"\s+", " ", text)
    return text.strip(" \t\r\n，、；：")


def to_traditional(text: str) -> str:
    return _OPENCC.convert(text)


def polish_transcript(raw_transcript: str) -> str:
    if settings.polish_provider == "ollama":
        return polish_local_transcript(polish_with_ollama(raw_transcript))
    return polish_with_openai(raw_transcript)


def polish_with_openai(raw_transcript: str) -> str:
    client = OpenAI(api_key=settings.openai_api_key)
    prompt = f"""
請將以下會議逐字稿整理成正式繁體中文會議文字稿。

規則：
1. 保留每一段主講人標籤。
2. 不新增原文沒有的決議或結論。
3. 修正明顯錯字、簡體字、標點與斷句。
4. 移除明顯語助詞、重複字詞、空白與多餘文字。
5. 不改變原意。

逐字稿：
{raw_transcript}
""".strip()

    response = client.responses.create(
        model=settings.openai_text_model,
        input=prompt,
    )
    return polish_local_transcript(response.output_text)


def parse_alias_message(message: str) -> dict[str, str]:
    aliases: dict[str, str] = {}
    for line in message.splitlines():
        if "=" not in line:
            continue
        source, target = line.split("=", 1)
        source = source.strip()
        target = target.strip()
        if source and target:
            aliases[source] = target
    return aliases


def _normalize_speaker_colon(line: str) -> str:
    match = re.match(r"^(Speaker\s+\d+)\s*[:：]\s*(.+)$", line, flags=re.IGNORECASE)
    if match:
        return f"{match.group(1)}：{match.group(2).strip()}"
    return line


_SENTENCE_CHUNK = re.compile(r".+?(?:[。！？；\n]+|$)", re.DOTALL)
_DISPLAY_SEGMENT_MAX_CHARS = 110


def split_segments_by_sentences(segments: list[TranscriptSegment]) -> list[TranscriptSegment]:
    """Break long turns into readable 1–2 line chunks without dropping punctuation."""
    result: list[TranscriptSegment] = []
    for seg in segments:
        text = seg.text.strip()
        if len(text) <= _DISPLAY_SEGMENT_MAX_CHARS:
            result.append(seg)
            continue

        parts = _split_display_text(text)

        # Assign time by relative text length so every displayed row still
        # seeks to its own portion of the source audio.
        seg_start = seg.start or 0.0
        seg_end = seg.end or seg_start + 1.0
        duration = seg_end - seg_start
        total_chars = max(sum(len(part) for part in parts), 1)
        consumed_chars = 0

        for part in parts:
            sub_start = seg_start + (duration * consumed_chars / total_chars)
            consumed_chars += len(part)
            sub_end = seg_start + (duration * consumed_chars / total_chars)
            result.append(
                TranscriptSegment(
                    speaker=seg.speaker,
                    start=sub_start,
                    end=sub_end,
                    text=part,
                )
            )

    return result


def _split_display_text(text: str) -> list[str]:
    """Prefer sentence/clause ends, with a safe hard limit for unpunctuated ASR."""
    matches = list(_SENTENCE_CHUNK.finditer(text))
    sentence_parts = [match.group().strip() for match in matches if match.group().strip()]
    if not sentence_parts:
        return [text]

    chunks: list[str] = []
    current = ""
    for sentence in sentence_parts:
        if current and len(current) + len(sentence) > _DISPLAY_SEGMENT_MAX_CHARS:
            chunks.extend(_split_long_display_piece(current))
            current = ""
        if len(sentence) > _DISPLAY_SEGMENT_MAX_CHARS:
            chunks.extend(_split_long_display_piece(sentence))
        else:
            current += sentence
    if current:
        chunks.extend(_split_long_display_piece(current))
    return [chunk for chunk in chunks if chunk]


def _split_long_display_piece(text: str) -> list[str]:
    chunks: list[str] = []
    remaining = text.strip()
    while len(remaining) > _DISPLAY_SEGMENT_MAX_CHARS:
        window = remaining[:_DISPLAY_SEGMENT_MAX_CHARS]
        break_at = max(window.rfind(mark) + 1 for mark in "，、 ")
        # Do not create a very short first fragment just to use a comma.
        if break_at < _DISPLAY_SEGMENT_MAX_CHARS // 2:
            break_at = _DISPLAY_SEGMENT_MAX_CHARS
        chunks.append(remaining[:break_at].strip())
        remaining = remaining[break_at:].strip()
    if remaining:
        chunks.append(remaining)
    return chunks
