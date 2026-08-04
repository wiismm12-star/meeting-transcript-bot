from __future__ import annotations

import asyncio
from dataclasses import dataclass
from pathlib import Path

from openai import RateLimitError
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ChatAction
from telegram.ext import (
    Application,
    ApplicationBuilder,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

from transcript_bot.audio import AudioProcessingError, normalize_audio
from transcript_bot.config import settings
from transcript_bot.database import (
    MeetingRecord,
    create_meeting,
    get_latest_meeting_id,
    get_meeting_export,
    get_meeting_speaker_samples,
    get_speaker_aliases,
    save_transcript_segments,
    update_meeting_transcript_text,
    upsert_speaker_aliases,
)
from transcript_bot.deepgram import DeepgramError
from transcript_bot.exporters import write_docx, write_text
from transcript_bot.formatting import (
    apply_speaker_aliases,
    normalize_speaker_labels,
    parse_alias_message,
    polish_local_transcript,
    polish_transcript,
    render_plain_transcript,
)
from transcript_bot.storage import create_job_paths
from transcript_bot.transcription import transcribe_with_diarization


@dataclass(frozen=True)
class PendingExport:
    meeting_id: str
    export_type: str


PENDING_EXPORTS: dict[int, PendingExport] = {}


def build_application(token: str) -> Application:
    app = ApplicationBuilder().token(token).build()
    app.add_handler(CallbackQueryHandler(handle_export_choice, pattern=r"^export:"))
    app.add_handler(CommandHandler("latest", handle_latest_command))
    app.add_handler(MessageHandler(filters.VOICE | filters.AUDIO | filters.Document.AUDIO, handle_audio))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))
    return app


async def handle_latest_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.effective_user or not update.message:
        return

    user_id = update.effective_user.id
    meeting_id = get_latest_meeting_id(settings.data_dir, user_id)
    if not meeting_id:
        await update.message.reply_text("目前找不到最近一次會議，請先上傳一段語音或音檔。")
        return

    meeting = get_meeting_export(settings.data_dir, meeting_id, user_id)
    if not meeting:
        await update.message.reply_text("找不到最近一次會議，或你沒有權限查看這筆會議。")
        return

    aliases = get_speaker_aliases(settings.data_dir, meeting_id, user_id)
    alias_text = _speaker_alias_summary(aliases)
    preview_text = (
        _preview_text(meeting.transcript_text)
        if meeting.transcript_text.strip()
        else "這筆會議目前沒有逐字稿內容。"
    )
    await update.message.reply_text(
        f"最近一次會議 ID：{meeting_id}\n\n"
        f"{preview_text}\n\n"
        f"{alias_text}\n\n"
        "你也可以重新選擇要輸出的檔案：",
        reply_markup=_export_keyboard(meeting_id),
    )


async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.effective_user or not update.message or not update.message.text:
        return

    user_id = update.effective_user.id
    text = update.message.text.strip()
    aliases = parse_alias_message(update.message.text)
    pending_export = PENDING_EXPORTS.get(user_id)

    if pending_export:
        if aliases:
            upsert_speaker_aliases(settings.data_dir, pending_export.meeting_id, user_id, aliases)
        elif text not in {"直接輸出", "略過", "skip", "Skip", "SKIP"}:
            await update.message.reply_text(
                "請用 `Speaker 1 = 主持人` 這種格式填寫，或回覆 `直接輸出` 保留原本的 Speaker 標籤。"
            )
            return

        PENDING_EXPORTS.pop(user_id, None)
        await _export_meeting_documents(
            update.message,
            context,
            pending_export.meeting_id,
            pending_export.export_type,
            user_id,
        )
        return

    if not aliases:
        await update.message.reply_text("請傳語音訊息、音檔，或用 `Speaker 1 = 主持人` 這種格式指定主講人名稱。")
        return

    meeting_id = get_latest_meeting_id(settings.data_dir, user_id)
    if not meeting_id:
        await update.message.reply_text("目前找不到可套用的會議，請先上傳一段語音或音檔。")
        return

    upsert_speaker_aliases(settings.data_dir, meeting_id, user_id, aliases)
    saved_aliases = get_speaker_aliases(settings.data_dir, meeting_id, user_id)
    rendered = "\n".join(f"{source} -> {target}" for source, target in saved_aliases.items())
    await update.message.reply_text(f"已記住會議 {meeting_id} 的主講人對應：\n{rendered}")


