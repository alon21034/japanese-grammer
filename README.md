# Japanese Grammar Daily LINE Bot

每天以本地爬下來的 NHK Easy 新聞為主體，透過離線生成文法講解與練習題；LINE 推播預設為「今日一句 + 搭配文法」精簡模式。

## 功能
- 使用本地 `data/nhk_easy` 文章資料（由爬蟲同步）。
- 每日選擇尚未發送的下一篇 NHK 新聞（全部發完後自動循環）。
- 透過離線模板生成 1~3 個文法講解（繁中）與 1~3 題練習。
- 先從本地 `data/crawl/articles/*.json` 做 Hybrid RAG 檢索，文法文章 RAG 流程維持不變。
- 同步 NHK 新聞時，會一併離線預先生成每個文法點的詳細解釋內容。
- 題型包含易混淆文法選擇題與短答日文題（翻譯、填空）。
- LINE webhook（`今日文法`）預設回覆一句新聞 + 搭配文法。
- 完整逐題練習模式可在本地 `local-ui` 測試。
- 透過 LINE Messaging API push 到你手機。
- 透過 webhook 自動註冊訂閱者（加好友後發訊息即可）。

## 1) 建立 LINE Channel
1. 到 [LINE Developers Console](https://developers.line.biz/) 建立 `Messaging API` Channel。
2. 取得：
   - `Channel access token`
   - `Channel secret`
3. 在 Messaging API 設定 webhook URL：`https://你的網域/callback`
4. 開啟 `Use webhook`。

## 2) 安裝與設定
```bash
cd /Users/vince.lee/Documents/workspaces/japanese-grammer
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
```

編輯 `.env`：
```dotenv
LINE_CHANNEL_ACCESS_TOKEN=...
LINE_CHANNEL_SECRET=...
# 可選：只推播到單一 user id
LINE_USER_ID=Uxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx
LOCAL_TEST_MODE=0
LOCAL_TEST_USER_ID=Ulocaltest
DATA_DIR=./data
```

## 3) 啟動 webhook server
```bash
cd /Users/vince.lee/Documents/workspaces/japanese-grammer
source .venv/bin/activate
python scripts/run_webhook.py --host 0.0.0.0 --port 8000
```

預設會啟用 hot reload；若要關閉可加 `--no-reload`。

## 4) 註冊你的手機為訂閱者
1. 將 LINE Official Account 加好友。
2. 傳任意訊息給 bot（或傳 `今日文法`）。
3. `data/subscribers.json` 會自動記錄 `userId`。
4. 若要看含平假名標註版本，可輸入：`今日文法 假名` 或 `今日文法 ふりがな`（會用獨立對照 section 顯示，不夾在原句中）。

## 4.1) 本地測試模式（不打 LINE API）
本地開發可用：
- 略過 LINE webhook 簽章驗證
- 不呼叫 LINE push/reply API，改寫入 `data/line_mock_events.jsonl`
- 完全離線生成課程與文法詳細解釋，不呼叫 OpenAI API

設定 `.env`：
```dotenv
LOCAL_TEST_MODE=1
LOCAL_TEST_USER_ID=Ulocaltest
```

啟動：
```bash
cd /Users/vince.lee/Documents/workspaces/japanese-grammer
source .venv/bin/activate
python scripts/run_webhook.py --host 127.0.0.1 --port 8000
```

瀏覽器互動版：
- 開啟 `http://127.0.0.1:8000/local-ui`
- 按「開始閱讀」先逐條顯示「文法庫對照＋例句」
- 按「下一句 / 上一句」由使用者決定何時前進或回看
- 按「詳細解釋」只會針對目前這一條文法顯示離線詳細說明
- 「詳細解釋」具備快取，重複相同文法時會直接使用快取避免重複計算
- 逐句模式只顯示新聞句子（不重複顯示搭配文法）
- 每句會額外顯示「漢字標註」section（漢字+讀音）
- 可切換「顯示平假名標註」

用 curl 模擬 LINE webhook：
```bash
curl -sS -X POST http://127.0.0.1:8000/callback \
  -H 'Content-Type: application/json' \
  -d '{"events":[{"type":"message","replyToken":"local-1","source":{"userId":"Ulocaltest"},"message":{"type":"text","text":"今日文法"}}]}'
```

查看 bot 回覆（mock）：
```bash
tail -n 20 data/line_mock_events.jsonl
```

## 5) 手動測試推播
```bash
cd /Users/vince.lee/Documents/workspaces/japanese-grammer
source .venv/bin/activate
PYTHONPATH=src python -m jp_daily_line_bot.daily_job
```

前提：先執行 `scripts/sync_nhk_easy.py`，確保 `data/nhk_easy/index.json` 與 `articles/*.json` 已存在。

## 5.1) 文法資料同步（預設只抓新資料）
```bash
cd /Users/vince.lee/Documents/workspaces/japanese-grammer
source .venv/bin/activate
python scripts/sync_grammar.py
```

若你要「全量重檢查」來源上的所有文法文章，請加：
```bash
python scripts/sync_grammar.py --full-check
```

輸出：
- `data/crawl/articles/*.json`：每篇文法內容（結構化）
- `data/crawl/manifest.json`：每個 URL 的 hash 與追蹤資訊
- `data/crawl/index.json`：本次來源中存在的文章索引
- `data/crawl/last_run.json`：本次爬蟲摘要（new/updated/unchanged/failed）

之後網站更新時，重跑同一支腳本即可：
```bash
cd /Users/vince.lee/Documents/workspaces/japanese-grammer
source .venv/bin/activate
python scripts/sync_grammar.py
```

可選參數：
- `--max 100`：只抓前 100 篇（測試用）
- `--delay 0.2`：每篇延遲秒數
- `--force`：即使沒變更也重寫檔案
- `--new-only`：只抓 manifest 裡還沒出現的新網址（預設）
- `--full-check`：重抓來源上的全部網址並比對變更

## 5.2) NHK Easy 新聞同步（預設只抓新資料）
```bash
cd /Users/vince.lee/Documents/workspaces/japanese-grammer
source .venv/bin/activate
python scripts/sync_nhk_easy.py
```

若你要「全量重檢查」來源上的全部新聞，請加：
```bash
python scripts/sync_nhk_easy.py --full-check
```

輸出：
- `data/nhk_easy/articles/*.json`：每篇新聞內容（標題、全文、假名版全文、原始 HTML）
- `data/nhk_easy/manifest.json`：每個 `news_id` 的 hash 與追蹤資訊
- `data/nhk_easy/index.json`：本次來源中存在的新聞索引
- `data/nhk_easy/last_run.json`：本次爬蟲摘要（new/updated/unchanged/failed）

可選參數：
- `--max 10`：只抓前 10 篇（測試用）
- `--delay 0.2`：每篇延遲秒數
- `--force`：即使沒變更也重寫檔案
- `--new-only`：只抓 manifest 裡還沒出現的新新聞（預設）
- `--full-check`：重抓來源上的全部新聞並比對變更

## 5.3) 只用本地資料重建文法對照與離線詳細解釋（不爬新聞）
當你不想重新爬 NHK 網頁，只想對現有 `data/nhk_easy/articles/*.json` 回填或重建：

```bash
cd /Users/vince.lee/Documents/workspaces/japanese-grammer
source .venv/bin/activate
python scripts/enrich_nhk_easy.py
```

預設 `--missing-only`：只處理缺少 `grammar_references` 或 `offline_detailed_explanations` 的檔案。  
若你要全部重算，請加：

```bash
python scripts/enrich_nhk_easy.py --all
```

## 6) 設定每天自動推播（cron）
以下範例為每天早上 08:00（Asia/Tokyo）執行：

```bash
crontab -e
```

加入：
```cron
0 8 * * * cd /Users/vince.lee/Documents/workspaces/japanese-grammer && /bin/zsh -lc 'source .venv/bin/activate && PYTHONPATH=src python -m jp_daily_line_bot.daily_job >> /tmp/jp_daily_line_bot.log 2>&1'
```

## 資料檔
- `data/subscribers.json`：已註冊的 LINE user IDs
- `data/nhk_progress.json`：已發送 NHK `news_id` 紀錄
- `data/quiz_state.json`：每位使用者待作答題目狀態

## 注意事項
- 請遵守來源網站的使用規範與著作權，建議僅推送重點與原文連結。
- LINE push 需要對方先加好友（且帳號可被推播）。
