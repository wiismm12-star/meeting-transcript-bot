# 專案研發工作清單

本檔案是此專案的主要研發追蹤清單。之後每次使用者驗證目前功能沒問題並要求繼續時，優先依照本清單往下實作。

如果中途有新功能或插單需求，先更新本清單的優先順序與勾選狀態，再繼續開發。

## 開發原則

- 專案未來會開源給不特定使用者使用。
- 不內建任何固定人名、公司名或私人會議資料。
- 不預設辨識真實身份，只做不同聲音的分群。
- 預設輸出 `Speaker 1`、`Speaker 2` 等通用標籤。
- 主講人命名由每位使用者或每場會議自行設定。
- API key、token、音檔、逐字稿與使用者資料不得提交到公開 repo。
- Deepgram 是預設轉錄供應商。
- OpenAI 潤稿是可選功能，預設關閉。

## 下一步優先順序

### 準確度插單：台灣中文與免費替代轉錄

- [x] Deepgram 預設改用台灣繁中 `zh-TW`
- [x] 加入 Gladia 轉錄供應商（免費額度、多人分離、中英混說與自訂詞彙）
- [x] 補齊 Gladia 設定、錯誤訊息與單元測試
- [x] 加入 Deepgram 專有名詞提示詞設定，改善品牌名與站名辨識
- [x] 加入本機 Whisper large-v3 轉錄供應商（faster-whisper、繁中提示與時間戳）
- [x] 支援 Whisper 與 pyannote.audio 組合，兼顧文字辨識與多人分段

### 第一批：開源安全與資料持久化

- [x] 確認 `.env` 未被 git 追蹤
- [x] 確認 `.gitignore` 排除 `.env`、`data/`、`.venv/`、`__pycache__/`、`*.egg-info/`
- [x] 檢查 repo 中沒有 Telegram token、OpenAI key、Deepgram key
- [x] 建立 SQLite 資料庫初始化流程
- [x] 建立 `meetings` 資料表
- [x] 建立 `transcript_segments` 資料表
- [x] 建立 `speaker_aliases` 資料表
- [x] 每次處理音檔時建立唯一 `meeting_id`
- [x] 將逐字稿段落寫入資料庫
- [x] 將 speaker alias 從記憶體改為 SQLite
- [x] Bot 重開後仍保留 speaker alias
- [x] 不同 Telegram user 的資料互相隔離
- [x] 同一使用者不同會議的 speaker alias 可分開管理

### 第二批：Bot 操作能力

- [x] 回覆使用者本次 `meeting_id`
- [x] 逐字稿完成後先詢問使用者要輸出 TXT、DOCX、兩者或不輸出
- [x] 選擇輸出後，依序顯示各 Speaker 的代表片段並直接詢問名稱
- [x] 主講人命名提示提供「跳過」按鈕
- [x] 支援 `/latest` 查詢最近一次會議
- [x] 支援簡化格式 `Speaker 1 = 主持人` 修改最近一次會議
- [x] 暫不實作 `/rename`；目前以輸出前 Speaker 範本與最近一次會議簡化格式處理主講人命名
- [x] 支援修改 speaker alias 後重新匯出 txt/docx
- [x] 匯出後可由使用者輸入不同收件 Email 寄送檔案
- [x] 支援 `/delete <meeting_id>` 刪除會議資料
- [x] 錯誤訊息不得洩露 API key、token 或敏感路徑
- [x] Telegram 回覆文字統一繁體中文
- [x] 使用 Deepgram 最新語者分離模型，自動區分不同聲音
- [x] 保留原始音檔聲道，避免多人語音被轉成單聲道
- [x] 加入 pyannote.audio 本機多人語者分離，並與 Deepgram 中文轉錄對齊
- [x] 加入 Ollama 本機免費潤稿供應商，並保留 OpenAI 作為可選方案
- [x] 本機潤稿無法確認正確性時捨棄該段
- [x] 潤稿輸出檢查並校正常見中文亂碼顯示
- [x] 潤稿保留原段落，僅精確修剪不成句的片段
- [x] 加入已確認的常見語音辨識錯字修正表

### 第三批：輸出格式與會議稿模板

- [x] 支援原始逐字稿模式
- [x] 支援清理版逐字稿模式
- [x] 支援會議紀錄模式
- [x] 會議紀錄模式採保守格式，不自動編造決議
- [x] 支援決議事項摘要模式
- [x] 決議摘要僅擷取原文中明確的行動與確認線索
- [x] 支援 `/mode raw`
- [x] 支援 `/mode cleaned`
- [x] 支援 `/mode minutes`
- [x] 支援 `/mode summary`
- [x] 匯出檔名包含日期與 meeting id
- [x] Word 檔加入標題、日期、主講人列表與段落樣式

### 第四批：測試與品質

