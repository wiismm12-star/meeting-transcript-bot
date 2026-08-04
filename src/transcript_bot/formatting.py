from __future__ import annotations

import re
from collections import OrderedDict

from openai import OpenAI
from opencc import OpenCC

from transcript_bot.config import settings
from transcript_bot.transcription import TranscriptSegment


_OPENCC = OpenCC("s2twp")
_FILLER_PATTERN = re.compile(
    r"(?<!\w)(?:呃+|嗯+|啊+|喔+|哦+|欸+|誒+|囈+|呃呃|嗯嗯|那個|這個)(?!\w)"
)


def normalize_speaker_labels(segments: list[TranscriptSegment]) -> list[TranscriptSegment]:
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

    return merge_adjacent_segments(normalized)


def merge_adjacent_segments(segments: list[TranscriptSegment]) -> list[TranscriptSegment]:
    if not segments:
        return []

    merged: list[TranscriptSegment] = [segments[0]]
    for segment in segments[1:]:
        previous = merged[-1]
        if previous.speaker == segment.speaker:
            previous.text = clean_text(f"{previous.text} {segment.text}")
            previous.end = segment.end
        else:
            merged.append(segment)
    return merged


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
    text = to_traditional(text)
    text = text.replace("\u3000", " ")
    text = re.sub(r"\s+", " ", text)
    text = _FILLER_PATTERN.sub("", text)
    text = re.sub(r"\s*([，。！？、；：])\s*", r"\1", text)
    text = re.sub(r"([，。！？、；：]){2,}", r"\1", text)
    text = re.sub(r"\b(\w+)(?:\s+\1\b)+", r"\1", text, flags=re.IGNORECASE)
    text = re.sub(r"\s+", " ", text)
    return text.strip(" \t\r\n，。！？、；：")


def to_traditional(text: str) -> str:
    return _OPENCC.convert(text)


def polish_transcript(raw_transcript: str) -> str:
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
