from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    telegram_bot_token: str = ""
    openai_api_key: str = ""
    deepgram_api_key: str = ""
    transcribe_provider: str = "deepgram"
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
    enable_pyannote_diarization: bool = False
    pyannote_hf_token: str = ""
    pyannote_num_speakers: int = 0
    data_dir: Path = Path("./data")
    max_audio_mb: int = 50

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
        if self.enable_polish and self.polish_provider == "openai" and not self.openai_api_key:
            missing.append("OPENAI_API_KEY")
        if self.enable_pyannote_diarization and not self.pyannote_hf_token:
            missing.append("PYANNOTE_HF_TOKEN")
        if missing:
            joined = ", ".join(missing)
            raise RuntimeError(f"缺少環境變數：{joined}，請先建立並設定 .env。")

        if self.transcribe_provider not in {"deepgram", "openai"}:
            raise RuntimeError("TRANSCRIBE_PROVIDER 只能是 deepgram 或 openai。")
        if self.polish_provider not in {"ollama", "openai"}:
            raise RuntimeError("POLISH_PROVIDER 只能是 ollama 或 openai。")
        if self.enable_pyannote_diarization and self.transcribe_provider != "deepgram":
            raise RuntimeError("啟用 pyannote 語者分離時，TRANSCRIBE_PROVIDER 必須設為 deepgram。")
        if self.pyannote_num_speakers < 0:
            raise RuntimeError("PYANNOTE_NUM_SPEAKERS 必須是 0 或正整數。")


settings = Settings()