- [x] 測試簡體轉繁體
- [x] 測試本地潤稿清理
- [x] 測試 speaker label 正規化
- [x] 測試 speaker alias 套用
- [x] 測試 Deepgram utterances parser
- [x] 測試 Deepgram words fallback parser
- [x] 測試 txt 匯出
- [x] 測試 docx 匯出
- [x] 測試缺少環境變數時的錯誤提示
- [x] 加入 GitHub Actions 跑基本測試

### 第五批：開源文件

- [x] 清理 README 亂碼與內容結構
- [x] README 說明本機安裝流程
- [x] README 說明 Telegram Bot token 取得方式
- [x] README 說明 Deepgram API key 取得方式
- [x] README 說明 OpenAI optional polish 設定
- [x] README 說明資料保存位置與刪除方式
- [x] README 說明開源版不提供共用 API key
- [x] 加入 `LICENSE`（MIT）
- [x] 加入 `CONTRIBUTING.md`
- [x] 加入 `SECURITY.md`
- [x] 加入 demo transcript 時只使用假資料

### 第六批：產品化功能

- [x] 本機 Web 會議工作台：可編輯會議名稱、顯示主講人與時間軸
- [x] 本機 Web 支援逐字稿、會議摘要、待辦事項與會議筆記分頁
- [x] 本機 Web 支援上傳單一錄音檔並建立會議逐字稿
- [x] 建立僅本機使用的 Web 校稿介面（綁定 127.0.0.1，暫不提供登入）
- [x] 支援逐段播放音檔與逐字稿對齊
- [x] 支援逐字稿段落手動編輯
- [x] 支援本機 Web 主講人名稱批次修改
- [x] 本機 Web 支援內嵌修改會議名稱與逐字稿段落，並即時同步主講人別名
- [x] 本機 Web 支援 TXT／Word 逐字稿下載
- [x] 本機 Web 支援音訊播放、逐段跳轉與 10 秒快轉／倒轉
- [x] 保留同一主講人的原始時間片段，並將超長無標點發言切成易讀的 1–2 行區塊
- [x] Windows 轉錄期間的 ffmpeg／ffprobe 背景程序不顯示主控台視窗
- [x] 本機 Web 會議摘要使用本機模型產生標題、概覽與重點條列，並快取於資料庫
- [x] 本機 Web 首頁支援多選、全選與批次刪除會議及本機工作檔
- [x] 本機 Web 工作台左側顯示所有會議紀錄，可直接切換目前會議
- [x] 上傳或轉錄失敗時自動清理未完成的本機會議資料
- [ ] 完成 LINE Bot 公開 HTTPS webhook 實測
- [ ] 將 LINE 音檔接入背景轉錄工作佇列
- [x] 建立 LINE Bot 簽章驗證、文字／音檔連線回覆與本機單元測試
- [ ] 加入長音檔切段
- [ ] 加入背景任務佇列
- [ ] 加入雲端檔案儲存，例如 S3 或 Cloudflare R2
- [ ] 支援 webhook / 雲端部署
- [ ] 加入使用者、權限與用量紀錄
- [ ] 加入成本估算
- [ ] 加入 API 錯誤重試機制

### 最後驗證：辨識準確度基準比較

- [ ] 以同一批台灣中文音檔比較 Deepgram 與 Gladia 的辨識結果
- [ ] 以同一批台灣中文音檔比較 Deepgram 與本機 Whisper large-v3 的辨識結果

## 已完成基礎項目

- [x] 建立 Telegram Bot 專案骨架
- [x] 支援 Telegram polling，本機啟動即可收訊息
- [x] 接收手機語音訊息、音檔與音訊文件
- [x] 下載 Telegram 音檔到本機工作目錄
- [x] 使用 ffmpeg 轉成單聲道 mp3
- [x] 串接 Deepgram Speech-to-Text
- [x] 啟用 Deepgram speaker diarization 主講人分離
- [x] 將主講人整理成 `Speaker 1`、`Speaker 2`
- [x] 支援 `Speaker 1 = 主持人` 這類主講人名稱對應
- [x] 產出 `.txt` 文字稿
- [x] 產出 `.docx` Word 文字稿
- [x] 回傳文字預覽與附件到 Telegram
- [x] 將逐字稿統一轉為繁體中文
- [x] 加入本地潤稿清理：移除多餘空白、重複標點與明顯語助詞
- [x] 保留 OpenAI 潤飾為可選功能，預設關閉

## 插單處理規則

1. 如果新需求是 bug fix，優先處理，完成後更新本清單。
2. 如果新需求會影響架構，先把它插入對應批次，再開始實作。
3. 如果新需求只是小功能，加入目前批次或下一批次。
4. 每次完成一項功能，要同步更新勾選狀態。
5. 使用者說「驗證沒問題，繼續」時，從「下一步優先順序」第一個未完成項目開始。
