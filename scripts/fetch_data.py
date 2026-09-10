#!/usr/bin/env python3
"""
黃金觀測室 — 資料抓取與規則式判讀
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
    dict(id="GC=F", src="yahoo", name="黃金期貨（COMEX）", unit="美元／盎司", dec=1, diff_unit="美元", pct=True),
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
def http_get(url: str, retries: int = 3) -> str:
    last = None
    for i in range(retries):
        try:
            req = urllib.request.Request(url, headers=UA)
            with urllib.request.urlopen(req, timeout=30) as r:
                return r.read().decode("utf-8", "replace")
        except Exception as e:  # noqa: BLE001
            last = e
            time.sleep(2 * (i + 1))
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
    url = f"https://fred.stlouisfed.org/graph/fredgraph.csv?id={series}&cosd={start}"
    return parse_csv(http_get(url))


def fetch_yahoo(symbol: str, rng: str, seed: bool) -> list[tuple[str, float]]:
    if seed:
        p = SEED_DIR / (symbol.replace("=", "_").replace(".", "_") + ".csv")
        return parse_csv(p.read_text()) if p.exists() else []
    url = f"https://query1.finance.yahoo.com/v8/finance/chart/{urllib.parse.quote(symbol)}?range={rng}&interval=1d"
    j = json.loads(http_get(url))
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
        out.append((d, float(c)))
    # 同一天保留最後一筆
    dedup = {}
    for d, v in out:
        dedup[d] = v
    return sorted(dedup.items())


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


def score_dollar(st):
    """美元：廣義美元指數 20 期變化。跌 >0.5% 支持黃金；漲 >0.5% 壓力。"""
    if not st or st.get("raw_chg20") is None:
        return 0, "資料不足", ""
    base = st["spark"][-21] if len(st["spark"]) >= 21 else None
    pct = (st["raw_chg20"] / base * 100) if base else 0
    src = f"廣義美元指數 20 個交易日變化 {st['chg20']}（{pct:+.2f}%）"
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
    total = sum(p["score"] for p in pillars)
    if total >= 2:
        return dict(score=total, stance="bull", label="利多到齊，偏向支持金價",
                    headline="利多條件到齊，偏向支持金價",
                    detail="降息預期、美元、實質利率、避險需求之中，多數已轉向對黃金有利。")
    if total <= -2:
        return dict(score=total, stance="bear", label="逆風增加，先留意壓力",
                    headline="逆風條件增加，先留意下跌壓力",
                    detail="多數條件目前對黃金不利，反彈時請保守看待。")
    return dict(score=total, stance="neutral", label="利多還沒到齊，先保守看待",
                headline="尚未形成一致利多，先保守看待",
                detail="四個條件方向不一致，黃金缺乏共同推力。")


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
        if t >= now_tpe - timedelta(hours=2):
            upcoming.append((t, ev))
    upcoming.sort(key=lambda x: x[0])
    if not upcoming:
        return None, []
    wd = "一二三四五六日"
    lst = []
    for t, ev in upcoming[:8]:
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
    for s in MARKET_SERIES:
        try:
            rows = fetch_yahoo(s["id"], "6mo", seed) if s["src"] == "yahoo" else fetch_fred(s["id"], start_daily, seed)
        except Exception as e:  # noqa: BLE001
            errors.append(f"{s['id']}: {e}")
            rows = []
        st = series_stats(rows, s["dec"], s["diff_unit"], s.get("pct", False))
        st_by_id[s["id"]] = st
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
    s2, t2, src2 = score_dollar(st_by_id.get("DTWEXBGS"))
    s3, t3, src3 = score_real_yield(st_by_id.get("DFII10"), st_by_id.get("T10YIE"))
    s4, t4, src4 = score_risk(st_by_id.get("DCOILWTICO"), st_by_id.get("VIXCLS"), st_by_id.get("BAMLH0A0HYM2"))
    pillars = [
        dict(key="rates", question="降息預期有沒有升溫？", why="為什麼看：市場預期降息越多，持有黃金的機會成本越低。",
             score=s1, title=t1, evidence=src1, links=["DGS2", "DFF"]),
        dict(key="dollar", question="美元有沒有轉弱？", why="為什麼看：黃金用美元計價，美元跌、金價通常撐得住。",
             score=s2, title=t2, evidence=src2, links=["DTWEXBGS"]),
        dict(key="real", question="實質利率有沒有下降？", why="為什麼看：扣掉通膨後的利率，是黃金最直接的對手。",
             score=s3, title=t3, evidence=src3, links=["DFII10", "T10YIE"]),
        dict(key="risk", question="避險需求有沒有升高？", why="為什麼看：油價、恐慌指數、信用利差反映市場怕不怕。",
             score=s4, title=t4, evidence=src4, links=["DCOILWTICO", "VIXCLS", "BAMLH0A0HYM2"]),
    ]
    verdict = build_verdict(pillars)
    nxt, upcoming = next_event(now_tpe)

    out = dict(
        generated_at=now_utc.isoformat(timespec="seconds"),
        generated_at_tpe=now_tpe.strftime("%m/%d %H:%M"),
        mode="seed" if seed else "live",
        verdict=verdict,
        pillars=pillars,
        fed=fedx,
        next_event=nxt,
        upcoming=upcoming,
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
