#!/usr/bin/env python3
"""
黃金觀測站 — 資料抓取與規則式判讀
=================================
用法：
    python scripts/fetch_data.py            # 線上抓取（FRED CSV ＋ Yahoo Finance），輸出 data/data.json
    python scripts/fetch_data.py --seed     # 離線模式：讀 scripts/seed/*.csv（開發、測試用）

資料來源（都免費、免 API key）：
    FRED  https://fred.stlouisfed.org/graph/fredgraph.csv?id=<SERIES>&cosd=<YYYY-MM-DD>
          （若有設定環境變數 FRED_API_KEY，會改走官方 JSON API，較穩定）
    Yahoo https://query1.finance.yahoo.com/v8/finance/chart/<SYMBOL>?range=6mo&interval=1d
          黃金期貨 GC=F、30 天聯邦基金期貨 ZQ<月碼><年>.CBT

輸出 data/data.json，前端 index.html 直接讀取。
"""
from __future__ import annotations

import argparse
import csv
import io
import json
import os
import sys
import time
import urllib.parse
import urllib.request
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SEED_DIR = ROOT / "scripts" / "seed"
DATA_DIR = ROOT / "data"
TAIPEI = timezone(timedelta(hours=8))

UA = {"User-Agent": "Mozilla/5.0 (compatible; gold-macro-dashboard/1.0)"}

# ---------------------------------------------------------------------------
# 指標設定
# ---------------------------------------------------------------------------
# kind: "level" 直接用數值；"index_mom" 由指數水準換算「季調月增率 %」；"diff" 由水準換算「月增（千人）」
ECON_SERIES = [
    dict(id="CPIAUCSL", name="CPI 整體物價", kind="index_mom", unit="季調月增率 · %", dec=2, diff_unit="pp"),
    dict(id="CPILFESL", name="核心 CPI", kind="index_mom", unit="季調月增率 · %", dec=2, diff_unit="pp"),
    dict(id="PCEPI", name="PCE 整體物價", kind="index_mom", unit="季調月增率 · %", dec=2, diff_unit="pp"),
    dict(id="PCEPILFE", name="核心 PCE", kind="index_mom", unit="季調月增率 · %", dec=2, diff_unit="pp"),
    dict(id="PPIFIS", name="PPI 最終需求", kind="index_mom", unit="季調月增率 · %", dec=2, diff_unit="pp"),
    dict(id="PAYEMS", name="非農新增就業", kind="diff", unit="月資料 · 千人", dec=0, diff_unit=""),
    dict(id="UNRATE", name="失業率", kind="level", unit="月資料 · %", dec=1, diff_unit="pp"),
]

MARKET_SERIES = [
    dict(id="GC=F", src="yahoo", rng="2y", name="黃金期貨（COMEX）", unit="美元／盎司", dec=1, diff_unit="美元", pct=True),
    dict(id="DX-Y.NYB", src="yahoo", rng="2y", name="美元指數 DXY", unit="點", dec=2, diff_unit="點", pct=True),
    dict(id="DFII10", src="fred", name="10 年期實質殖利率", unit="%", dec=2, diff_unit="bp"),
    dict(id="DGS10", src="fred", name="10 年期美債殖利率", unit="%", dec=2, diff_unit="bp"),
    dict(id="DGS2", src="fred", name="2 年期美債殖利率", unit="%", dec=2, diff_unit="bp"),
    dict(id="T10YIE", src="fred", name="10 年期通膨預期（損益兩平）", unit="%", dec=2, diff_unit="bp"),
    dict(id="DTWEXBGS", src="fred", name="廣義美元指數", unit="點", dec=2, diff_unit="點", pct=True),
    dict(id="DCOILWTICO", src="fred", name="WTI 原油", unit="美元／桶", dec=2, diff_unit="美元", pct=True),
    dict(id="DCOILBRENTEU", src="fred", name="布蘭特原油", unit="美元／桶", dec=2, diff_unit="美元", pct=True),
    dict(id="VIXCLS", src="fred", name="VIX 恐慌指數", unit="點", dec=2, diff_unit="點"),
    dict(id="BAMLH0A0HYM2", src="fred", name="高收益債信用利差", unit="%", dec=2, diff_unit="bp"),
]

AUX_FRED = ["DFF", "DFEDTARU", "DFEDTARL"]  # 有效聯邦基金利率、目標區間上下限

FUT_MONTH_CODE = {1: "F", 2: "G", 3: "H", 4: "J", 5: "K", 6: "M", 7: "N", 8: "Q", 9: "U", 10: "V", 11: "X", 12: "Z"}


# ---------------------------------------------------------------------------
# 抓資料
# ---------------------------------------------------------------------------
def http_get(url: str, retries: int = 2, timeout: int = 12) -> str:
    """短逾時、少重試：任何來源卡住最多拖 ~30 秒，不會讓整個工作掛死。"""
    last = None
    t0 = time.time()
    for i in range(retries):
        try:
            req = urllib.request.Request(url, headers=UA)
            with urllib.request.urlopen(req, timeout=timeout) as r:
                body = r.read().decode("utf-8", "replace")
            print(f"[get] {url.split('?')[0]} ok {time.time()-t0:.1f}s", flush=True)
            return body
        except Exception as e:  # noqa: BLE001
            last = e
            print(f"[get] {url.split('?')[0]} fail#{i+1}: {e}", flush=True)
            time.sleep(1)
    raise RuntimeError(f"GET failed {url}: {last}")


def parse_csv(text: str) -> list[tuple[str, float]]:
    rows = []
    for r in csv.reader(io.StringIO(text)):
        if len(r) < 2 or r[0] in ("observation_date", "DATE"):
            continue
        try:
            rows.append((r[0], float(r[1])))
        except ValueError:
            continue  # "." = 缺值（假日）
    return rows


