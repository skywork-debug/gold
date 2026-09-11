#!/usr/bin/env python3
"""
劇烈波動警示（每 30 分鐘由 GitHub Actions 執行，不耗 AI token）
================================================================
抓 Yahoo Finance 即時報價，和前一日收盤比較：
    黃金期貨 GC=F       單日漲跌 ≥ 1.5%
    VIX 恐慌指數 ^VIX    單日上升 ≥ 20%，或絕對值 ≥ 30
    美元指數 DX-Y.NYB    單日漲跌 ≥ 1.0%
    WTI 原油 CL=F        單日漲跌 ≥ 5.0%
任一條件成立 → data/alert.json 狀態為 alert，網頁頂端顯示警示。

為了不讓 repo 每 30 分鐘多一筆紀錄：只有「狀態或觸發項目改變」時才改寫 alert.json，
workflow 發現檔案沒變就不 commit。

用法：
    python scripts/check_alert.py            # 線上
    python scripts/check_alert.py --test     # 模擬觸發（本機測試用，不會連網）
"""
from __future__ import annotations

import argparse
import json
import sys
import time
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "data" / "alert.json"
TAIPEI = timezone(timedelta(hours=8))
UA = {"User-Agent": "Mozilla/5.0 (compatible; gold-macro-dashboard/1.0)"}

RULES = [
    # symbol, 名稱, 規則類型, 門檻, 說明
    dict(sym="GC=F", name="黃金", kind="abs_pct", th=1.5, why="金價單日大幅波動"),
    dict(sym="^VIX", name="VIX 恐慌指數", kind="up_pct_or_level", th=20.0, level=30.0, why="市場恐慌急升"),
    dict(sym="DX-Y.NYB", name="美元指數 DXY", kind="abs_pct", th=1.0, why="美元單日大幅波動"),
    dict(sym="CL=F", name="WTI 原油", kind="abs_pct", th=5.0, why="油價單日大幅波動（常伴隨地緣事件）"),
]


def quote(sym: str):
    url = f"https://query1.finance.yahoo.com/v8/finance/chart/{urllib.parse.quote(sym)}?range=1d&interval=5m"
    last = None
    for i in range(2):
        try:
            req = urllib.request.Request(url, headers=UA)
            with urllib.request.urlopen(req, timeout=12) as r:
                m = json.loads(r.read().decode())["chart"]["result"][0]["meta"]
            p, prev = m.get("regularMarketPrice"), m.get("previousClose") or m.get("chartPreviousClose")
            if p is None or not prev:
                return None
            return dict(price=float(p), prev=float(prev), time=int(m.get("regularMarketTime") or 0))
        except Exception as e:  # noqa: BLE001
            last = e
            time.sleep(1)
    print(f"[warn] {sym}: {last}", flush=True)
    return None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--test", action="store_true", help="模擬：黃金 −2.1%、VIX +25%")
    args = ap.parse_args()

    now = datetime.now(timezone.utc).astimezone(TAIPEI)
    fake = {"GC=F": (4300.0, 4392.0), "^VIX": (22.0, 17.6), "DX-Y.NYB": (99.1, 99.0), "CL=F": (101.0, 102.5)}
    triggers, readings = [], []
    for rule in RULES:
        q = dict(price=fake[rule["sym"]][0], prev=fake[rule["sym"]][1], time=0) if args.test else quote(rule["sym"])
        if not q:
            continue
        chg = (q["price"] / q["prev"] - 1) * 100
        readings.append(dict(name=rule["name"], price=round(q["price"], 2), chg_pct=round(chg, 2)))
        hit = False
        if rule["kind"] == "abs_pct" and abs(chg) >= rule["th"]:
            hit = True
        if rule["kind"] == "up_pct_or_level" and (chg >= rule["th"] or q["price"] >= rule["level"]):
            hit = True
        if hit:
            triggers.append(dict(name=rule["name"], why=rule["why"], price=round(q["price"], 2), chg_pct=round(chg, 2)))

    status = "alert" if triggers else "calm"
    key = status + "|" + ",".join(sorted(t["name"] for t in triggers))

    old = {}
    if OUT.exists():
        try:
            old = json.loads(OUT.read_text(encoding="utf-8"))
        except ValueError:
            old = {}
    if old.get("key") == key:
        print(f"[ok] 狀態未變（{status}），不改檔")
        return

    out = dict(key=key, status=status, since=now.strftime("%m/%d %H:%M"), since_iso=now.isoformat(timespec="minutes"),
               triggers=triggers, readings=readings,
               note=("市場劇烈波動中：總經分數可能還沒反映，請以即時行情為準" if triggers else "目前沒有劇烈波動"))
    OUT.parent.mkdir(exist_ok=True)
    OUT.write_text(json.dumps(out, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"[ok] 狀態改變 → {status}：{[t['name'] for t in triggers]}")


if __name__ == "__main__":
    sys.exit(main())
