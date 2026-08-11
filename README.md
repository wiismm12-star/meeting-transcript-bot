# 會議語音逐字稿 Bot

以 Telegram 為入口的繁體中文會議逐字稿工具。使用者在手機傳送語音或音檔後，Bot 會自動轉錄、區分不同聲音、提供主講人命名，並輸出 TXT 或 Word（DOCX）。

## 功能

- 支援 Telegram 語音、音檔與音訊文件。
- 預設使用 Deepgram Nova-3 的台灣繁中模型進行中文轉錄；也可切換至 Gladia 的免費額度方案。
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

OpenAI、Gladia、pyannote.audio 與 SMTP 都是選用功能。

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
DEEPGRAM_KEYTERMS=
ENABLE_POLISH=true
POLISH_PROVIDER=ollama
OLLAMA_BASE_URL=http://127.0.0.1:11434
OLLAMA_TEXT_MODEL=qwen3:8b
DATA_DIR=./data
MAX_AUDIO_MB=50
```

### Telegram 長音檔（選用本機 Bot API）

Telegram 官方雲端 Bot API 可能拒絕 Bot 下載較大的檔案，即使檔案本身可傳到聊天室。需要處理長錄音時，先啟動專案提供的本機 Bot API；它會把 Telegram 檔案存到本機掛載目錄，Bot 隨後直接讀取該檔案：

```powershell
docker compose -p meetingtranscript -f docker-compose.telegram-api.yml up -d
```

再在 `.env` 設定：

```env
TELEGRAM_API_ID=你的_api_id
TELEGRAM_API_HASH=你的_api_hash
TELEGRAM_API_BASE_URL=http://127.0.0.1:8081
TELEGRAM_LOCAL_MODE=true
TELEGRAM_LOCAL_FILE_ROOT=/var/lib/telegram-bot-api
TELEGRAM_LOCAL_FILE_HOST_ROOT=./data/telegram-bot-api
TELEGRAM_REQUEST_TIMEOUT=600
```

在 Windows + Docker Desktop，Bot Token 目錄中的冒號會由掛載層改寫；程式已自動處理這個路徑差異。設定後重啟 Telegram Bot。長檔下載完成後，既有的切段轉錄會自動接手。

請勿提交 `.env`、API Key、Token、音檔或 `data/` 資料夾。

若會議常出現品牌、產品名、站名或人名，可加上以逗號分隔的 Deepgram 專有名詞提示，協助模型保留正確拼寫：

```env
DEEPGRAM_KEYTERMS=KKBOX,風雲榜,忠孝復興,文湖線,手扶梯
```

## Gladia 高準確轉錄（選用）

Gladia 提供多人語者分離、中文與英文混說、自訂詞彙與免費額度。啟用後，音檔會上傳到 Gladia 雲端處理；轉錄完成後，本程式會盡力刪除該次 Gladia 工作與暫存音檔。

1. 到 [Gladia](https://app.gladia.io/) 建立 API Key。
2. 在 `.env` 填入：

```env
TRANSCRIBE_PROVIDER=gladia
GLADIA_API_KEY=你的_gladia_api_key
# 已知會議人數時可填入，例如 4；0 代表自動估計
GLADIA_NUM_SPEAKERS=0
# 以逗號分隔常見專有名詞，可提高辨識準確度
GLADIA_VOCABULARY=KKBOX,風雲榜,文湖線,手扶梯
```

未設定時，專案仍會使用 Deepgram。Gladia 免費額度與服務條件依其官方方案為準。

## 本機免費高準確轉錄（Whisper large-v3）

Whisper 的模型權重可在本機下載與執行，因此音檔不會上傳到雲端，也沒有按分鐘計費。此專案使用較快的 `faster-whisper` 執行器，並保留逐段時間戳，可與 pyannote.audio 搭配分辨多人。

你的 RTX 3060 是 4 GB VRAM，建議先使用 CPU INT8 跑 `large-v3`，避免 GPU 記憶體不足；雖然速度較慢，但品質優先。安裝一次執行器：

```powershell
uv sync --extra whisper
```

再將 `.env` 設為：

```env
TRANSCRIBE_PROVIDER=whisper
WHISPER_MODEL=large-v3
WHISPER_DEVICE=cpu
WHISPER_COMPUTE_TYPE=int8
WHISPER_LANGUAGE=zh
# 專有名詞提示；可沿用既有 DEEPGRAM_KEYTERMS 的內容
WHISPER_INITIAL_PROMPT=KKBOX,風雲榜,忠孝復興,文湖線,手扶梯
WHISPER_MODEL_DIR=./data/models/whisper