def fetch_fred(series: str, start: str, seed: bool) -> list[tuple[str, float]]:
    if seed:
        p = SEED_DIR / f"{series}.csv"
        return parse_csv(p.read_text()) if p.exists() else []
    key = os.environ.get("FRED_API_KEY")
    if key:
        url = ("https://api.stlouisfed.org/fred/series/observations?"
               + urllib.parse.urlencode(dict(series_id=series, api_key=key, file_type="json", observation_start=start)))
        j = json.loads(http_get(url))
        out = []
        for o in j.get("observations", []):
            try:
                out.append((o["date"], float(o["value"])))
            except ValueError:
                pass
        return out
    # 沒有 API key 時走 fredgraph.csv（GitHub Actions 的機房 IP 偶爾會被 FRED 擋，建議設定 FRED_API_KEY）
    url = f"https://fred.stlouisfed.org/graph/fredgraph.csv?id={series}&cosd={start}"
    return parse_csv(http_get(url))


def fetch_yahoo(symbol: str, rng: str, seed: bool, interval: str = "1d") -> list[tuple[str, float]]:
    if seed:
        suffix = "" if interval == "1d" else f"_{interval}"
        p = SEED_DIR / (symbol.replace("=", "_").replace(".", "_") + suffix + ".csv")
        return parse_csv(p.read_text()) if p.exists() else []
    url = f"https://query1.finance.yahoo.com/v8/finance/chart/{urllib.parse.quote(symbol)}?range={rng}&interval={interval}"
    try:
        j = json.loads(http_get(url))
    except Exception as e:  # noqa: BLE001
        if symbol == "GC=F":
            print(f"[warn] Yahoo GC=F 失敗（{e}），改用 stooq 現貨黃金 XAUUSD", flush=True)
            return fetch_stooq("xauusd")
        raise
    res = (j.get("chart") or {}).get("result") or []
    if not res:
        return []
    r = res[0]
    ts = r.get("timestamp") or []
    close = r["indicators"]["quote"][0].get("close") or []
    out = []
    for t, c in zip(ts, close):
        if c is None:
            continue
        d = datetime.fromtimestamp(t, tz=timezone.utc).date().isoformat()
        if interval != "1d":
            d = d[:7] + "-01"
        out.append((d, float(c)))
    # 同一天保留最後一筆
    dedup = {}
    for d, v in out:
        dedup[d] = v
    return sorted(dedup.items())


def fetch_stooq(symbol: str) -> list[tuple[str, float]]:
    """stooq 免費日線 CSV（Date,Open,High,Low,Close,Volume）。黃金現貨代碼 xauusd。"""
    text = http_get(f"https://stooq.com/q/d/l/?s={symbol}&i=d")
    out = []
    for r in csv.reader(io.StringIO(text)):
        if len(r) >= 5 and r[0][:1].isdigit():
            try:
                out.append((r[0], float(r[4])))
            except ValueError:
                pass
    return out[-140:]


# ---------------------------------------------------------------------------
# 計算工具
# ---------------------------------------------------------------------------
def r(x, n=2):
    return None if x is None else round(x, n)


def series_stats(rows, dec, diff_unit, pct=False):
    """把時間序列整理成：最新、前期、差、5 期、20 期變化、近 24 期 sparkline。"""
    if not rows:
        return None
    vals = [v for _, v in rows]
    dates = [d for d, _ in rows]
    latest, prev = vals[-1], (vals[-2] if len(vals) > 1 else None)

    def delta(k):
        return (vals[-1] - vals[-1 - k]) if len(vals) > k else None

    def fmt_delta(dv):
        if dv is None:
            return None
        if diff_unit == "bp":
            return f"{dv*100:+.0f} bp"
        if diff_unit == "pp":
            return f"{dv:+.{dec}f} pp"
        if diff_unit == "":
            return f"{dv:+.0f}"
        return f"{dv:+.{dec}f} {diff_unit}"

    def fmt_pct(k):
        if len(vals) <= k or vals[-1 - k] == 0:
            return None
        return f"{(vals[-1]/vals[-1-k]-1)*100:+.2f}%"

    return dict(
        latest=r(latest, dec), latest_date=dates[-1],
        prev=r(prev, dec), prev_date=dates[-2] if prev is not None else None,
        chg=fmt_delta(delta(1)), chg5=fmt_delta(delta(5)), chg20=fmt_delta(delta(20)),
        chg5_pct=fmt_pct(5) if pct else None, chg20_pct=fmt_pct(20) if pct else None,
        raw_chg5=r(delta(5), 4), raw_chg20=r(delta(20), 4),
        spark=[r(v, 4) for v in vals[-24:]],
        spark_dates=dates[-24:],
    )


def index_to_mom(rows):
    out = []
    for i in range(1, len(rows)):
        (d0, v0), (d1, v1) = rows[i - 1], rows[i]
        # 跳過月份不連續（例如 2025-10 CPI 因政府停擺缺報）
        m0 = int(d0[:4]) * 12 + int(d0[5:7])
        m1 = int(d1[:4]) * 12 + int(d1[5:7])
        if m1 - m0 != 1:
            continue
        out.append((d1, (v1 / v0 - 1) * 100))
    return out


def index_to_diff(rows):
    return [(rows[i][0], rows[i][1] - rows[i - 1][1]) for i in range(1, len(rows))]


def annualized_3m(rows):
    """近三個月年化（用指數水準）。"""
    if len(rows) < 4:
        return None
    return ((rows[-1][1] / rows[-4][1]) ** 4 - 1) * 100


def yoy(rows):
    if len(rows) < 13:
        return None
    return (rows[-1][1] / rows[-13][1] - 1) * 100


