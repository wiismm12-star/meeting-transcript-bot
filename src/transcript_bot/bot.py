from __future__ import annotations

import asyncio
import logging
import shutil
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
    SpeakerSample,
    create_meeting,
    delete_meeting,
    get_latest_meeting_id,
    get_meeting_export,
    get_meeting_speaker_samples,
    get_speaker_aliases,
    save_transcript_segments,
    update_meeting_transcript_text,
    upsert_speaker_aliases,
)
from transcript_bot.deepgram import DeepgramError
from transcript_bot.pyannote_diarization import PyannoteDiarizationError
from transcript_bot.ollama_client import OllamaError
from transcript_bot.mailer import EmailDeliveryError, is_valid_email, send_transcript_email
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


logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class PendingExport:
    meeting_id: str
    export_type: str
    speaker_samples: tuple[SpeakerSample, ...]
    current_index: int = 0


@dataclass(frozen=True)
class PendingEmail:
    meeting_id: str
    export_type: str


PENDING_EXPORTS: dict[int, PendingExport] = {}
PENDING_EMAILS: dict[int, PendingEmail] = {}


def build_application(token: str) -> Application:
    app = ApplicationBuilder().token(token).build()
    app.add_handler(CallbackQueryHandler(handle_export_choice, pattern=r"^export:"))
    app.add_handler(CallbackQueryHandler(handle_speaker_name_action, pattern=r"^speaker:"))
    app.add_handler(CommandHandler("latest", handle_latest_command))
    app.add_handler(CommandHandler("delete", handle_delete_command))
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


async def handle_delete_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.effective_user or not update.message:
        return

    if len(context.args) != 1:
        await update.message.reply_text("用法：`/delete <meeting_id>`")
        return

    meeting_id = context.args[0].strip()
    user_id = update.effective_user.id
    meeting = get_meeting_export(settings.data_dir, meeting_id, user_id)
    if not meeting:
        await update.message.reply_text("找不到這筆會議，或你沒有權限刪除它。")
        return

    try:
        _delete_meeting_files(settings.data_dir, meeting_id)
    except OSError:
        logger.exception("Failed to remove meeting files for %s", meeting_id)
        await update.message.reply_text("刪除本機音檔或匯出檔時失敗，請稍後再試。")
        return

    if not delete_meeting(settings.data_dir, meeting_id, user_id):
        await update.message.reply_text("刪除失敗，請稍後再試。")
        return

    pending_export = PENDING_EXPORTS.get(user_id)
    if pending_export and pending_export.meeting_id == meeting_id:
        PENDING_EXPORTS.pop(user_id, None)
    pending_email = PENDING_EMAILS.get(user_id)
    if pending_email and pending_email.meeting_id == meeting_id:
        PENDING_EMAILS.pop(user_id, None)

    await update.message.reply_text(f"已刪除會議 {meeting_id}，以及相關逐字稿與本機檔案。")


async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.effective_user or not update.message or not update.message.text:
        return

    user_id = update.effective_user.id
    text = update.message.text.strip()
    pending_export = PENDING_EXPORTS.get(user_id)

    if pending_export:
        await _handle_pending_speaker_name(update.message, context, pending_export, text, user_id)
        return

    pending_email = PENDING_EMAILS.get(user_id)
    if pending_email:
        await _handle_pending_email(update.message, pending_email, text, user_id)
        return

    aliases = parse_alias_message(update.message.text)
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
    await update.message.reply_text(
        f"已記住會議 {meeting_id} 的主講人對應：\n"
        f"{rendered}\n\n"
        "你可以重新選擇要輸出的檔案：",
        reply_markup=_export_keyboard(meeting_id),
    )


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
    except PyannoteDiarizationError as exc:
        await message.reply_text(str(exc))
    except OllamaError as exc:
        await message.reply_text(str(exc))
    except RateLimitError as exc:
        error_code = getattr(exc, "code", None)
        if error_code in {"insufficient_quota", "credit_balance_exhausted"}:
            await message.reply_text(
                "OpenAI API 額度不足或帳務尚未啟用。請到 OpenAI Platform 檢查帳務與額度，"
                "加值或提高額度後再重試。"
            )
        else:
            await message.reply_text("OpenAI API 暫時達到速率限制，請稍後再試。")
    except Exception:
        logger.exception("Failed to process audio message")
        await message.reply_text("處理失敗，請稍後再試，或改傳較短的音檔。")


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

    speaker_samples = tuple(get_meeting_speaker_samples(settings.data_dir, meeting_id))
    if not speaker_samples:
        await _export_meeting_documents(query.message, context, meeting_id, export_type, user.id)
        return

    pending_export = PendingExport(
        meeting_id=meeting_id,
        export_type=export_type,
        speaker_samples=speaker_samples,
    )
    PENDING_EXPORTS[user.id] = pending_export
    await _ask_for_speaker_name(query.message, pending_export, user.id)


