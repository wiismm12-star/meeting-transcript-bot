# 會議語音逐字稿 Bot MVP

這是一個 Telegram Bot MVP。使用者可以直接用手機在 Telegram 傳語音訊息或音檔，後端會下載音訊、轉檔、做語音逐字稿與主講人分離，最後回傳文字稿與 Word 檔。

## 功能範圍

- 接收 Telegram 語音訊息、音檔、一般音訊文件
- 使用 ffmpeg 將音訊轉為單聲道 mp3
- 預設使用 Deepgram Nova-3 產生含主講人標籤的逐字稿
- 將 `SPEAKER_0` 這類標籤整理成可讀的 `Speaker 1`
- 產生原始逐字稿 `.txt`
- 產生逐字稿 `.docx`
- 支援使用者回覆 `Speaker 1 = 王經理` 來替換主講人名稱
- 預設以本機 Ollama 免費潤稿，將逐字稿整理成正式繁體中文
- 也可選擇 OpenAI 作為雲端潤稿供應商

## 系統需求

- Python 3.11+
- ffmpeg
- Telegram Bot Token
- Deepgram API Key
- Ollama（預設本機免費潤稿；建議模型：Qwen3 8B）
- OpenAI API Key，可選，只有使用 OpenAI 潤稿或 OpenAI 轉錄時需要
- SMTP 寄件帳號，可選，只有要寄送 Email 附件時需要

## 安裝

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -e .
Copy-Item .env.example .env
```

如果你在這台電腦使用 Python launcher，也可以直接：

```powershell
py -3 -m pip install -e .
```

## 環境變數

編輯 `.env`：

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
ENABLE_PYANNOTE_DIARIZATION=false
PYANNOTE_HF_TOKEN=
PYANNOTE_NUM_SPEAKERS=0
```

## 本機多人語者分離（pyannote.audio，可選）

啟用後由 pyannote.audio 依聲音特徵分群，再以 Deepgram 產生的中文詞級時間戳套用 Speaker 標籤。這不需要主講人在錄音中自我介紹；系統只會產生通用的 `Speaker 1`、`Speaker 2` 等標籤。

