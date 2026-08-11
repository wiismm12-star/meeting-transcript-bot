from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    # The web workspace is intended for trusted internal-network use. Keep the
    # bind address configurable so deployments can restrict it to one LAN interface.
    web_host: str = "0.0.0.0"
    web_port: int = 8765
    telegram_bot_token: str = ""
    telegram_api_id: int | None = None
    telegram_api_hash: str = ""
    telegram_api_base_url: str = ""
    telegram_local_mode: bool = False
    telegram_local_file_root: str = "/var/lib/telegram-bot-api"
    telegram_local_file_host_root: Path = Path("./data/telegram-bot-api")
    # A local Bot API server may need to fetch a large file from Telegram before
    # it can answer getFile, which takes much longer than PTB's 5-second default.
    telegram_request_timeout: int = 600
    openai_api_key: str = ""
    deepgram_api_key: str = ""
    deepgram_keyterms: str = ""
    deepgram_timeout: int = 900
    gladia_api_key: str = ""
    transcribe_provider: str = "deepgram"
    whisper_model: str = "large-v3"
    whisper_device: str = "cpu"
    whisper_compute_type: str = "int8"
    whisper_language: str = "zh"
    whisper_initial_prompt: str = ""
    whisper_model_dir: Path = Path("./data/models/whisper")
    gladia_num_speakers: int = 0
    gladia_vocabulary: str = ""
    openai_transcribe_model: str = "gpt-4o-transcribe-diarize"
    openai_text_model: str = "gpt-4.1-mini"
    enable_polish: bool = False
    polish_provider: str = "ollama"
    ollama_base_url: str = "http://127.0.0.1:11434"
    ollama_text_model: str = "qwen3:8b"
    smtp_host: str = ""
    smtp_port: int = 587
    smtp_username: str = ""
    smtp_password: str = ""
    smtp_from: str = ""
    smtp_use_tls: bool = True
    enable_email_delivery: bool = False
    enable_line_bot: bool = False
    line_channel_secret: str = ""
    line_channel_access_token: str = ""
    line_proxy_port: int = 8766
    # A phone-reachable base URL used only in LINE completion messages. Leave
    # empty to keep completed transcripts inside the local workspace.
    line_download_base_url: str = ""
    # Public workspace mode is deliberately opt-in.  When enabled, the Web
    # process must stay on loopback and be exposed only through a HTTPS tunnel.
    enable_google_login: bool = False
    google_oauth_client_id: str = ""
    google_oauth_client_secret: str = ""
    google_oauth_redirect_uri: str = ""
    google_oauth_allowed_domain: str = ""
    web_session_secret: str = ""
    public_web_base_url: str = ""
    web_allow_upload: bool = True
    # Optional dual-access mode: direct private-network requests can use the
    # shared LAN workspace without Google, while the public tunnel still
    # requires Google login.
    web_lan_bypass_google_login: bool = False
    web_lan_trusted_cidrs: str = "127.0.0.0/8,10.0.0.0/8,172.16.0.0/12,192.168.0.0/16"
    # Keep bot-originated meetings on the shared internal workflow by default.
    # Enable only when Telegram/LINE users should explicitly claim records in
    # the public Google-protected workspace.
    bot_use_public_claim_links: bool = False
    enable_pyannote_diarization: bool = False
    pyannote_hf_token: str = ""
    pyannote_num_speakers: int = 0
    pyannote_min_speakers: int = 2
    pyannote_max_speakers: int = 15
    pyannote_model: str = "pyannote/speaker-diarization-community-1"
    data_dir: Path = Path("./data")
    max_audio_mb: int = 200
    # Long-recording support: split into chunks at silence boundaries and
    # transcribe the chunks in parallel, then merge into one transcript.
    chunk_max_seconds: int = 600
    chunk_overlap_seconds: float = 1.5
    chunk_min_silence_seconds: float = 0.5
    chunk_max_workers: int = 4
    max_concurrent_jobs: int = 2
    # 台灣在地化專有名詞對照表（ASR 誤讀糾錯 + 品牌詞保護）。
    # 檔案不存在時自動跳過，不影響潤稿流程。
    glossary_file: Path = Path("./data/glossary.txt")

    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8")

    @property
    def max_audio_bytes(self) -> int:
        return self.max_audio_mb * 1024 * 1024

    def validate_runtime(self) -> None:
        missing = []
        if not self.telegram_bot_token:
            missing.append("TELEGRAM_BOT_TOKEN")
        if self.transcribe_provider == "openai" and not self.openai_api_key:
            missing.append("OPENAI_API_KEY")
        if self.transcribe_provider == "deepgram" and not self.deepgram_api_key:
            missing.append("DEEPGRAM_API_KEY")
        if self.transcribe_provider == "gladia" and not self.gladia_api_key:
            missing.append("GLADIA_API_KEY")
        if self.enable_polish and self.polish_provider == "openai" and not self.openai_api_key:
            missing.append("OPENAI_API_KEY")
        if self.enable_pyannote_diarization and not self.pyannote_hf_token:
            missing.append("PYANNOTE_HF_TOKEN")
        if self.enable_line_bot and not self.line_channel_secret:
            missing.append("LINE_CHANNEL_SECRET")
        if self.enable_line_bot and not self.line_channel_access_token:
            missing.append("LINE_CHANNEL_ACCESS_TOKEN")
        if missing:
            joined = ", ".join(missing)
            raise RuntimeError(f"缺少環境變數：{joined}，請先建立並設定 .env。")

        if self.transcribe_provider not in {"deepgram", "gladia", "openai", "whisper"}:
            raise RuntimeError("TRANSCRIBE_PROVIDER 只能是 deepgram、gladia、openai 或 whisper。")
        if self.polish_provider not in {"ollama", "openai"}:
            raise RuntimeError("POLISH_PROVIDER 只能是 ollama 或 openai。")
        if self.enable_pyannote_diarization and self.transcribe_provider not in {"deepgram", "whisper"}:
            raise RuntimeError("啟用 pyannote 語者分離時，TRANSCRIBE_PROVIDER 必須設為 deepgram 或 whisper。")
        if self.pyannote_num_speakers < 0:
            raise RuntimeError("PYANNOTE_NUM_SPEAKERS 必須是 0 或正整數。")
        if self.pyannote_min_speakers < 1:
            raise RuntimeError("PYANNOTE_MIN_SPEAKERS 必須是正整數。")
        if self.pyannote_max_speakers < self.pyannote_min_speakers:
            raise RuntimeError("PYANNOTE_MAX_SPEAKERS 必須大於或等於 PYANNOTE_MIN_SPEAKERS。")
        if self.gladia_num_speakers < 0:
            raise RuntimeError("GLADIA_NUM_SPEAKERS 必須是 0 或正整數。")
        if self.max_concurrent_jobs < 1:
            raise RuntimeError("MAX_CONCURRENT_JOBS 必須是 1 或以上的正整數。")
        if not 1 <= self.web_port <= 65535:
            raise RuntimeError("WEB_PORT 必須介於 1 和 65535 之間。")
        if not 1 <= self.line_proxy_port <= 65535:
            raise RuntimeError("LINE_PROXY_PORT 必須介於 1 和 65535 之間。")
        if self.telegram_request_timeout < 1:
            raise RuntimeError("TELEGRAM_REQUEST_TIMEOUT 必須是正整數。")


settings = Settings()