# ---------------------------------------------------------------------------
# 降息／升息預期（聯邦基金期貨）
# ---------------------------------------------------------------------------
def fed_expectations(today: date, mid: float | None, eff: float | None, dgs2: float | None, seed: bool):
    """
    用 CME 30 天聯邦基金期貨（Yahoo: ZQ<月碼><yy>.CBT）估算市場對未來政策利率的預期。
    隱含利率 = 100 − 期貨價格。與目前目標區間中點比較，正數 = 預期升息、負數 = 預期降息。
    找不到期貨時退而用「2 年期殖利率 − 有效聯邦基金利率」當代理指標。
    """
    horizons = []
    dec_year = today.year
    contracts = [("年底", date(dec_year, 12, 1)), ("明年年中", date(dec_year + 1, 6, 1))]
    if today.month == 12:
        contracts = [("明年年中", date(dec_year + 1, 6, 1)), ("明年年底", date(dec_year + 1, 12, 1))]
    for label, d in contracts:
        sym = f"ZQ{FUT_MONTH_CODE[d.month]}{str(d.year)[2:]}.CBT"
        try:
            rows = fetch_yahoo(sym, "1mo", seed)
        except Exception as e:  # noqa: BLE001
            print(f"[warn] {sym}: {e}", file=sys.stderr)
            rows = []
        if rows and mid is not None:
            implied = 100 - rows[-1][1]
            horizons.append(dict(label=label, symbol=sym, date=rows[-1][0], price=r(rows[-1][1], 3),
                                 implied=r(implied, 2), vs_now_bp=r((implied - mid) * 100, 0)))
    proxy = None
    if dgs2 is not None and eff is not None:
        proxy = r((dgs2 - eff) * 100, 0)
    return dict(target_mid=mid, effective=eff, horizons=horizons, proxy_2y_minus_ff_bp=proxy)


# ---------------------------------------------------------------------------
# 規則式判讀
# ---------------------------------------------------------------------------
def score_rates(fedx):
    """降息預期：到年底的隱含變動。≤ −25bp 支持黃金；≥ +25bp 壓力。"""
    h = fedx["horizons"][0] if fedx["horizons"] else None
    if h is None:
        p = fedx.get("proxy_2y_minus_ff_bp")
        if p is None:
            return 0, "資料不足", "無法取得期貨資料"
        bp = p
        src = f"2 年期殖利率 − 有效聯邦基金利率 = {bp:+.0f} bp（代理指標）"
    else:
        bp = h["vs_now_bp"]
        src = f"聯邦基金期貨隱含{h['label']}利率 {h['implied']:.2f}%，較目前中點 {fedx['target_mid']:.3f}% 差 {bp:+.0f} bp"
    if bp <= -25:
        return 1, "市場在押降息", src
    if bp >= 25:
        return -1, "市場在押升息", src
    return 0, "市場沒押明顯方向", src


def score_dollar(st, label="美元指數 DXY"):
    """美元：DXY（抓不到時用廣義美元指數）20 期變化。跌 ≥0.5% 支持黃金；漲 ≥0.5% 壓力。"""
    if not st or st.get("raw_chg20") is None:
        return 0, "資料不足", ""
    base = st["spark"][-21] if len(st["spark"]) >= 21 else None
    pct = (st["raw_chg20"] / base * 100) if base else 0
    src = f"{label} {st['latest']:.2f}，20 個交易日變化 {st['chg20']}（{pct:+.2f}%）"
    if pct <= -0.5:
        return 1, "美元轉弱", src
    if pct >= 0.5:
        return -1, "美元走強", src
    return 0, "美元持平", src


def score_real_yield(st_real, st_bei):
    """實質利率：10 年期實質殖利率 20 期變化。降 ≥10bp 支持；升 ≥10bp 壓力。"""
    if not st_real or st_real.get("raw_chg20") is None:
        return 0, "資料不足", ""
    bp = st_real["raw_chg20"] * 100
    src = f"10 年期實質殖利率 {st_real['latest']:.2f}%，20 個交易日變化 {bp:+.0f} bp"
    if st_bei and st_bei.get("chg20"):
        src += f"；通膨預期 20 日 {st_bei['chg20']}"
    if bp <= -10:
        return 1, "實質利率下降", src
    if bp >= 10:
        return -1, "實質利率上升", src
    return 0, "實質利率持平", src


def score_risk(st_wti, st_vix, st_hy):
    """避險與通膨題材：油價、VIX、信用利差三者的 20 期方向。兩項以上同向才算數。"""
    pos = neg = 0
    parts = []
    if st_wti and st_wti.get("raw_chg20") is not None:
        base = st_wti["spark"][-21] if len(st_wti["spark"]) >= 21 else None
        pct = (st_wti["raw_chg20"] / base * 100) if base else 0
        parts.append(f"WTI 20 日 {pct:+.1f}%")
        if pct >= 5:
            pos += 1
        elif pct <= -5:
            neg += 1
    if st_vix and st_vix.get("latest") is not None:
        parts.append(f"VIX {st_vix['latest']:.1f}")
        if st_vix["latest"] >= 20:
            pos += 1
        elif st_vix["latest"] <= 15:
            neg += 1
    if st_hy and st_hy.get("raw_chg20") is not None:
        bp = st_hy["raw_chg20"] * 100
        parts.append(f"高收益債利差 20 日 {bp:+.0f} bp")
        if bp >= 20:
            pos += 1
        elif bp <= -20:
            neg += 1
    src = "；".join(parts)
    if pos >= 2:
        return 1, "避險需求升溫", src
    if neg >= 2:
        return -1, "市場偏樂觀、避險需求低", src
    return 0, "避險訊號分歧", src


