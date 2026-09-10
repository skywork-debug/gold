# GOLD 黃金觀測室

黃金版總經儀表板：**即時金價（Pepperstone 報價）＋今日結論 → 拆解四個推力 → 接下來要盯的數據（時間軸）→ 完整數據表**。

即時金價用 TradingView 嵌入元件載入 `PEPPERSTONE:XAUUSD`（Pepperstone 現貨黃金 CFD），在讀者瀏覽器裡即時更新、不需要 API key；Pepperstone 本身沒有公開的報價 API，這是唯一免費又合法拿到它報價的方式。

- 純靜態網站（`index.html` + `data/data.json`），放 GitHub Pages 就能跑
- GitHub Actions 每天台灣時間 08:30、21:30 自動抓資料、重算判讀、commit 回 repo
- 判讀是規則式（門檻寫在 `scripts/fetch_data.py`），透明、可調、不花 AI 費用
- 所有資料來源免費、免 API key

## 目錄

```
index.html                 網頁（含指標白話字典、判讀規則說明）
data/data.json             抓取結果（Actions 自動更新）
data/calendar.json         數據公布時間表（第 3 段「下一個」讀這裡，每年年初更新一次）
scripts/fetch_data.py      抓資料＋規則判讀
scripts/seed/*.csv         離線測試用的樣本資料
.github/workflows/update.yml   排程
```

## 部署步驟（一次做完）

1. 在 GitHub 建一個新 repo（例如 `gold-watch`），把這個資料夾全部上傳（含 `.github` 隱藏資料夾）。
2. Repo → **Settings → Pages** → Source 選 `Deploy from a branch`，Branch 選 `main` / `(root)`，Save。
3. Repo → **Settings → Actions → General** → Workflow permissions 勾 **Read and write permissions**，Save。（否則 bot 無法 commit）
4. Repo → **Actions** → 左側點「更新黃金觀測室資料」→ **Run workflow**，跑完約 30 秒，`data/data.json` 會更新成即時資料。
5. 打開 `https://<帳號>.github.io/<repo>/` 就能看到。之後每天自動更新，不用再管。

選填：到 https://fred.stlouisfed.org/docs/api/api_key.html 申請免費 FRED API key，存到 Repo → Settings → Secrets → Actions → `FRED_API_KEY`。腳本會自動改走官方 JSON API（更穩定）；沒設也能用 CSV 端點正常跑。

## 本機測試

```bash
python scripts/fetch_data.py --seed     # 離線用樣本資料
python scripts/fetch_data.py            # 線上抓
python -m http.server 8000              # 然後開 http://localhost:8000
```

## 放了哪些數據、為什麼

| 條件 | 指標 | 來源 | 對黃金的邏輯 |
|---|---|---|---|
| ① 降息預期 | CME 30 天聯邦基金期貨（ZQ）隱含利率；備援：2 年期殖利率 − 有效聯邦基金利率 | Yahoo Finance、FRED | 押降息越多 → 抱黃金的機會成本越低 |
| ② 美元走勢 | 廣義美元指數 DTWEXBGS | FRED | 美元弱 → 黃金相對便宜、需求升 |
| ③ 實質利率 | 10 年期 TIPS 殖利率 DFII10、通膨預期 T10YIE | FRED | 黃金最直接的對手；實質利率降 = 最大利多 |
| ④ 避險需求 | WTI／布蘭特油價、VIX、高收益債利差 | FRED | 通膨與地緣風險、市場恐慌 → 避險買盤 |
| 背景 | 黃金期貨 GC=F、10 年／2 年期殖利率、CPI／核心 CPI／PCE／核心 PCE／PPI／非農／失業率 | Yahoo、FRED | 解釋聯準會會不會降息 |

判讀規則：四項各 +1／0／−1，加總 ≥ +2 偏多、≤ −2 偏空、其餘中性。門檻在 `fetch_data.py` 的 `score_rates / score_dollar / score_real_yield / score_risk`。

## 未來可加的資料（需要付費或沒有簡單 API，先列著）

- **央行購金**：World Gold Council 每月 Excel（無 API，可手動更新到 JSON）
- **黃金 ETF 持倉**：SPDR GLD 官網每日 CSV（網址常變，較不穩）
- **CME FedWatch 機率表**：官方 API 要付費；本站用期貨隱含利率替代，方向一致
- **COT 期貨持倉**：CFTC 每週五公布，有公開 CSV，可加「投機淨多單」
- **地緣風險指數 GPR**：matteoiacoviello.com 每月更新 Excel

## 換季注意

`data/calendar.json` 內的時間為台灣時間：美國夏令（3 月中～11 月初）美東 08:30 = 台灣 20:30；冬令 = 21:30。每年 12 月把下一年的 BLS／BEA／FOMC 日期填進去即可。
