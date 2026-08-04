# 會議語音逐字稿 Bot

以 Telegram 為入口的繁體中文會議逐字稿工具。使用者在手機傳送語音或音檔後，Bot 會自動轉錄、區分不同聲音、提供主講人命名，並輸出 TXT 或 Word（DOCX）。

## 功能

- 支援 Telegram 語音、音檔與音訊文件。
- 預設使用 Deepgram Nova-3 進行中文轉錄。
- 支援 Deepgram 與可選的本機 pyannote.audio 多人語者分離；不需要在錄音中先自我介紹。
- 將來源標籤正規化為 `Speaker 1`、`Speaker 2`，並可在每場會議個別命名。
- 提供原始逐字稿、清理版、會議紀錄、決議事項摘要四種輸出模式。
- 預設使用本機 Ollama 潤稿，將內容轉為繁體中文並保守清理雜訊。
- 輸出 UTF-8 TXT 與含標題、日期、主講人清單的 Word 檔。
- 使用 SQLite 保存每個使用者、每場會議的逐字稿與主講人別名。

## 系統需求

- Python 3.11+
- [ffmpeg](https://ffmpeg.org/)（需可在 PowerShell 執行 `ffmpeg -version`）
- Telegram Bot Token
- Deepgram API Key
- Ollama 與 `qwen3:8b`（預設本機潤稿；可選但建議安裝）

OpenAI、pyannote.audio 與 SMTP 都是選用功能。

## 本機安裝

```powershell
git clone https://github.com/wiismm12-star/meeting-transcript-bot.git
cd meeting-transcript-bot
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
python -m pip install -e .
Copy-Item .env.example .env
```

若 PowerShell 阻止啟用虛擬環境，仍可直接使用 ` .\.venv\Scripts\python.exe ` 執行下列所有指令。

## 設定 Bot 與 Deepgram

1. 在 Telegram 向 [@BotFather](https://t.me/BotFather) 建立 Bot，取得 Token。
2. 在 [Deepgram Console](https://console.deepgram.com/) 建立 API Key。
3. 編輯 `.env`，至少填入以下欄位：

```env
TELEGRAM_BOT_TOKEN=你的_telegram_bot_token
DEEPGRAM_API_KEY=你的_deepgram_api_key
TRANSCRIBE_PROVIDER=deepgram
ENABLE_POLISH=true
POLISH_PROVIDER=ollama
OLLAMA_BASE_URL=http://127.0.0.1:11434
OLLAMA_TEXT_MODEL=qwen3:8b
DATA_DIR=./data
MAX_AUDIO_MB=50
```

請勿提交 `.env`、API Key、Token、音檔或 `data/` 資料夾。

## 本機免費潤稿（預設）

安裝 [Ollama](https://ollama.com/) 後執行：

```powershell
ollama pull qwen3:8b
```

逐字稿會在本機整理，不會因潤稿而傳送給雲端模型。若不需要潤稿，可將 `ENABLE_POLISH=false`。

### OpenAI 潤稿（選用、依用量計費）

```env
ENABLE_POLISH=true
POLISH_PROVIDER=openai
OPENAI_API_KEY=你的_openai_api_key
OPENAI_TEXT_MODEL=gpt-4.1-mini
```

也可改用 OpenAI 轉錄：

```env
TRANSCRIBE_PROVIDER=openai
OPENAI_API_KEY=你的_openai_api_key
OPENAI_TRANSCRIBE_MODEL=gpt-4o-transcribe-diarize
```

## 多人語者分離（pyannote.audio，選用）

pyannote.audio 會依聲音特徵做分群，不需要主講人在錄音裡報名字。它只產生通用標籤，真實名稱仍由會議使用者自行填寫。

1. 建立 Hugging Face 帳號，並接受 [`pyannote/speaker-diarization-community-1`](https://huggingface.co/pyannote/speaker-diarization-community-1) 的使用條款。
2. 建立具有讀取權限的 Hugging Face Token。
3. 安裝選用依賴並設定 `.env`：

```powershell
.\.venv\Scripts\python.exe -m pip install -e ".[pyannote]"
```

```env
TRANSCRIBE_PROVIDER=deepgram
ENABLE_PYANNOTE_DIARIZATION=true
PYANNOTE_HF_TOKEN=你的_Hugging_Face_Token
PYANNOTE_NUM_SPEAKERS=0
```

`PYANNOTE_NUM_SPEAKERS=0` 代表自動估計人數；已知是四人會議時，可設為 `4`。

## 啟動與使用

```powershell
.\.venv\Scripts\python.exe -m transcript_bot.main
```

1. 在 Telegram 對 Bot 傳送語音或音檔。
2. 完成後選擇「輸出 TXT」、「輸出 Word 檔（DOCX）」或兩者。
3. Bot 會逐一顯示每位 Speaker 的代表片段；直接回覆名稱，或按「跳過此人」。
4. Word 檔會帶入會議日期、會議 ID、主講人清單及格式化段落。

可使用以下命令：

```text
/latest                 查看最近一次會議並重新輸出
/mode raw               原始逐字稿
/mode cleaned           清理版逐字稿
/mode minutes           保守會議紀錄
/mode summary           僅擷取原文明確的決議或行動
/delete <meeting_id>    刪除自己的會議與相關本機檔案
```

## Email 寄送（目前預設關閉）

SMTP 寄送程式已保留，但避免尚未完成設定時干擾使用流程，目前預設關閉。完成 SMTP 設定與測試後，在 `.env` 設為：

```env
SMTP_HOST=smtp.example.com
SMTP_PORT=587
SMTP_USERNAME=你的寄件帳號
SMTP_PASSWORD=你的SMTP應用程式密碼
SMTP_FROM=你的寄件帳號
SMTP_USE_TLS=true
ENABLE_EMAIL_DELIVERY=true
```

請使用服務商建立的 SMTP 應用程式密碼，不要填一般登入密碼。

## 資料保存與刪除

- 資料庫、下載音檔與匯出檔均位於 `DATA_DIR`（預設 `./data`）。
- 每場會議以唯一 meeting ID 隔離，主講人別名也依 Telegram 使用者與會議分開保存。
- 使用 `/delete <meeting_id>` 可刪除自己的會議資料與其工作目錄。
- 刪除後無法復原，請先確認需要的檔案已下載。

## 測試

```powershell
.\.venv\Scripts\python.exe -m unittest discover -s tests -v
```

GitHub Actions 會在每次 push 與 pull request 自動執行同一套測試。

## 限制與安全提醒

- 多人同時說話、遠距收音、回音或雜訊，均可能降低分群與轉錄品質。
- Speaker 標籤代表不同聲音群組，不代表系統識別了真實身份。
- 開源版不提供共用的 Telegram、Deepgram、OpenAI 或 Hugging Face API Key；請使用者自行建立與保管。
- 請勿將機密會議資料上傳到未經組織核准的雲端服務。

## 授權、貢獻與安全通報

本專案採用 [MIT License](LICENSE)。請參閱 [CONTRIBUTING.md](CONTRIBUTING.md) 與 [SECURITY.md](SECURITY.md)。
