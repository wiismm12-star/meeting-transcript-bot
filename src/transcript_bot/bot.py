from __future__ import annotations

import asyncio
import json
import logging
import shutil
from dataclasses import dataclass
from pathlib import Path
from pathlib import PurePosixPath

from openai import RateLimitError
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ChatAction
from telegram.error import BadRequest, TimedOut
from telegram.ext import (
    Application,
    ApplicationBuilder,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

from transcript_bot.audio import AudioProcessingError, get_audio_duration, normalize_audio
from transcript_bot.config import settings
from transcript_bot.database import (
    MeetingRecord,
    SpeakerSample,
    create_meeting,
    delete_meeting,
    get_latest_meeting_id,
    get_meeting_export,
    get_meeting_segments,
    get_meeting_speaker_samples,
    get_speaker_aliases,
    save_transcript_segments,
    update_meeting_metadata,
    update_meeting_summary_text,
    update_meeting_transcript_text,
    upsert_speaker_aliases,
)
from transcript_bot.deepgram import DeepgramError
from transcript_bot.gladia import GladiaError
from transcript_bot.whisper_local import LocalWhisperError
from transcript_bot.pyannote_diarization import PyannoteDiarizationError
from transcript_bot.ollama_client import OllamaError
from transcript_bot.mailer import EmailDeliveryError, is_valid_email, send_transcript_email
from transcript_bot.exporters import write_docx, write_text
from transcript_bot.formatting import (
    MeetingSummary,
    apply_speaker_aliases,
    build_fallback_meeting_summary,
    normalize_speaker_labels,
    parse_alias_message,
    polish_local_transcript,
    polish_transcript,
    render_plain_transcript,
    render_raw_transcript,
    render_meeting_minutes,
)
from transcript_bot.ollama_client import summarize_meeting_with_ollama
from transcript_bot.storage import create_job_paths
from transcript_bot.transcription import transcribe_audio_smart
from transcript_bot.job_status import cancel_requested, clear_job_status, write_job_status


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
OUTPUT_MODES: dict[int, str] = {}


class TelegramLocalFileError(RuntimeError):
    """The local Bot API file path cannot be read from this host."""


def _telegram_meeting_title(message) -> str:
    """Use the uploaded file name as the same initial meeting title as Web."""
    for media in (getattr(message, "audio", None), getattr(message, "document", None)):
        file_name = getattr(media, "file_name", None)
        if file_name:
            return Path(file_name).stem[:160]
    return "Telegram 會議"


def _generate_meeting_summary(transcript: str, meeting_title: str) -> MeetingSummary:
    """Match the Web summary behavior, retaining a readable offline fallback."""
    try:
        payload = summarize_meeting_with_ollama(transcript, meeting_title)
        return MeetingSummary(
            title=str(payload["title"]),
            overview=str(payload["overview"]),
            highlights=[str(item) for item in payload["highlights"]],
        )
    except OllamaError:
        return build_fallback_meeting_summary(transcript, meeting_title)


def _serialize_summary(summary: MeetingSummary) -> str:
    return json.dumps(
        {"title": summary.title, "overview": summary.overview, "highlights": summary.highlights},
        ensure_ascii=False,
    )


def _summary_preview(summary: MeetingSummary) -> str:
    highlights = "\n".join(f"- {item}" for item in summary.highlights[:3])
    detail = f"\n{highlights}" if highlights else ""
    return f"會議摘要：{summary.title}\n{summary.overview}{detail}"


def _local_telegram_file_path(file_path: str) -> Path | None:
    """Map a local Bot API path to its host-mounted counterpart safely.

    Docker Desktop replaces ``:`` in Linux file names with U+F03A on Windows
    bind mounts. Bot tokens contain a colon, so try both spellings before
    declaring the local file unavailable.
    """
    try:
        relative = PurePosixPath(file_path).relative_to(settings.telegram_local_file_root)
    except ValueError:
        relative = None

    root = settings.telegram_local_file_host_root
    candidates = [root.joinpath(*relative.parts)] if relative is not None else []
    if relative is not None and ":" in file_path:
        candidates.append(root.joinpath(*(part.replace(":", "\uf03a") for part in relative.parts)))
    direct_match = next((path for path in candidates if path.is_file()), None)
    if direct_match is not None:
        return direct_match

    # Some Docker Desktop releases alter more than just the colon in the Bot
    # API's absolute path. The generated media filename is unique per bot, so
    # fall back to finding that filename only inside the explicitly configured
    # mounted directory. Never use a path supplied by Telegram outside it.
    filename = PurePosixPath(file_path).name
    if not filename:
        return None
    try:
        matches = [path for path in root.rglob(filename) if path.is_file()]
    except OSError:
        return None
    return max(matches, key=lambda path: path.stat().st_mtime_ns, default=None)


async def _download_telegram_audio(telegram_file, target: Path) -> None:
    if settings.telegram_local_mode and telegram_file.file_path:
        source = _local_telegram_file_path(telegram_file.file_path)
        if source is not None:
            shutil.copyfile(source, target)
            return
        raise TelegramLocalFileError(
            "Local Bot API returned a file path that is not available in the host-mounted directory."
        )
    await telegram_file.download_to_drive(custom_path=target)


def _telegram_download_error_message(exc: BadRequest | TimedOut) -> str:
    """Return a safe, actionable message for Telegram download failures."""
    if isinstance(exc, TimedOut):
        return "Telegram 音檔下載逾時，請稍後重試。"
    if "file is too big" in str(exc).lower():
        return (
            "這個音檔超過 Telegram Bot 可下載的大小限制，無法開始轉錄。"
            "請改傳 20–30 秒測試片段、壓縮或切成較短音檔，"
            "或改用本機 Web 工作台上傳。"
        )
    return "無法從 Telegram 下載這個音檔，請稍後重試或改用本機 Web 工作台上傳。"


def build_application(token: str) -> Application:
    builder = ApplicationBuilder().token(token)
    builder.read_timeout(settings.telegram_request_timeout).media_write_timeout(
        settings.telegram_request_timeout
    )
    if settings.telegram_api_base_url:
        base = settings.telegram_api_base_url.rstrip("/")
        builder.base_url(f"{base}/bot").base_file_url(f"{base}/file/bot").local_mode(settings.telegram_local_mode)
    app = builder.build()
    app.add_handler(CallbackQueryHandler(handle_export_choice, pattern=r"^export:"))
    app.add_handler(CallbackQueryHandler(handle_speaker_name_action, pattern=r"^speaker:"))
    app.add_handler(CallbackQueryHandler(handle_email_action, pattern=r"^email:"))
    app.add_handler(CommandHandler("latest", handle_latest_command))
    app.add_handler(CommandHandler("mode", handle_mode_command))
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
    preview_text = _preview_text(_transcript_for_mode(meeting, user_id))
    await update.message.reply_text(
        f"最近一次會議 ID：{meeting_id}\n\n"
        f"{preview_text}\n\n"
        f"{alias_text}\n\n"
        "你也可以重新選擇要輸出的檔案：",
        reply_markup=_export_keyboard(meeting_id),
    )


async def handle_mode_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.effective_user or not update.message:
        return

    if len(context.args) != 1 or context.args[0].lower() not in {"raw", "cleaned", "minutes"}:
        await update.message.reply_text(
            "用法：`/mode raw` 原始逐字稿、`/mode cleaned` 清理版，或 `/mode minutes` 會議紀錄。"
        )
        return

    mode = context.args[0].lower()
    OUTPUT_MODES[update.effective_user.id] = mode
    label = {"raw": "原始逐字稿", "cleaned": "清理版逐字稿", "minutes": "會議紀錄"}[mode]
    await update.message.reply_text(f"已切換為「{label}」模式，之後的預覽與匯出都會使用此模式。")


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
                id=paths.job_id, user_id=user.id, source_platform="telegram",
                audio_file_path=str(paths.input_audio), normalized_audio_path=str(paths.normalized_audio),
                transcript_txt_path=str(paths.transcript_txt), transcript_docx_path=str(paths.transcript_docx),
            ),
        )
        loop = asyncio.get_running_loop()
        last_pushed_pct = -10
        last_pushed_step = ""

        def _progress_text(step: str, pct: int) -> str:
            labels = {
                "downloading": "從 Telegram 下載音檔",
                "normalizing": "音檔標準化",
                "transcribing": "語音辨識",
                "polishing": "潤稿整理",
                "summarizing": "整理會議摘要",
                "completed": "處理完成",
            }
            return f"處理進度：{pct}%\n目前階段：{labels.get(step, '處理中')}"

        async def _send_progress(step: str, pct: int) -> None:
            try:
                # Telegram does not normally notify users when a message is
                # edited. Send a fresh, throttled status message so progress
                # reaches the user as an actual push notification.
                await message.reply_text(_progress_text(step, pct))
            except (BadRequest, TimedOut):
                logger.debug("Unable to send Telegram progress message", exc_info=True)

        def _schedule_progress(step: str, pct: int) -> None:
            nonlocal last_pushed_pct, last_pushed_step
            # A stage change is always useful; during transcription, limit
            # updates to 10% increments to stay well below Telegram rate limits.
            if step == last_pushed_step and pct < last_pushed_pct + 10:
                return
            last_pushed_step = step
            last_pushed_pct = pct
            asyncio.create_task(_send_progress(step, pct))

        def _set_progress(step: str, pct: int, label: str) -> None:
            write_job_status(settings.data_dir, paths.job_id, source="telegram", step=step, pct=pct, label=label)
            # Callbacks from the transcription worker run in a background
            # thread, so hand Telegram I/O back to the application's event loop.
            loop.call_soon_threadsafe(_schedule_progress, step, pct)

        _set_progress("downloading", 0, "downloading (從 Telegram 下載音檔)")
        await context.bot.send_chat_action(chat_id=message.chat_id, action=ChatAction.UPLOAD_DOCUMENT)

        telegram_file = await context.bot.get_file(media.file_id)
        await _download_telegram_audio(telegram_file, paths.input_audio)
        if cancel_requested(settings.data_dir, paths.job_id):
            raise asyncio.CancelledError
        _set_progress("normalizing", 5, "normalizing (音檔標準化)")
        normalize_audio(paths.input_audio, paths.normalized_audio)
        if cancel_requested(settings.data_dir, paths.job_id):
            raise asyncio.CancelledError
        duration = get_audio_duration(paths.normalized_audio)
        _set_progress("transcribing", 20, "transcribing (語音辨識)")
        last_transcription_pct = 20

        def _progress(value: float, marker: float) -> None:
            nonlocal last_transcription_pct
            if marker == -999.0:
                pct = min(80, max(25, int(value)))
                label = "transcribing (分段語音辨識)"
            elif value >= 0 and duration > 0:
                # Single-file Whisper reports the end timestamp of each segment.
                # Map that audio-time progress into the 20–80% transcription range.
                pct = min(80, max(20, 20 + int((value / duration) * 60)))
                label = "transcribing (語音辨識)"
            else:
                return

            if pct > last_transcription_pct:
                last_transcription_pct = pct
                _set_progress("transcribing", pct, label)

        def _chunk_label(completed: int, total: int) -> None:
            _set_progress(
                "transcribing", min(80, 20 + int(completed / total * 60)),
                f"transcribing (分段語音辨識 {completed}/{total})",
            )

        segments = await asyncio.to_thread(
            transcribe_audio_smart,
            paths.normalized_audio,
            progress_callback=_progress,
            chunk_label_callback=_chunk_label,
        )
        if cancel_requested(settings.data_dir, paths.job_id):
            raise asyncio.CancelledError
        _set_progress("polishing", 82, "polishing (潤稿整理)")
        normalized_segments = normalize_speaker_labels(segments)
        save_transcript_segments(settings.data_dir, paths.job_id, normalized_segments)

        raw_text = render_plain_transcript(normalized_segments)
        polished_text = polish_transcript(raw_text) if settings.enable_polish else polish_local_transcript(raw_text)
        polished_text = polish_local_transcript(polished_text)
        update_meeting_transcript_text(settings.data_dir, paths.job_id, polished_text)
        meeting_title = _telegram_meeting_title(message)
        update_meeting_metadata(settings.data_dir, paths.job_id, meeting_title, "")
        if cancel_requested(settings.data_dir, paths.job_id):
            raise asyncio.CancelledError
        _set_progress("summarizing", 90, "summarizing (會議摘要)")
        summary = await asyncio.to_thread(_generate_meeting_summary, polished_text, meeting_title)
        update_meeting_summary_text(settings.data_dir, paths.job_id, _serialize_summary(summary))
        await _send_progress("completed", 100)
        clear_job_status(settings.data_dir, paths.job_id)
        displayed_text = (
            render_raw_transcript(normalized_segments)
            if _output_mode(user.id) == "raw"
            else render_meeting_minutes(polished_text)
            if _output_mode(user.id) == "minutes"
            else polished_text
        )

        await message.reply_text(
            f"會議 ID：{paths.job_id}\n\n{_summary_preview(summary)}\n\n"
            f"逐字稿預覽：\n{_preview_text(displayed_text)}\n\n請選擇要輸出的檔案：",
            reply_markup=_export_keyboard(paths.job_id),
        )
    except asyncio.CancelledError:
        _delete_meeting_files(settings.data_dir, paths.job_id)
        delete_meeting(settings.data_dir, paths.job_id, user.id)
        clear_job_status(settings.data_dir, paths.job_id)
        await message.reply_text("已依 Web 工作台的要求終止這次轉錄。")
    except TelegramLocalFileError:
        logger.exception("Local Telegram Bot API file was not available on the host")
        _delete_meeting_files(settings.data_dir, paths.job_id)
        delete_meeting(settings.data_dir, paths.job_id, user.id)
        clear_job_status(settings.data_dir, paths.job_id)
        await message.reply_text(
            "本機 Telegram 大檔服務尚未取得這個音檔，請稍後重試。"
            "若持續發生，請確認本機 Bot API 容器與 data/telegram-bot-api 掛載正常。"
        )
    except (BadRequest, TimedOut) as exc:
        logger.warning("Telegram audio download failed: %s", exc)
        _delete_meeting_files(settings.data_dir, paths.job_id)
        delete_meeting(settings.data_dir, paths.job_id, user.id)
        clear_job_status(settings.data_dir, paths.job_id)
        await message.reply_text(_telegram_download_error_message(exc))
    except AudioProcessingError as exc:
        await message.reply_text(str(exc))
    except DeepgramError as exc:
        await message.reply_text(str(exc))
    except GladiaError as exc:
        await message.reply_text(str(exc))
    except LocalWhisperError as exc:
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
        if 'paths' in locals():
            clear_job_status(settings.data_dir, paths.job_id)
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

    if not _transcript_for_mode(meeting, user.id).strip():
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


