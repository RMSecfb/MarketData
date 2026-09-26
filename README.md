# MarketData｜金融市場每日監控儀表板

本專案為風險管理使用之金融市場每日監控工具，自動蒐集主要市場風險指標、美國公債殖利率及重點個股行情，並透過網頁儀表板提供每日監控、歷史趨勢、人工資料維護及警示門檻管理。

系統以 GitHub 作為程式及資料儲存來源，透過 GitHub Actions
每日自動更新市場資料，並由 Cloudflare Pages 提供網頁介面。

https://marketdata-dga.pages.dev/

------------------------------------------------------------------------

## 1. 系統功能

### 📊 每日監控

每日監控項目包含：

-   VIX 恐慌指數
-   MOVE 債券波動率指數
-   美國 2 年期公債殖利率
-   美國 10 年期公債殖利率
-   美國 30 年期公債殖利率
-   10Y－2Y 利差
-   費城半導體指數（SOX）
-   主要監控個股
-   主要國家 5 年期 CDS

系統依 `config.json`
所設定之警示門檻呈現綠／黃／紅燈號，並產生每日市場摘要。

### 📈 趨勢圖

可查詢各市場指標及監控個股之歷史走勢，資料來源為 `data/`
目錄內每日留存之市場資料。

### ✏️ 人工維護

支援人工維護：

-   VIX
-   MOVE
-   2Y / 10Y / 30Y 美國公債殖利率
-   SOX
-   個股行情
-   各國 CDS

一般市場指標人工調整資料儲存於：

`data/manual_override.json`

CDS 則直接寫入對應日期之：

`data/market_YYYY-MM-DD.json`

### ⚙️ 系統設定

`config.json` 用於管理：

-   各項市場指標警示門檻
-   監控個股
-   個股名稱及風險等級
-   Dashboard 顯示相關設定

------------------------------------------------------------------------

## 2. 系統架構

``` text
MarketData/
├─ .github/
│  └─ workflows/
│     └─ fetch_market.yml        # GitHub Actions 每日排程
├─ data/
│  ├─ latest.json                # 最新市場資料
│  ├─ market_YYYY-MM-DD.json     # 每日歷史市場資料
│  └─ manual_override.json       # 人工維護資料
├─ functions/
│  ├─ _lib/
│  │  └─ github.js               # GitHub API 共用功能
│  └─ api/
│     └─ github-write.js         # Cloudflare 寫入 API
├─ config.json                   # 警示門檻及監控標的設定
├─ fetch_market.py               # 市場資料抓取主程式
├─ index.html                    # Dashboard 前端
└─ README.md
```

### 市場資料流程

``` text
市場資料來源
    ↓
fetch_market.py
    ↓
GitHub Actions
    ↓
data/market_YYYY-MM-DD.json
    ↓
GitHub Repository
    ↓
Cloudflare Pages
    ↓
金融市場監控 Dashboard
```

### 人工資料維護流程

``` text
Dashboard
    ↓
Cloudflare Pages Function
/api/github-write
    ↓
Cloudflare Secret：GITHUB_TOKEN
    ↓
GitHub API
    ↓
MarketData Repository
```

GitHub Token 不儲存於瀏覽器端或 `index.html`。

------------------------------------------------------------------------

## 3. 每日自動排程

GitHub Actions 目前於**台灣時間每週二至週六**執行：

  台灣時間   UTC     說明
  ---------- ------- ----------------------
  06:00      22:00   第一次抓取
  06:30      22:30   資料不完整時再次嘗試
  07:00      23:00   最後一次嘗試

排程設定於：

`.github/workflows/fetch_market.yml`

系統會先依 NYSE 交易日曆判斷最近一個已完成之交易日。

若第一次執行後，所有必要市場資料皆已取得 TARGET DATE
同日有效值，後續排程將自動跳過。

若仍存在 stale / missing 資料，後續排程會再次嘗試更新。

------------------------------------------------------------------------

## 4. 非交易日與資料日期

系統區分：

-   `target_date`：報表日期
-   `date`：該項市場資料實際所屬日期

若 TARGET DATE
無法取得該項市場資料，系統可使用最近一期已確認之市場資料，並保留實際資料日期及
stale 狀態。

例如：

``` json
{
  "target_date": "2026-09-05",
  "vix": {
    "value": 14.53,
    "date": "2026-09-04",
    "is_stale": true,
    "fallback_reason": "target_date_unavailable"
  }
}
```

Dashboard 會另外揭露非同日資料，避免將前一交易日資料誤認為 TARGET DATE
當日行情。

------------------------------------------------------------------------

## 5. 手動補抓市場資料

除每日排程外，GitHub Actions 支援手動指定日期重新執行。

操作方式：

1.  進入 GitHub Repository。
2.  點選 `Actions`。
3.  選擇 `Fetch Market Data Daily`。
4.  點選 `Run workflow`。
5.  於 `target_date` 輸入指定日期，格式為 `YYYY-MM-DD`。
6.  執行 Workflow。

手動執行時會強制重新抓取指定日期資料。

> **注意：** 如同一日期尚需人工維護
> CDS，現行作業建議先完成市場資料補抓並確認資料，再進行 CDS 維護。

------------------------------------------------------------------------

## 6. CDS 人工維護

CDS 目前採人工維護方式。

監控國家：

-   🇺🇸 美國
-   🇰🇷 南韓
-   🇯🇵 日本
-   🇨🇳 中國
-   🇭🇰 香港

操作方式：

1.  Dashboard 選擇欲維護日期。
2.  於主要國家 CDS 區域輸入資料。
3.  點選儲存。
4.  系統透過 Cloudflare Pages Function 寫入 GitHub。