async def handle_audio(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message:
        return

    await update.message.reply_text("已收到音訊，正在處理逐字稿。")
    asyncio.create_task(process_audio(update, context))


async def process_audio(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.message
    user = update.effective_user
    if not message or not user:
        return

    try:
        media, suffix, file_size = _extract_audio_meta(update)
        if file_size and file_size > settings.max_audio_bytes:
            await message.reply_text(f"音檔超過 {settings.max_audio_mb} MB，請先壓縮或切段後再上傳。")
            return

        paths = create_job_paths(settings.data_dir, suffix)
        create_meeting(
            settings.data_dir,
            MeetingRecord(
                id=paths.job_id,
                user_id=user.id,
                source_platform="telegram",
                audio_file_path=str(paths.input_audio),
                normalized_audio_path=str(paths.normalized_audio),
                transcript_txt_path=str(paths.transcript_txt),
                transcript_docx_path=str(paths.transcript_docx),
            ),
        )
        await context.bot.send_chat_action(chat_id=message.chat_id, action=ChatAction.UPLOAD_DOCUMENT)

        telegram_file = await context.bot.get_file(media.file_id)
        await telegram_file.download_to_drive(custom_path=paths.input_audio)

        normalize_audio(paths.input_audio, paths.normalized_audio)
        segments = transcribe_with_diarization(paths.normalized_audio)
        normalized_segments = normalize_speaker_labels(segments)
        save_transcript_segments(settings.data_dir, paths.job_id, normalized_segments)

        raw_text = render_plain_transcript(normalized_segments)
        polished_text = polish_transcript(raw_text) if settings.enable_polish else polish_local_transcript(raw_text)
        polished_text = polish_local_transcript(polished_text)
        update_meeting_transcript_text(settings.data_dir, paths.job_id, polished_text)

        await message.reply_text(
            f"會議 ID：{paths.job_id}\n\n{_preview_text(polished_text)}\n\n請選擇要輸出的檔案：",
            reply_markup=_export_keyboard(paths.job_id),
        )
    except AudioProcessingError as exc:
        await message.reply_text(str(exc))
    except DeepgramError as exc:
        await message.reply_text(str(exc))
    except RateLimitError as exc:
        error_code = getattr(exc, "code", None)
        if error_code in {"insufficient_quota", "credit_balance_exhausted"}:
            await message.reply_text(
                "OpenAI API 額度不足或 billing 尚未啟用。請到 OpenAI Platform 檢查 Billing / Credits，"
                "加值或提高額度後再重試。"
            )
        else:
            await message.reply_text("OpenAI API 暫時達到速率限制，請稍後再試。")
    except Exception as exc:
        await message.reply_text(f"處理失敗：{exc}")


async def handle_export_choice(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    user = update.effective_user
    if not query or not user or not query.data:
        return

    await query.answer()

    try:
        _, meeting_id, export_type = query.data.split(":", 2)
    except ValueError:
        await query.edit_message_reply_markup(reply_markup=None)
        await query.message.reply_text("無法辨識輸出選項，請重新上傳音檔後再試。")
        return

    if export_type == "none":
        await query.edit_message_reply_markup(reply_markup=None)
        await query.message.reply_text(f"已略過會議 {meeting_id} 的檔案輸出。")
        return

    meeting = get_meeting_export(settings.data_dir, meeting_id, user.id)
    if not meeting:
        await query.edit_message_reply_markup(reply_markup=None)
        await query.message.reply_text("找不到這筆會議，或你沒有權限輸出此會議。")
        return

    if not meeting.transcript_text.strip():
        await query.edit_message_reply_markup(reply_markup=None)
        await query.message.reply_text("這筆會議目前沒有可輸出的逐字稿內容。")
        return

    await query.edit_message_reply_markup(reply_markup=None)
    PENDING_EXPORTS[user.id] = PendingExport(meeting_id=meeting_id, export_type=export_type)

    speaker_samples = get_meeting_speaker_samples(settings.data_dir, meeting_id)
    aliases = get_speaker_aliases(settings.data_dir, meeting_id, user.id)
    await query.message.reply_text(
        "輸出前請確認主講人名稱。\n\n"
        "下面是每個 Speaker 的代表發言片段，請依照你聽到的內容填入對應名稱。\n"
        "你可以複製下方範本後填寫，或回覆 `直接輸出` 保留原本的 Speaker 標籤：\n\n"
        f"{_speaker_alias_template(speaker_samples, aliases)}"
    )


def _extract_audio_meta(update: Update):
    message = update.message
    if not message:
        raise ValueError("找不到訊息內容。")

    if message.voice:
        return message.voice, ".ogg", message.voice.file_size
    if message.audio:
        suffix = Path(message.audio.file_name or "audio.mp3").suffix or ".mp3"
        return message.audio, suffix, message.audio.file_size
    if message.document:
        suffix = Path(message.document.file_name or "audio").suffix or ".bin"
        return message.document, suffix, message.document.file_size

    raise ValueError("請傳 Telegram 語音訊息或音檔。")


def _preview_text(text: str) -> str:
    if len(text) <= 3500:
        return text
    return f"{text[:3500]}\n\n文字稿較長，完整內容請下載附件。"


async def _reply_document(message, path: Path, caption: str) -> None:
    with path.open("rb") as file:
        await message.reply_document(document=file, filename=path.name, caption=caption)


async def _export_meeting_documents(message, context: ContextTypes.DEFAULT_TYPE, meeting_id: str, export_type: str, user_id: int) -> None:
    meeting = get_meeting_export(settings.data_dir, meeting_id, user_id)
    if not meeting:
        await message.reply_text("找不到這筆會議，或你沒有權限輸出此會議。")
        return

    aliases = get_speaker_aliases(settings.data_dir, meeting_id, user_id)
    transcript_text = polish_local_transcript(apply_speaker_aliases(meeting.transcript_text, aliases))
    transcript_txt = Path(meeting.transcript_txt_path)
    transcript_docx = Path(meeting.transcript_docx_path)

    await context.bot.send_chat_action(chat_id=message.chat_id, action=ChatAction.UPLOAD_DOCUMENT)

    if export_type in {"txt", "both"}:
        write_text(transcript_txt, transcript_text)
        await _reply_document(message, transcript_txt, "文字稿 TXT")

    if export_type in {"docx", "both"}:
        write_docx(transcript_docx, "會議逐字稿", transcript_text)
        await _reply_document(message, transcript_docx, "文字稿 DOCX")

    await message.reply_text(f"會議 {meeting_id} 的檔案已輸出完成。")


def _export_keyboard(meeting_id: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("輸出 TXT", callback_data=f"export:{meeting_id}:txt"),
                InlineKeyboardButton("輸出 DOCX", callback_data=f"export:{meeting_id}:docx"),
            ],
            [
                InlineKeyboardButton("TXT + DOCX", callback_data=f"export:{meeting_id}:both"),
                InlineKeyboardButton("不用輸出檔案", callback_data=f"export:{meeting_id}:none"),
            ],
        ]
    )


def _speaker_alias_template(speaker_samples, aliases: dict[str, str]) -> str:
    if not speaker_samples:
        return "直接輸出"

    lines = []
    for sample in speaker_samples:
        label = sample.speaker_label
        display_name = aliases.get(label, "")
        sample_text = _sample_text(sample.text)
        lines.append(f"{label}：{sample_text}")
        lines.append(f"{label} = {display_name}")
        lines.append("")
    return "\n".join(lines).strip()


def _speaker_alias_summary(aliases: dict[str, str]) -> str:
    if not aliases:
        return "目前尚未設定主講人名稱。"
    rendered = "\n".join(f"{source} -> {target}" for source, target in aliases.items())
    return f"目前主講人對應：\n{rendered}"


def _sample_text(text: str, max_chars: int = 48) -> str:
    cleaned = polish_local_transcript(text).replace("\n", " ")
    if len(cleaned) <= max_chars:
        return cleaned
    return f"{cleaned[:max_chars]}..."
