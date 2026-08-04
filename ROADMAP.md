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
- [x] 選擇輸出後先請使用者填寫 Speaker 對應範本，再產生檔案
- [x] Speaker 對應範本附上每位 Speaker 的代表發言片段
- [x] 支援 `/latest` 查詢最近一次會議
- [ ] 支援 `/rename` 修改指定會議的主講人名稱
- [x] 支援簡化格式 `Speaker 1 = 主持人` 修改最近一次會議
- [ ] 支援修改 speaker alias 後重新匯出 txt/docx
- [ ] 支援 `/delete <meeting_id>` 刪除會議資料
- [ ] 錯誤訊息不得洩露 API key、token 或敏感路徑
- [ ] Telegram 回覆文字統一繁體中文

### 第三批：輸出格式與會議稿模板

- [ ] 支援原始逐字稿模式
- [ ] 支援清理版逐字稿模式
- [ ] 支援會議紀錄模式
- [ ] 支援決議事項摘要模式
- [ ] 支援 `/mode raw`
- [ ] 支援 `/mode cleaned`
- [ ] 支援 `/mode minutes`
- [ ] 支援 `/mode summary`
- [ ] 匯出檔名包含日期與 meeting id
- [ ] Word 檔加入標題、日期、主講人列表與段落樣式

### 第四批：測試與品質

- [ ] 測試簡體轉繁體
- [ ] 測試本地潤稿清理
- [ ] 測試 speaker label 正規化
- [ ] 測試 speaker alias 套用
- [ ] 測試 Deepgram utterances parser
- [ ] 測試 Deepgram words fallback parser
- [ ] 測試 txt 匯出
- [ ] 測試 docx 匯出
- [ ] 測試缺少環境變數時的錯誤提示
- [ ] 加入 GitHub Actions 跑基本測試

### 第五批：開源文件

- [ ] 清理 README 亂碼與內容結構
- [ ] README 說明本機安裝流程
- [ ] README 說明 Telegram Bot token 取得方式
- [ ] README 說明 Deepgram API key 取得方式
- [ ] README 說明 OpenAI optional polish 設定
- [ ] README 說明資料保存位置與刪除方式
- [ ] README 說明開源版不提供共用 API key
- [ ] 加入 `LICENSE`
- [ ] 加入 `CONTRIBUTING.md`
- [ ] 加入 `SECURITY.md`
- [ ] 加入 demo transcript 時只使用假資料

### 第六批：產品化功能

- [ ] 建立 Web 校稿介面
- [ ] 支援逐段播放音檔與逐字稿對齊
- [ ] 支援逐字稿段落手動編輯
- [ ] 支援主講人名稱批次修改
- [ ] 加入 LINE Bot 入口
- [ ] 加入長音檔切段
- [ ] 加入背景任務佇列
- [ ] 加入雲端檔案儲存，例如 S3 或 Cloudflare R2
- [ ] 支援 webhook / 雲端部署
- [ ] 加入使用者、權限與用量紀錄
- [ ] 加入成本估算
- [ ] 加入 API 錯誤重試機制

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