CDS 寫入對應日期：

`data/market_YYYY-MM-DD.json`

系統可依前期資料計算變化，供 Dashboard 顯示。

------------------------------------------------------------------------

## 7. 主要資料來源

目前市場資料主要透過以下來源取得：

  資料             主要來源
  ---------------- ---------------
  VIX              Yahoo Finance
  MOVE             Yahoo Finance
  SOX              Yahoo Finance
  個股             Yahoo Finance
  美國公債殖利率    U.S. Treasury
  CDS              人工維護

資料來源或第三方服務可能因網站/API
調整、更新時間或交易日差異而發生短暫異常，因此系統保留 stale fallback
及人工維護機制。

------------------------------------------------------------------------

## 8. Cloudflare Pages

Dashboard 透過 Cloudflare Pages 發布，並與本 GitHub Repository 的 `main`
branch 連動。

前端需要寫入 GitHub 時，不直接使用 GitHub Personal Access
Token，而是呼叫：

`/api/github-write`

再由 Cloudflare Pages Function 使用 Cloudflare Secret：

`GITHUB_TOKEN`

執行 GitHub Repository 寫入。

### GITHUB_TOKEN 權限

Token 原則上僅授權本專案所需 Repository 及必要權限：

-   Repository：`MarketData`
-   Contents：Read and write

請勿將 Token 寫入：

-   `index.html`
-   JavaScript
-   GitHub Repository
-   公開 Gist
-   瀏覽器 localStorage

------------------------------------------------------------------------

## 9. 日常維護原則

一般情況下無需人工啟動市場資料抓取。

若發現資料異常，可依序確認：

1.  GitHub Actions 是否正常完成。
2.  `data/market_YYYY-MM-DD.json` 是否已產生或更新。
3.  各指標 `date` 是否與 `target_date` 一致。
4.  是否存在 `is_stale` 或 missing 狀態。
5.  Cloudflare Pages 是否已完成較新的 Deployment。

若 Cloudflare 單一 Deployment 長時間停留於 deploy 階段，但 GitHub
資料已正常 Commit，可進一步確認後續較新的 Deployment 是否成功。

較新的成功 Deployment 會包含當時 `main` branch
已存在的檔案，因此歷史資料補登時，重點應確認 GitHub
資料及最新網站版本是否正確。

------------------------------------------------------------------------

## 10. 修改注意事項

### 修改監控標的或門檻

優先調整：

`config.json`

避免將可設定參數直接寫死於 `index.html`。

### 修改市場資料抓取方式

主要程式：

`fetch_market.py`

修改後應確認：

-   TARGET DATE 判斷
-   NYSE 交易日判斷
-   非交易日處理
-   stale fallback
-   個別資料來源異常處理
-   `data_quality` 狀態
-   `latest.json` 與每日歷史資料是否正常產生

### 修改 Dashboard

主要檔案：

`index.html`

修改後至少應確認：

-   每日監控
-   日期切換
-   市場摘要
-   非同日資料揭露
-   趨勢圖
-   CDS 維護
-   人工維護
-   設定頁

### 修改 Cloudflare 寫入功能

主要檔案：

-   `functions/api/github-write.js`
-   `functions/_lib/github.js`

修改後應確認：

-   CDS 寫入
-   人工維護資料寫入
-   設定資料寫入
-   GitHub Repository 寫入權限
-   Cloudflare Secret 是否正常

------------------------------------------------------------------------

## 11. 主要檔案說明

  --------------------------------------------------------------------------
  檔案                                   用途
  -------------------------------------- -----------------------------------
  `index.html`                           Dashboard 前端主程式

  `fetch_market.py`                      市場資料抓取及整理

  `config.json`                          警示門檻、監控個股及顯示設定

  `.github/workflows/fetch_market.yml`   每日自動排程及 GitHub Actions 流程

  `data/latest.json`                     最新市場資料

  `data/market_YYYY-MM-DD.json`          每日歷史市場資料

  `data/manual_override.json`            市場資料人工維護紀錄

  `functions/api/github-write.js`        Dashboard 寫入 GitHub 的 Cloudflare
                                         API

  `functions/_lib/github.js`             GitHub API 共用功能
  --------------------------------------------------------------------------

------------------------------------------------------------------------

## 12. 異常處理快速檢查

### Dashboard 沒有最新資料

依序確認：

``` text
GitHub Actions
    ↓
market_YYYY-MM-DD.json
    ↓
latest.json
    ↓
Cloudflare Deployment
    ↓
Dashboard
```

### 部分市場資料日期較舊

確認該項目：

-   `date`
-   `is_stale`
-   `fallback_reason`

若為資料來源尚未更新，後續 06:30 / 07:00 排程仍會再次嘗試取得 TARGET
DATE 資料。

### CDS 無法儲存

確認：

1.  Cloudflare Pages Function 是否正常。
2.  Cloudflare `GITHUB_TOKEN` Secret 是否存在。
3.  Token 是否仍有效。
4.  Token 是否具 `MarketData` Repository 的 Contents Read and write權限。
5.  GitHub Repository 是否有正常收到 Commit。

------------------------------------------------------------------------

## 13. 使用目的與資料說明

本系統供內部市場風險監控及管理參考使用。
市場資料可能受到資料來源更新時間、交易日差異、第三方服務異常等因素影響。
Dashboard透過資料日期揭露、stale 標示及人工維護機制協助辨識資料狀態。

如涉及正式風險控管、交易決策或對外資訊使用，仍應依公司正式資料來源及相關作業規範辦理。