def build_verdict(pillars):
    """外匯多空雙向：只描述總經環境偏多或偏空，分數互相抵消時視為盤整。"""
    total = sum(p["score"] for p in pillars)
    bulls = [p["title"] for p in pillars if p["score"] > 0]
    bears = [p["title"] for p in pillars if p["score"] < 0]
    if total >= 3:
        return dict(score=total, stance="bull", label="總經強烈偏多", headline="四個推力大多同向，對黃金偏多",
                    detail="多數推力同時站在黃金上漲這一邊，趨勢較明確。")
    if total == 2:
        return dict(score=total, stance="bull", label="總經偏多", headline="偏多的推力占上風",
                    detail="偏多推力明顯多於偏空推力，環境偏向支撐金價。")
    if total <= -3:
        return dict(score=total, stance="bear", label="總經強烈偏空", headline="四個推力大多同向，對黃金偏空",
                    detail="多數推力同時站在黃金下跌這一邊，趨勢較明確。")
    if total == -2:
        return dict(score=total, stance="bear", label="總經偏空", headline="偏空的推力占上風",
                    detail="偏空推力明顯多於偏多推力，環境對金價形成壓力。")
    if total == 1:
        return dict(score=total, stance="neutral", label="略偏多，但訊號不足", headline="稍微偏多，還不到明確方向",
                    detail="偏多推力只多一票，容易被下一個數據翻轉，仍以區間震盪看待。")
    if total == -1:
        return dict(score=total, stance="neutral", label="略偏空，但訊號不足", headline="稍微偏空，還不到明確方向",
                    detail="偏空推力只多一票，容易被下一個數據翻轉，仍以區間震盪看待。")
    if bulls and bears:
        return dict(score=0, stance="neutral", label="多空拉鋸，偏向盤整", headline="多空推力互相抵消",
                    detail=f"偏多（{'、'.join(bulls)}）與偏空（{'、'.join(bears)}）力道相當，缺乏單一方向，偏向區間盤整。")
    return dict(score=0, stance="neutral", label="缺乏推力，偏向盤整", headline="四個推力都沒有明顯方向",
                detail="總經面沒有給出方向，金價較可能在區間內整理，等待下一個數據。")


# ---------------------------------------------------------------------------
# 美元指數強弱定位
# ---------------------------------------------------------------------------
DXY_ZONES = [  # 市場慣用區間：1973 年 3 月基期 = 100，站上 100 代表比基期強
    dict(lo=0, hi=90, label="弱勢", gold="對黃金有利"),
    dict(lo=90, hi=95, label="偏弱", gold="對黃金偏有利"),
    dict(lo=95, hi=100, label="中性", gold="影響不大"),
    dict(lo=100, hi=105, label="偏強", gold="對黃金偏不利"),
    dict(lo=105, hi=999, label="強勢", gold="對黃金不利"),
]