async def handle_speaker_name_action(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    user = update.effective_user
    if not query or not user or not query.data or not query.message:
        return

    try:
        _, action, meeting_id, index_text = query.data.split(":", 3)
        expected_index = int(index_text)
    except ValueError:
        await query.answer("此按鈕已失效，請重新選擇輸出。", show_alert=True)
        return

    pending_export = PENDING_EXPORTS.get(user.id)
    if (
        not pending_export
        or pending_export.meeting_id != meeting_id
        or pending_export.current_index != expected_index
    ):
        await query.answer("此命名提示已失效，請使用最新提示。", show_alert=True)
        return

    await query.answer()
    await query.edit_message_reply_markup(reply_markup=None)
    if action == "skip":
        await _handle_pending_speaker_name(query.message, context, pending_export, "略過", user.id)
    elif action == "export":
        await _handle_pending_speaker_name(query.message, context, pending_export, "直接輸出", user.id)
    else:
        await query.message.reply_text("無法辨識這個按鈕，請直接回覆名稱或重新選擇輸出。")


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


async def _handle_pending_speaker_name(message, context: ContextTypes.DEFAULT_TYPE, pending_export: PendingExport, text: str, user_id: int) -> None:
    if text == "直接輸出":
        PENDING_EXPORTS.pop(user_id, None)
        await _export_meeting_documents(
            message,
            context,
            pending_export.meeting_id,
            pending_export.export_type,
            user_id,
        )
        return

    sample = pending_export.speaker_samples[pending_export.current_index]
    if text not in {"略過", "掠過", "跳過", "保留"}:
        upsert_speaker_aliases(settings.data_dir, pending_export.meeting_id, user_id, {sample.speaker_label: text})

    next_index = pending_export.current_index + 1
    if next_index >= len(pending_export.speaker_samples):
        PENDING_EXPORTS.pop(user_id, None)
        await _export_meeting_documents(
            message,
            context,
            pending_export.meeting_id,
            pending_export.export_type,
            user_id,
        )
        return

    next_pending_export = PendingExport(
        meeting_id=pending_export.meeting_id,
        export_type=pending_export.export_type,
        speaker_samples=pending_export.speaker_samples,
        current_index=next_index,
    )
    PENDING_EXPORTS[user_id] = next_pending_export
    await _ask_for_speaker_name(message, next_pending_export, user_id)


async def _ask_for_speaker_name(message, pending_export: PendingExport, user_id: int) -> None:
    sample = pending_export.speaker_samples[pending_export.current_index]
    current_name = get_speaker_aliases(settings.data_dir, pending_export.meeting_id, user_id).get(sample.speaker_label)
    current_name_text = f"\n目前名稱：{current_name}\n" if current_name else "\n"
    await message.reply_text(
        f"請為 {sample.speaker_label} 命名。\n\n"
        f"代表片段：\n{_sample_text(sample.text)}\n"
        f"{current_name_text}\n"
        "請直接回覆名稱即可；也可使用下方按鈕。",
        reply_markup=InlineKeyboardMarkup(
            [
                [
                    InlineKeyboardButton(
                        "跳過此人",
                        callback_data=(
                            f"speaker:skip:{pending_export.meeting_id}:{pending_export.current_index}"
                        ),
                    ),
                    InlineKeyboardButton(
                        "直接輸出",
                        callback_data=(
                            f"speaker:export:{pending_export.meeting_id}:{pending_export.current_index}"
                        ),
                    ),
                ]
            ]
        ),
    )


def _delete_meeting_files(data_dir: Path, meeting_id: str) -> None:
    """Remove only the job directory derived from a validated meeting ID."""
    if not meeting_id or Path(meeting_id).name != meeting_id:
        raise OSError("Invalid meeting ID")

    jobs_dir = (data_dir / "jobs").resolve()
    job_dir = (jobs_dir / meeting_id).resolve()
    if job_dir.parent != jobs_dir:
        raise OSError("Invalid meeting directory")

    if job_dir.exists():
        shutil.rmtree(job_dir)


async def _reply_document(message, path: Path, caption: str) -> None:
    with path.open("rb") as file:
        await message.reply_document(document=file, filename=path.name, caption=caption)


async def _handle_pending_email(message, pending_email: PendingEmail, text: str, user_id: int) -> None:
    if text.lower() in {"略過", "掠過", "跳過", "不用", "取消"}:
        PENDING_EMAILS.pop(user_id, None)
        await message.reply_text("已略過 Email 寄送。")
        return
    if not is_valid_email(text):
        await message.reply_text("Email 格式不正確，請直接輸入有效的收件 Email，或回覆「略過」。")
        return

    meeting = get_meeting_export(settings.data_dir, pending_email.meeting_id, user_id)
    if not meeting:
        PENDING_EMAILS.pop(user_id, None)
        await message.reply_text("找不到這筆會議，或你沒有權限寄送它。")
        return

    try:
        await asyncio.to_thread(
            send_transcript_email,
            text,
            meeting.id,
            _email_attachments(meeting, pending_email.export_type),
        )
    except EmailDeliveryError as exc:
        await message.reply_text(str(exc))
        return

    PENDING_EMAILS.pop(user_id, None)
    await message.reply_text(f"已寄送會議 {meeting.id} 的檔案到 {text}。")


def _email_attachments(meeting, export_type: str) -> list[Path]:
    attachments: list[Path] = []
    if export_type in {"txt", "both"}:
        attachments.append(Path(meeting.transcript_txt_path))
    if export_type in {"docx", "both"}:
        attachments.append(Path(meeting.transcript_docx_path))
    return attachments


async def _export_meeting_documents(message, context: ContextTypes.DEFAULT_TYPE, meeting_id: str, export_type: str, user_id: int) -> None:
    meeting = get_meeting_export(settings.data_dir, meeting_id, user_id)
    if not meeting:
        await message.reply_text("找不到這筆會議，或你沒有權限輸出此會議。")
        return

    aliases = get_speaker_aliases(settings.data_dir, meeting_id, user_id)
    transcript_text = polish_local_transcript(apply_speaker_aliases(meeting.transcript_text, aliases))
    transcript_txt = Path(meeting.transcript_txt_path)
    transcript_docx = Path(meeting.transcript_docx_path)

    try:
        await context.bot.send_chat_action(chat_id=message.chat_id, action=ChatAction.UPLOAD_DOCUMENT)

        if export_type in {"txt", "both"}:
            write_text(transcript_txt, transcript_text)
            await _reply_document(message, transcript_txt, "文字稿 TXT")

        if export_type in {"docx", "both"}:
            write_docx(transcript_docx, "會議逐字稿", transcript_text)
            await _reply_document(message, transcript_docx, "文字稿 DOCX")
    except Exception:
        logger.exception("Failed to export meeting documents")
        await message.reply_text("檔案輸出失敗，請稍後再試。")
        return

    await message.reply_text(f"會議 {meeting_id} 的檔案已輸出完成。")
    PENDING_EMAILS[user_id] = PendingEmail(meeting_id=meeting_id, export_type=export_type)
    await message.reply_text("若要寄送檔案，請直接回覆收件 Email；不需要請回覆「略過」。")


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