1. 建立 Hugging Face 帳號，並接受 [`pyannote/speaker-diarization-community-1`](https://huggingface.co/pyannote/speaker-diarization-community-1) 的使用條款。
2. 建立具有讀取權限的 Hugging Face Token。
3. 使用 Python 3.11+ 建立虛擬環境並安裝選用套件：

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e ".[pyannote]"
```

4. 在 `.env` 設定：

```env
TRANSCRIBE_PROVIDER=deepgram
ENABLE_PYANNOTE_DIARIZATION=true
PYANNOTE_HF_TOKEN=你的_Hugging_Face_Token
PYANNOTE_NUM_SPEAKERS=4
```

`PYANNOTE_NUM_SPEAKERS=0` 代表讓模型自行估計人數；已確定是四人會議時可設為 `4`。啟用後請以虛擬環境啟動 Bot：

```powershell
.\.venv\Scripts\python.exe -m transcript_bot.main
```

如果之後要改回 OpenAI 轉錄：

```env
TRANSCRIBE_PROVIDER=openai
OPENAI_API_KEY=你的_openai_api_key
OPENAI_TRANSCRIBE_MODEL=gpt-4o-transcribe-diarize
```

## 本機免費潤稿（Ollama，預設）

安裝 Ollama 後，下載一次模型即可離線使用；逐字稿不會傳送到雲端：

```powershell
ollama pull qwen3:8b
```

`.env` 使用以下設定：

```env
ENABLE_POLISH=true
POLISH_PROVIDER=ollama
OLLAMA_TEXT_MODEL=qwen3:8b
```

## 改用 OpenAI 潤稿（可選、依 API 用量計費）

```env
ENABLE_POLISH=true
POLISH_PROVIDER=openai
OPENAI_API_KEY=你的_openai_api_key
OPENAI_TEXT_MODEL=gpt-4.1-mini
```

## 寄送至指定 Email（可選）

設定 SMTP 後，使用者每次輸出 TXT 或 DOCX 後，可直接在 Telegram 輸入不同的收件 Email，或按下略過寄送按鈕。寄件帳號只設定在伺服器的 `.env`。寄送功能預設關閉，完成 SMTP 設定並驗證後才設為 `true`：

```env
SMTP_HOST=smtp.example.com
SMTP_PORT=587
SMTP_USERNAME=你的寄件帳號
SMTP_PASSWORD=你的SMTP應用程式密碼
SMTP_FROM=你的寄件帳號
SMTP_USE_TLS=true
ENABLE_EMAIL_DELIVERY=false
```

請使用服務商提供的 SMTP 應用程式密碼，不要使用一般登入密碼。

如果要把 Deepgram 產生的逐字稿再交給 OpenAI 整理成正式稿，設定如下：

```env
ENABLE_POLISH=true
POLISH_PROVIDER=openai
OPENAI_API_KEY=你的_openai_api_key
OPENAI_TEXT_MODEL=gpt-4.1-mini
```

確認 ffmpeg 可執行：

```powershell
ffmpeg -version
```

## 啟動

```powershell
py -3 -m transcript_bot.main
```

如果 `transcript-bot` 指令可用，也可以：

```powershell
transcript-bot
```

啟動後不要關掉 PowerShell 視窗，回 Telegram 傳語音給你的 Bot 測試。

## 使用方式

1. 在 Telegram 找到你的 Bot
2. 傳一段語音訊息或音檔
3. Bot 會回覆處理狀態
4. 完成後會回傳 `.txt` 和 `.docx`
5. 如果要替換主講人名稱，可以傳：

```text
Speaker 1 = 王經理
Speaker 2 = 陳工程師
```

## 目前限制

- 第一版使用 Bot polling，正式部署可改成 webhook
- 主講人真名需由使用者手動指定
- 多人同時說話、遠距收音、回音大的會議室會影響語者分離準確度
- 長音檔建議先切段或使用雲端任務佇列處理

## MVP 進度清單

- [x] 建立 Telegram Bot 專案骨架
- [x] 支援 Telegram polling，本機啟動即可收訊息
- [x] 接收手機語音訊息、音檔與音訊文件
- [x] 下載 Telegram 音檔到本機工作目錄
- [x] 使用 ffmpeg 轉成單聲道 mp3
- [x] 串接 Deepgram Speech-to-Text
- [x] 啟用 Deepgram speaker diarization 主講人分離
- [x] 將主講人整理成 `Speaker 1`、`Speaker 2`
- [x] 支援 `Speaker 1 = 王經理` 這類主講人名稱對應
- [x] 產出 `.txt` 文字稿
- [x] 產出 `.docx` Word 文字稿
- [x] 回傳文字預覽與附件到 Telegram
- [x] 將逐字稿統一轉為繁體中文
- [x] 加入本地潤稿清理：移除多餘空白、重複標點與明顯語助詞
- [x] 保留 OpenAI 潤飾為可選功能，預設關閉
- [ ] 加入 LINE Bot 入口
- [ ] 建立 Web 校稿介面
- [ ] 支援逐字稿段落手動編輯
- [ ] 支援主講人名稱持久化儲存
- [ ] 支援會議稿模板，例如逐字稿、會議紀錄、決議事項摘要
- [ ] 加入長音檔切段與背景任務佇列
- [ ] 加入雲端檔案儲存，例如 S3 或 Cloudflare R2
- [ ] 改成 webhook / 雲端部署
- [ ] 加入使用者、權限與用量紀錄
- [ ] 加入成本估算與錯誤重試機制

## 下一步建議

- 加入 LINE Bot 入口
- 加入 Web 校稿介面
- 加入會議稿模板管理
- 加入 S3 / Cloudflare R2 儲存
- 加入工作佇列，例如 Celery、RQ 或 BullMQ