def dxy_context(st, seed):
    if not st:
        return None
    lv = st["latest"]
    zone = next(z for z in DXY_ZONES if z["lo"] <= lv < z["hi"])
    pct10 = med10 = lo10 = hi10 = None
    try:
        m = fetch_yahoo("DX-Y.NYB", "10y", seed, interval="1mo")
        vals = sorted(v for _, v in m)
        if len(vals) >= 60:
            pct10 = round(sum(1 for v in vals if v <= lv) / len(vals) * 100)
            med10 = r(vals[len(vals) // 2], 2)
            lo10, hi10 = r(vals[0], 2), r(vals[-1], 2)
    except Exception as e:  # noqa: BLE001
        print(f"[warn] DXY 10y: {e}", flush=True)
    ma = st.get("ma200")
    return dict(level=lv, date=st["latest_date"], zone=zone["label"], zone_gold=zone["gold"],
                zones=DXY_ZONES[:-1] + [dict(DXY_ZONES[-1], hi=115)],
                pct10y=pct10, median10y=med10, low10y=lo10, high10y=hi10,
                ma200=ma, vs_ma200_pct=(r((lv / ma - 1) * 100, 2) if ma else None))


# ---------------------------------------------------------------------------
# 每個指標對黃金的加減分（−2 … +2）與分類
# ---------------------------------------------------------------------------
def _band(x, cuts):
    """cuts = (強負, 負, 正, 強正) 門檻；x ≤ 強負 → −2 … x ≥ 強正 → +2"""
    a, b, c, d = cuts
    if x is None:
        return 0
    if x <= a:
        return -2
    if x <= b:
        return -1
    if x >= d:
        return 2
    if x >= c:
        return 1
    return 0


def _pct20(st):
    if not st or st.get("raw_chg20") is None or len(st["spark"]) < 21:
        return None
    return st["raw_chg20"] / st["spark"][-21] * 100


# 參考指標：顯示但不計分（避免同一件事重複計算）
REFERENCE_ONLY = {"CPIAUCSL": "整體 CPI 與核心 CPI 是同一件事，只用核心計分",
                  "PCEPI": "整體 PCE 與核心 PCE 是同一件事，只用核心計分",
                  "DTWEXBGS": "與 DXY 同方向，只用 DXY 計分",
                  "DCOILBRENTEU": "與 WTI 幾乎同步，只用 WTI 計分",
                  "DGS10": "與 2 年期、實質殖利率重疊，只作參考",
                  "GC=F": "觀察對象本身"}


def _blend(extra):
    a3, yy = extra.get("ann3m"), extra.get("yoy")
    if a3 is None and yy is None:
        return None
    if a3 is None:
        return yy
    if yy is None:
        return a3
    return (a3 + yy) / 2  # 近 3 個月年化與年增率平均，避免單月數字失真


def indicator_score(sid, st, extra, ctx):
    """回傳 (分數 −2…+2, 一句話理由)。正數＝讓黃金偏多，負數＝偏空。
    核心邏輯：聯準會看通膨與就業 → 決定利率 → 利率與美元決定黃金。"""
    if sid in REFERENCE_ONLY:
        return None, REFERENCE_ONLY[sid]
    if not st:
        return 0, "資料不足"
    regime = ctx["regime"]  # hike / cut / neutral：期貨市場目前押的方向
    if sid in ("CPILFESL", "PCEPILFE"):
        b = _blend(extra)
        if b is None:
            return 0, "資料不足"
        adj = 0.4 if sid == "CPILFESL" else 0.0  # CPI 長期比 PCE 高約 0.3–0.5 個百分點
        x = b - adj
        sc = 2 if x < 1.5 else 1 if x < 2.0 else 0 if x <= 2.5 else -1 if x <= 3.0 else -2
        word = "已低於" if x < 2.0 else "接近" if x <= 2.5 else "明顯高於"
        note = f"（CPI 通常比 PCE 高約 0.4，換算約 {x:.1f}%）" if adj else ""
        return sc, f"近 3 個月年化與年增平均 {b:.1f}%{note}，{word}聯準會 2% 目標；越高越逼升息"
    if sid == "PPIFIS":
        b = _blend(extra)
        if b is None:
            return 0, "資料不足"
        sc = 2 if b < 0.5 else 1 if b < 1.5 else 0 if b <= 2.5 else -1 if b <= 3.5 else -2
        return sc, f"上游物價年化約 {b:.1f}%（越高越可能傳到 CPI）"
    if sid == "T10YIE":
        bp = st["raw_chg20"] * 100 if st.get("raw_chg20") is not None else None
        if bp is None:
            return 0, "資料不足"
        if regime == "hike":
            return -_band(bp, (-20, -8, 8, 20)), f"通膨預期 20 日 {bp:+.0f} bp；市場押升息時，通膨預期上升會加強升息壓力"
        if regime == "cut":
            return _band(bp, (-20, -8, 8, 20)), f"通膨預期 20 日 {bp:+.0f} bp；市場押降息時，通膨預期上升會壓低實質利率"
        return 0, f"通膨預期 20 日 {bp:+.0f} bp；利率方向不明，暫不計分"
    if sid == "PAYEMS":
        sp = [v for v in st["spark"] if v is not None][-3:]
        avg = sum(sp) / len(sp)
        sc = -_band(avg, (50, 100, 175, 250))  # 就業越弱越逼降息 → 偏多
        return sc, f"近 3 個月平均新增 {avg:.0f} 千人（100 以下偏弱、175 以上偏強）"
    if sid == "UNRATE":
        sp = [v for v in st["spark"] if v is not None][-12:]
        rise = st["latest"] - min(sp)
        sc = 2 if rise >= 0.5 else 1 if rise >= 0.3 else (-1 if st["latest"] < sp[0] - 0.2 else 0)
        return sc, f"比近 12 個月低點高 {rise:.1f} 個百分點（升 0.5 以上是衰退警訊）"
    if sid == "DGS2":
        bp = st["raw_chg20"] * 100 if st.get("raw_chg20") is not None else None
        return -_band(bp, (-20, -8, 8, 20)), (f"20 日 {bp:+.0f} bp（上升＝市場加碼押升息）" if bp is not None else "資料不足")
    if sid == "DFII10":
        bp = st["raw_chg20"] * 100 if st.get("raw_chg20") is not None else None
        return -_band(bp, (-25, -10, 10, 25)), (f"20 日 {bp:+.0f} bp（黃金最直接的對手）" if bp is not None else "資料不足")
    if sid == "DX-Y.NYB":
        pc = _pct20(st)
        return -_band(pc, (-1.5, -0.5, 0.5, 1.5)), (f"20 日 {pc:+.2f}%" if pc is not None else "資料不足")
    if sid == "DCOILWTICO":
        pc = _pct20(st)
        if pc is None and len(st["spark"]) >= 2:
            pc = (st["spark"][-1] / st["spark"][0] - 1) * 100
        if pc is None:
            return 0, "資料不足"
        if regime == "hike":
            return -_band(pc, (-10, -5, 5, 10)), f"約 1 個月 {pc:+.1f}%；市場押升息時，油價上漲推升通膨、加強升息壓力"
        if regime == "cut":
            return _band(pc, (-10, -5, 5, 10)), f"約 1 個月 {pc:+.1f}%；市場押降息時，油價上漲以通膨避險、地緣風險為主"
        if pc >= 10 and ctx.get("vix", 0) >= 20:
            return 1, f"約 1 個月 {pc:+.1f}% 且 VIX 偏高，偏向地緣避險"
        return (-1 if pc >= 10 else 1 if pc <= -10 else 0), f"約 1 個月 {pc:+.1f}%；利率方向不明，大漲視為通膨壓力"
    if sid == "VIXCLS":
        v = st["latest"]
        sc = 2 if v >= 25 else 1 if v >= 20 else -1 if v <= 13 else 0
        return sc, f"VIX {v:.1f}（20 以上開始緊張，資金找避險）"
    if sid == "BAMLH0A0HYM2":
        bp = st["raw_chg20"] * 100 if st.get("raw_chg20") is not None else None
        return _band(bp, (-50, -20, 20, 50)), (f"20 日 {bp:+.0f} bp（擴大＝市場怕違約）" if bp is not None else "資料不足")
    return 0, ""


CATEGORIES = [
    dict(key="rates", name="利率與降息預期", icon="%", weight=2,
         desc="主軸：通膨、就業、油價最後都透過利率影響黃金。押降息偏多，押升息偏空",
         ids=["FEDX", "DFII10", "DGS2", "DGS10"]),
    dict(key="dollar", name="美元", icon="$", weight=1, desc="黃金用美元計價，美元轉弱偏多，走強偏空",
         ids=["DX-Y.NYB", "DTWEXBGS"]),
    dict(key="inflation", name="通膨", icon="↗", weight=1,
         desc="通膨越高於 2% 目標，聯準會越可能升息 → 偏空；回到目標附近才有降息空間 → 偏多",
         ids=["PCEPILFE", "CPILFESL", "PPIFIS", "T10YIE", "PCEPI", "CPIAUCSL"]),
    dict(key="jobs", name="就業", icon="◎", weight=1, desc="就業轉弱會逼聯準會降息 → 偏多；就業強勁讓升息沒有顧慮 → 偏空",
         ids=["PAYEMS", "UNRATE"]),
    dict(key="risk", name="避險與能源", icon="⚑", weight=1,
         desc="恐慌與信用壓力升高 → 避險買盤偏多；油價要看利率環境，押升息時油價漲算偏空",
         ids=["VIXCLS", "BAMLH0A0HYM2", "DCOILWTICO", "DCOILBRENTEU"]),
]


def build_categories(pool, fedx):
    h = fedx["horizons"][0] if fedx.get("horizons") else None
    if h:
        bp = h["vs_now_bp"]
        pool["FEDX"] = dict(id="FEDX", name="降息預期（期貨隱含年底利率）", unit="%", kind="fed",
                            stats=dict(latest=h["implied"], latest_date=h["date"], chg=f"{bp:+.0f} bp vs 現在", spark=None),
                            gold_score=-_band(bp, (-50, -25, 25, 50)),
                            gold_reason=(f"比目前利率中點 {fedx['target_mid']:.3f}% {'高' if bp > 0 else '低'} {abs(bp):.0f} bp"
                                         f"（市場押約 {abs(round(bp/25))} 碼{'升息' if bp > 0 else '降息'}）") if bp else "與目前利率相同")
    out = []
    for c in CATEGORIES:
        items = [pool[i] for i in c["ids"] if i in pool and pool[i].get("stats")]
        scored = [it["gold_score"] for it in items if it.get("gold_score") is not None]
        avg = round(sum(scored) / len(scored), 1) if scored else 0.0
        out.append(dict(key=c["key"], name=c["name"], icon=c["icon"], desc=c["desc"], weight=c["weight"],
                        ids=[it["id"] for it in items], score=avg, max=2, n_scored=len(scored),
                        weighted=round(avg * c["weight"], 1)))
    return out


def build_verdict_total(categories):
    total = round(sum(c["weighted"] for c in categories), 1)
    mx = 2 * sum(c["weight"] for c in categories)
    pos = [c["name"] for c in categories if c["score"] >= 0.5]
    neg = [c["name"] for c in categories if c["score"] <= -0.5]
    if total >= 6:
        st, lb, dt = "bull", "總經強烈偏多", "多數類別同時讓黃金偏多，方向明確"
    elif total >= 3:
        st, lb, dt = "bull", "總經偏多", "偏多的類別占上風"
    elif total > 1:
        st, lb, dt = "neutral", "略偏多，訊號不足", "偏多稍占上風，但容易被下一個數據翻轉"
    elif total <= -6:
        st, lb, dt = "bear", "總經強烈偏空", "多數類別同時讓黃金偏空，方向明確"
    elif total <= -3:
        st, lb, dt = "bear", "總經偏空", "偏空的類別占上風"
    elif total < -1:
        st, lb, dt = "neutral", "略偏空，訊號不足", "偏空稍占上風，但容易被下一個數據翻轉"
    else:
        st, lb, dt = "neutral", "多空拉鋸，偏向盤整", "多空力道相當，缺乏單一方向"
    parts = []
    if pos:
        parts.append("偏多：" + "、".join(pos))
    if neg:
        parts.append("偏空：" + "、".join(neg))
    return dict(score=total, max=mx, stance=st, label=lb, headline=lb,
                detail=dt + ("（" + "；".join(parts) + "）" if parts else "。"))


def technicals(rows):
    """技術位置：50／200 日均線、近 20 日與 52 週高低點。只描述位置，不計分。"""
    if not rows or len(rows) < 50:
        return None
    vals = [v for _, v in rows]
    last = vals[-1]
    ma50 = sum(vals[-50:]) / 50
    ma200 = sum(vals[-200:]) / 200 if len(vals) >= 200 else None
    y = vals[-252:]
    t = dict(last=r(last, 1), ma50=r(ma50, 1), ma200=r(ma200, 1),
             hi20=r(max(vals[-20:]), 1), lo20=r(min(vals[-20:]), 1),
             hi52=r(max(y), 1), lo52=r(min(y), 1),
             vs_ma50=r((last / ma50 - 1) * 100, 2),
             vs_ma200=r((last / ma200 - 1) * 100, 2) if ma200 else None, date=rows[-1][0])
    if ma200:
        above50, above200, cross = last > ma50, last > ma200, ma50 > ma200
        if above50 and above200:
            t["trend"], t["stance"] = "多頭結構：價格在 50 與 200 日均線之上", "bull"
        elif not above50 and not above200:
            t["trend"], t["stance"] = "空頭結構：價格在 50 與 200 日均線之下", "bear"
        elif above50 and not above200:
            t["trend"], t["stance"] = "反彈中：站回 50 日線，但仍在 200 日線下方", "neutral"
        else:
            t["trend"], t["stance"] = "回檔中：跌破 50 日線，但仍在 200 日線上方", "neutral"
        t["cross"] = "50 日線在 200 日線之上（中期偏多排列）" if cross else "50 日線在 200 日線之下（中期偏空排列）"
    return t


def fetch_cot(seed):
    """CFTC 分類持倉報告（Disaggregated, Futures Only），COMEX 黃金 088691。
    看「管理基金（避險基金、CTA）」淨多單在過去 3 年的百分位，判斷擁擠度。"""
    rows = []
    if seed:
        p = SEED_DIR / "COT_GOLD.csv"
        if not p.exists():
            return None
        for rr in csv.DictReader(io.StringIO(p.read_text())):
            rows.append((rr["date"], float(rr["net"]), float(rr["oi"])))
    else:
        start = (date.today() - timedelta(days=3 * 365 + 14)).isoformat()
        q = urllib.parse.urlencode({
            "$select": "report_date_as_yyyy_mm_dd,m_money_positions_long_all,m_money_positions_short_all,open_interest_all",
            "cftc_contract_market_code": "088691",
            "$where": f"report_date_as_yyyy_mm_dd>'{start}'",
            "$order": "report_date_as_yyyy_mm_dd", "$limit": "500"})
        j = json.loads(http_get("https://publicreporting.cftc.gov/resource/72hh-3qpy.json?" + q))
        for o in j:
            try:
                rows.append((o["report_date_as_yyyy_mm_dd"][:10],
                             float(o["m_money_positions_long_all"]) - float(o["m_money_positions_short_all"]),
                             float(o["open_interest_all"])))
            except (KeyError, ValueError):
                pass
    if len(rows) < 30:
        return None
    nets = [n for _, n, _ in rows]
    last_d, last_n, last_oi = rows[-1]
    pct = round(sum(1 for n in nets if n <= last_n) / len(nets) * 100)
    if pct >= 90:
        zone, stance, note = "多方極度擁擠", "bear", "大家幾乎都已經買了，後面能加碼的人變少，一有利空容易多殺多"
    elif pct >= 75:
        zone, stance, note = "多方偏擁擠", "neutral", "多單偏多，追多的空間變小，要留意回檔"
    elif pct <= 10:
        zone, stance, note = "空方極度擁擠", "bull", "看空或觀望的人已經很多，一有利多容易軋空反彈"
    elif pct <= 25:
        zone, stance, note = "多單偏少", "neutral", "投機資金還沒大舉進場，上漲時有加碼空間"
    else:
        zone, stance, note = "中性", "neutral", "投機部位在正常區間，沒有擁擠"
    return dict(date=last_d, net=int(last_n), oi=int(last_oi), net_pct_oi=r(last_n / last_oi * 100, 1),
                chg_1w=int(last_n - nets[-2]), chg_4w=int(last_n - nets[-5]) if len(nets) > 5 else None,
                pct3y=pct, min3y=int(min(nets)), max3y=int(max(nets)), weeks=len(nets),
                zone=zone, stance=stance, note=note,
                spark=[int(n) for n in nets[-52:]], spark_dates=[d for d, _, _ in rows[-52:]])


def calendar_moves(rows):
    """與前一日、7 日前、30 日前（日曆天，取當天或之前最近一筆）比較。"""
    if not rows:
        return None
    last_d = date.fromisoformat(rows[-1][0]); last_v = rows[-1][1]
    def back(days):
        target = last_d - timedelta(days=days)
        cand = [v for d, v in rows if date.fromisoformat(d) <= target]
        return cand[-1] if cand else None
    out = {}
    prev = rows[-2][1] if len(rows) > 1 else None
    for k, base in (("d1", prev), ("d7", back(7)), ("d30", back(30))):
        if base:
            out[k] = dict(abs=r(last_v - base, 1), pct=r((last_v / base - 1) * 100, 2))
    out["date"] = rows[-1][0]; out["close"] = r(last_v, 1)
    return out


# ---------------------------------------------------------------------------
# 下一個數據（讀 data/calendar.json）
# ---------------------------------------------------------------------------
def next_event(now_tpe: datetime):
    p = DATA_DIR / "calendar.json"
    if not p.exists():
        return None, []
    cal = json.loads(p.read_text(encoding="utf-8"))
    upcoming = []
    for ev in cal.get("events", []):
        try:
            t = datetime.strptime(ev["time_tpe"], "%Y-%m-%d %H:%M").replace(tzinfo=TAIPEI)
        except (KeyError, ValueError):
            continue
        if now_tpe - timedelta(hours=2) <= t <= now_tpe + timedelta(days=30):
            upcoming.append((t, ev))
    upcoming.sort(key=lambda x: x[0])
    if not upcoming:
        return None, []
    wd = "一二三四五六日"
    lst = []
    for t, ev in upcoming:
        o = dict(ev)
        o["time_tpe"] = t.strftime("%m/%d %H:%M")
        o["date_iso"] = t.date().isoformat()
        o["weekday"] = "週" + wd[t.weekday()]
        o["days_left"] = (t.date() - now_tpe.date()).days
        lst.append(o)
    return lst[0], lst


# ---------------------------------------------------------------------------
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--seed", action="store_true", help="離線：讀 scripts/seed/*.csv")
    args = ap.parse_args()
    seed = args.seed

    now_utc = datetime.now(timezone.utc)
    now_tpe = now_utc.astimezone(TAIPEI)
    today = now_tpe.date()
    start_daily = (today - timedelta(days=120)).isoformat()   # 20 期變化 + 24 期 sparkline 綽綽有餘
    start_month = (today - timedelta(days=3 * 365)).isoformat()

    errors = []

    # ---- 經濟數據 ----
    econ = []
    raw_month = {}
    for s in ECON_SERIES:
        try:
            rows = fetch_fred(s["id"], start_month, seed)
        except Exception as e:  # noqa: BLE001
            errors.append(f"{s['id']}: {e}")
            rows = []
        raw_month[s["id"]] = rows
        if s["kind"] == "index_mom":
            conv = index_to_mom(rows)
        elif s["kind"] == "diff":
            conv = index_to_diff(rows)
        else:
            conv = rows
        st = series_stats(conv, s["dec"], s["diff_unit"])
        extra = {}
        if s["kind"] == "index_mom" and rows:
            extra = dict(ann3m=r(annualized_3m(rows), 2), yoy=r(yoy(rows), 2))
        econ.append(dict(id=s["id"], name=s["name"], unit=s["unit"], period=(rows[-1][0][:7] if rows else None),
                         stats=st, **extra))

    # ---- 金融市場 ----
    markets = []
    st_by_id = {}
    gold_moves = None
    gold_tech = None
    for s in MARKET_SERIES:
        try:
            rows = fetch_yahoo(s["id"], s.get("rng", "6mo"), seed) if s["src"] == "yahoo" else fetch_fred(s["id"], start_daily, seed)
        except Exception as e:  # noqa: BLE001
            errors.append(f"{s['id']}: {e}")
            rows = []
        st = series_stats(rows, s["dec"], s["diff_unit"], s.get("pct", False))
        if st and len(rows) >= 200:
            st["ma200"] = r(sum(v for _, v in rows[-200:]) / 200, 2)
        st_by_id[s["id"]] = st
        if s["id"] == "GC=F":
            gold_moves = calendar_moves(rows)
            gold_tech = technicals(rows)
        markets.append(dict(id=s["id"], name=s["name"], unit=s["unit"], source=s["src"], stats=st))

    aux = {}
    for sid in AUX_FRED:
        try:
            rows = fetch_fred(sid, start_daily, seed)
            aux[sid] = rows[-1][1] if rows else None
        except Exception as e:  # noqa: BLE001
            errors.append(f"{sid}: {e}")
            aux[sid] = None
    mid = None
    if aux.get("DFEDTARU") is not None and aux.get("DFEDTARL") is not None:
        mid = (aux["DFEDTARU"] + aux["DFEDTARL"]) / 2
    dgs2 = st_by_id.get("DGS2", {}) or {}
    fedx = fed_expectations(today, mid, aux.get("DFF"), dgs2.get("latest"), seed)
    fedx["target_range"] = f"{aux.get('DFEDTARL')}–{aux.get('DFEDTARU')}%" if mid is not None else None

    # ---- 規則式判讀 ----
    s1, t1, src1 = score_rates(fedx)
    if st_by_id.get("DX-Y.NYB"):
        s2, t2, src2 = score_dollar(st_by_id["DX-Y.NYB"])
    else:
        s2, t2, src2 = score_dollar(st_by_id.get("DTWEXBGS"), "廣義美元指數")
    s3, t3, src3 = score_real_yield(st_by_id.get("DFII10"), st_by_id.get("T10YIE"))
    s4, t4, src4 = score_risk(st_by_id.get("DCOILWTICO"), st_by_id.get("VIXCLS"), st_by_id.get("BAMLH0A0HYM2"))
    pillars = [
        dict(key="rates", question="降息預期有沒有升溫？", why="為什麼看：市場預期降息越多，持有黃金的機會成本越低。",
             score=s1, title=t1, evidence=src1, links=["DGS2", "DFF"]),
        dict(key="dollar", question="美元有沒有轉弱？", why="為什麼看：黃金用美元計價，美元跌、金價通常撐得住。",
             score=s2, title=t2, evidence=src2, links=["DX-Y.NYB", "DTWEXBGS"]),
        dict(key="real", question="實質利率有沒有下降？", why="為什麼看：扣掉通膨後的利率，是黃金最直接的對手。",
             score=s3, title=t3, evidence=src3, links=["DFII10", "T10YIE"]),
        dict(key="risk", question="避險需求有沒有升高？", why="為什麼看：油價、恐慌指數、信用利差反映市場怕不怕。",
             score=s4, title=t4, evidence=src4, links=["DCOILWTICO", "VIXCLS", "BAMLH0A0HYM2"]),
    ]
    # ---- 每個指標加減分（依利率環境判斷傳導方向）----
    h0 = fedx["horizons"][0] if fedx.get("horizons") else None
    bp0 = h0["vs_now_bp"] if h0 else fedx.get("proxy_2y_minus_ff_bp")
    regime = "hike" if bp0 is not None and bp0 >= 25 else "cut" if bp0 is not None and bp0 <= -25 else "neutral"
    ctx = dict(regime=regime, vix=(st_by_id.get("VIXCLS") or {}).get("latest") or 0)
    for it in econ + markets:
        ex = {k: it.get(k) for k in ("ann3m", "yoy")}
        it["gold_score"], it["gold_reason"] = indicator_score(it["id"], it.get("stats"), ex, ctx)
    pool = {e["id"]: e for e in econ + markets}
    categories = build_categories(pool, fedx)
    verdict = build_verdict_total(categories)
    verdict["regime"] = regime
    nxt, upcoming = next_event(now_tpe)
    try:
        cot = fetch_cot(seed)
    except Exception as e:  # noqa: BLE001
        errors.append(f"CFTC COT: {e}")
        cot = None
    sp = DATA_DIR / "structural.json"
    structural = json.loads(sp.read_text(encoding="utf-8")) if sp.exists() else None
    fed_item = pool.get("FEDX")
    dxy = dxy_context(st_by_id.get("DX-Y.NYB"), seed)

    out = dict(
        generated_at=now_utc.isoformat(timespec="seconds"),
        generated_at_tpe=now_tpe.strftime("%m/%d %H:%M"),
        mode="seed" if seed else "live",
        verdict=verdict,
        pillars=pillars,
        fed=fedx,
        next_event=nxt,
        upcoming=upcoming,
        categories=categories,
        fed_item=fed_item,
        gold_moves=gold_moves,
        gold_tech=gold_tech,
        cot=cot,
        structural=structural,
        dxy=dxy,
        econ=econ,
        markets=markets,
        errors=errors,
        count=len(econ) + len(markets),
    )
    DATA_DIR.mkdir(exist_ok=True)
    (DATA_DIR / "data.json").write_text(json.dumps(out, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"[ok] wrote data/data.json  mode={out['mode']}  verdict={verdict['stance']} ({verdict['score']})")
    for e in errors:
        print("[warn]", e, file=sys.stderr)


if __name__ == "__main__":
    main()