# 多人會議建議一起開啟；須另依下一節安裝與設定 pyannote
ENABLE_PYANNOTE_DIARIZATION=true
```

第一次轉錄會下載 Whisper 模型權重至 `data/models/whisper`。此專案刻意不使用 Windows 的 Hugging Face 快取符號連結，因此不需要開啟開發人員模式。Whisper 負責辨識文字；多人分段仍須使用 pyannote.audio。模型不能保證 100% 還原，尤其是重疊說話、遠距收音與未提供的專有名詞，但這是目前最適合本機離線比較的方案。

### 轉錄方案基準比較（已完成）

本專案已使用同一批台灣中文錄音完成 Deepgram、Gladia 與本機 Whisper large-v3 的辨識結果比較。這項驗證已完成，不列為後續 MVP 待辦；只有在更換模型、重要設定或錄音條件後，才需要重新比較。

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
TRANSCRIBE_PROVIDER=whisper
ENABLE_PYANNOTE_DIARIZATION=true
PYANNOTE_HF_TOKEN=你的_Hugging_Face_Token
PYANNOTE_NUM_SPEAKERS=0
```

`PYANNOTE_NUM_SPEAKERS=0` 代表自動估計人數；已知是四人會議時，可設為 `4`。

## LINE Bot（webhook 與音檔轉錄）

LINE Bot 會先驗證安全 webhook；收到 LINE 音訊、影片或支援格式的音檔後，會立即回覆已開始背景轉錄，並將檔案交給與 Web 上傳共用的工作佇列。完成的逐字稿可在本機會議工作台查看、編輯與下載。

請在 LINE Developers 建立 Messaging API channel，取得 Channel secret 與 Channel access token，並在 `.env` 填入：

```env
ENABLE_LINE_BOT=true
LINE_CHANNEL_SECRET=你的_channel_secret
LINE_CHANNEL_ACCESS_TOKEN=你的_channel_access_token
# 讓收到完成通知的手機可直接開啟 TXT／DOCX；可先填同一內網的 Web 位址
LINE_DOWNLOAD_BASE_URL=http://192.168.1.10:8765
```

LINE 需要公開 HTTPS webhook，不能直接連到 `127.0.0.1`。本專案提供只轉送 `/line/webhook` 的本機 Proxy（預設 `127.0.0.1:8766`），避免臨時 Tunnel 直接公開沒有登入保護的 Web 工作台。先在另一個 PowerShell 啟動：

```powershell
.\.venv\Scripts\python.exe -m transcript_bot.line_proxy
```

再以 Cloudflare Tunnel 或 ngrok 將 **8766** 暫時公開，並將它顯示的 HTTPS URL 加上 `/line/webhook` 後，填入 LINE Developers Console：

```text
https://臨時-tunnel-網址/line/webhook
```

按 LINE Console 的「Verify」後應成功。LINE 的 webhook 會先驗證 `X-Line-Signature`，未通過驗證的請求不會被處理。轉錄完成後，Bot 會主動通知原傳送者；設定 `LINE_DOWNLOAD_BASE_URL` 時，通知會附上工作台、TXT 與 Word 的下載連結。

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

## 本機 Web 校稿

啟動本機校稿介面：

```powershell
.\.venv\Scripts\python.exe -m transcript_bot.web
```

網站預設監聽本機所有網卡（`WEB_HOST=0.0.0.0`），同一公司內網的同事可透過 `http://這台電腦的內網-IP:8765` 開啟。若主機有固定內網 DNS，建議改用該名稱。

> ⚠️ **內網存取提醒：** 此工作台目前沒有登入、使用者權限或會議資料隔離；能連入的人都可檢視、修改、下載與刪除全部會議。請只開放給受信任的公司網段，並在 Windows 防火牆建立只允許該網段 TCP 8765 的規則。若需要限制為單一網卡，可在 `.env` 設定 `WEB_HOST=192.168.x.x`；連接埠可用 `WEB_PORT` 變更。

以「私人」網路設定檔為例，請用系統管理員 PowerShell 將 `192.168.1.0/24` 改成公司的實際網段後執行：

```powershell
New-NetFirewallRule -DisplayName "Meeting Transcript Web (Internal)" -Direction Inbound -Action Allow -Protocol TCP -LocalPort 8765 -RemoteAddress 192.168.1.0/24 -Profile Private
```