async def handle_email_action(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    user = update.effective_user
    if not query or not user or not query.data or not query.message:
        return

    try:
        _, action, meeting_id, export_type = query.data.split(":", 3)
    except ValueError:
        await query.answer("此按鈕已失效，請重新選擇輸出。", show_alert=True)
        return

    pending_email = PENDING_EMAILS.get(user.id)
    if (
        not pending_email
        or pending_email.meeting_id != meeting_id
        or pending_email.export_type != export_type
    ):
        await query.answer("此寄送提示已失效。", show_alert=True)
        return

    await query.answer()
    await query.edit_message_reply_markup(reply_markup=None)
    if action == "skip":
        PENDING_EMAILS.pop(user.id, None)
        await query.message.reply_text("已略過 Email 寄送。")
    else:
        await query.message.reply_text("無法辨識這個按鈕，請重新選擇輸出。")


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
        await message.reply_text("Email 格式不正確，請直接輸入有效的收件 Email，或按下「略過寄送」。")
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


def _output_mode(user_id: int) -> str:
    return OUTPUT_MODES.get(user_id, "cleaned")


def _transcript_for_mode(meeting, user_id: int) -> str:
    if _output_mode(user_id) == "raw":
        return render_raw_transcript(get_meeting_segments(settings.data_dir, meeting.id, user_id))
    if _output_mode(user_id) == "minutes":
        return render_meeting_minutes(meeting.transcript_text)
    return meeting.transcript_text


async def _export_meeting_documents(message, context: ContextTypes.DEFAULT_TYPE, meeting_id: str, export_type: str, user_id: int) -> None:
    meeting = get_meeting_export(settings.data_dir, meeting_id, user_id)
    if not meeting:
        await message.reply_text("找不到這筆會議，或你沒有權限輸出此會議。")
        return

    aliases = get_speaker_aliases(settings.data_dir, meeting_id, user_id)
    transcript_text = apply_speaker_aliases(_transcript_for_mode(meeting, user_id), aliases)
    speaker_names = _meeting_speaker_names(meeting_id, user_id, aliases)
    if _output_mode(user_id) == "cleaned":
        transcript_text = polish_local_transcript(transcript_text)
    transcript_txt = Path(meeting.transcript_txt_path)
    transcript_docx = Path(meeting.transcript_docx_path)

    try:
        await context.bot.send_chat_action(chat_id=message.chat_id, action=ChatAction.UPLOAD_DOCUMENT)

        if export_type in {"txt", "both"}:
            write_text(transcript_txt, transcript_text)
            await _reply_document(message, transcript_txt, "文字稿 TXT")

        if export_type in {"docx", "both"}:
            title = {
                "minutes": "會議紀錄",
            }.get(_output_mode(user_id), "會議逐字稿")
            write_docx(
                transcript_docx,
                title,
                transcript_text,
                meeting_id=meeting.id,
                meeting_date=meeting.created_at,
                speakers=speaker_names,
            )
            await _reply_document(message, transcript_docx, "文字稿 DOCX")
    except Exception:
        logger.exception("Failed to export meeting documents")
        await message.reply_text("檔案輸出失敗，請稍後再試。")
        return

    await message.reply_text(f"會議 {meeting_id} 的檔案已輸出完成。")
    if not settings.enable_email_delivery:
        return

    PENDING_EMAILS[user_id] = PendingEmail(meeting_id=meeting_id, export_type=export_type)
    await message.reply_text(
        "若要寄送檔案，請直接回覆收件 Email；不需要寄送請按下方按鈕。",
        reply_markup=_email_skip_keyboard(meeting_id, export_type),
    )


def _export_keyboard(meeting_id: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("輸出 TXT", callback_data=f"export:{meeting_id}:txt"),
                InlineKeyboardButton("輸出 Word 檔（DOCX）", callback_data=f"export:{meeting_id}:docx"),
            ],
            [
                InlineKeyboardButton("TXT ＋ Word", callback_data=f"export:{meeting_id}:both"),
                InlineKeyboardButton("不用輸出檔案", callback_data=f"export:{meeting_id}:none"),
            ],
        ]
    )


def _email_skip_keyboard(meeting_id: str, export_type: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [[InlineKeyboardButton("略過寄送", callback_data=f"email:skip:{meeting_id}:{export_type}")]]
    )


def _speaker_alias_summary(aliases: dict[str, str]) -> str:
    if not aliases:
        return "目前尚未設定主講人名稱。"
    rendered = "\n".join(f"{source} -> {target}" for source, target in aliases.items())
    return f"目前主講人對應：\n{rendered}"


def _meeting_speaker_names(meeting_id: str, user_id: int, aliases: dict[str, str]) -> list[str]:
    names: list[str] = []
    for segment in get_meeting_segments(settings.data_dir, meeting_id, user_id):
        name = aliases.get(segment.speaker, segment.speaker)
        if name not in names:
            names.append(name)
    return names


def _sample_text(text: str, max_chars: int = 48) -> str:
    cleaned = polish_local_transcript(text).replace("\n", " ")
    if len(cleaned) <= max_chars:
        return cleaned
    return f"{cleaned[:max_chars]}..."