> ⚠️ **改完 `src/transcript_bot/*.py` 後必須重啟伺服器。** 本機 Web 以 `debug=False` 執行，編輯原始碼**不會**熱重載。若直接把音檔丟到仍在跑的舊程序上，執行的會是舊程式碼（例如潤稿規則沒生效）。重啟步驟：
>
> ```powershell
> # 1. 關閉佔用 8765 的舊程序
> Get-CimInstance Win32_Process | Where-Object { $_.CommandLine -like '*transcript_bot.web*' } | ForEach-Object { Stop-Process -Id $_.ProcessId -Force }
> # 2. 清除 Python 位元組碼快取（避免載入到舊 .pyc）
> find src -name "__pycache__" -type d -exec rm -rf {} + 2>$null
> # 3. 重新啟動
> .\.venv\Scripts\python.exe -m transcript_bot.web
> ```
>
> 重啟前可用 `netstat -ano | findstr :8765` 取得 PID，再用 `Get-CimInstance Win32_Process -Filter "ProcessId=<PID>" | Select-Object CreationDate` 確認其啟動時間是否早於你的修改時間——若較早，就是「跑在舊程式碼上」。

### 台灣在地化潤稿（專有名詞對照表）

潤稿除了本機 Ollama 的在地化 prompt，還有一份**確定性對照表** `data/glossary.txt`，強制糾正 ASR 誤讀的專有名詞 / 品牌詞（例如 `KKBUS → KKBOX`），不依賴模型判斷。格式為每行一組，使用 `=>`、`->` 或 `→` 分隔，以 `#` 開頭為註解：

```text
# 錯誤寫法 => 正確寫法
KKBUS => KKBOX
KK BUS => KKBOX
```

- 對照表在潤稿「前」與「後」各套用一次：既保護專有名詞不被模型改壞，也糾正模型殘留的誤讀。
- **編輯後下一次轉錄自動生效，不必重啟伺服器**（程式依檔案 mtime 重新載入）。
- 檔案不存在時自動跳過，不影響潤稿。

### 上傳與轉錄

首頁可直接**點選或拖放** `m4a`、`mp3`、`wav`、`ogg`、`webm`、`mp4` 或 `aac` 錄音檔，系統會在本機背景轉錄，**不阻塞網頁操作**。轉錄中會顯示即時進度條與階段標籤（如「transcribing (語音辨識) 42%」），完成後自動刷新會議清單。

若不慎重複上傳或想放棄等待，可隨時點選 **✕ 終止轉錄** 刪除進行中的任務與暫存音檔。伺服器重啟時也會自動清除因異常中斷而殘留的未完成會議。

### 校稿功能

可直接編輯並儲存清理版逐字稿、批次設定主講人名稱、點選原始段落跳至對應音檔時間，並下載目前內容的 TXT 或 Word 檔。

### 批次管理

「最近會議」區塊支援**全選、多選**後一鍵刪除，刪除前會彈出二次確認視窗，防止誤刪。

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

## 假資料範例

公開展示用的逐字稿請只使用不含真實姓名、公司、聯絡方式或會議內容的假資料。可參考 [examples/demo_transcript.txt](examples/demo_transcript.txt)。

## 限制與安全提醒

- 多人同時說話、遠距收音、回音或雜訊，均可能降低分群與轉錄品質。
- Speaker 標籤代表不同聲音群組，不代表系統識別了真實身份。
- 開源版不提供共用的 Telegram、Deepgram、OpenAI 或 Hugging Face API Key；請使用者自行建立與保管。
- 請勿將機密會議資料上傳到未經組織核准的雲端服務。
- 超過 1 小時的錄音會在後端自動切成約 10 分鐘的小段（於靜音處落刀、切口前後留 1.5 秒重疊），平行轉錄後合併成**一份**完整會議紀錄，使用者照常上傳整個音檔即可。相關參數見 `.env` 的 `CHUNK_MAX_SECONDS` / `CHUNK_OVERLAP_SECONDS` / `CHUNK_MAX_WORKERS`。
- 上傳大小上限以「標準化後」為準（`MAX_AUDIO_MB`，預設 200 MB @ 64kbps ≈ 7 小時），標準化前的高音質大檔不會被直接擋掉。

## 授權、貢獻與安全通報

本專案採用 [MIT License](LICENSE)。請參閱 [CONTRIBUTING.md](CONTRIBUTING.md) 與 [SECURITY.md](SECURITY.md)。
