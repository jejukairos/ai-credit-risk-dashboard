# -*- coding: utf-8 -*-
"""데이터를 받아 docs/data.json 과 docs/status.json 을 만듭니다.

데이터 출처마다 따로 시도하고, 실패하면 data/ 폴더에 저장해 둔 지난 데이터를 그대로 쓰면서
status.json 에 오류를 기록합니다. 화면 상단에 그 오류가 표시됩니다.
"""
import io
import json
import os
import sys
import time
import traceback
from datetime import datetime, timedelta, timezone

import pandas as pd
import requests

import config

BASE = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.join(BASE, "data")
DOCS = os.path.join(BASE, "docs")
KST = timezone(timedelta(hours=9))
UA = {"User-Agent": "Mozilla/5.0 (ai-credit-risk-dashboard)"}


def now_kst():
    return datetime.now(KST).strftime("%Y-%m-%d %H:%M")


def load_env():
    path = os.path.join(BASE, ".env")
    if os.path.exists(path):
        with open(path, encoding="utf-8-sig") as f:
            for line in f:
                if "=" in line:
                    k, v = line.strip().split("=", 1)
                    os.environ.setdefault(k.strip(), v.strip())


def get(url, tries=3, timeout=60, **kw):
    last = None
    for i in range(tries):
        try:
            r = requests.get(url, headers=UA, timeout=timeout, **kw)
            r.raise_for_status()
            return r
        except Exception as e:  # noqa: BLE001
            last = e
            time.sleep(2 ** (i + 1))
    raise last


# ---------- 1. 주가 (야후파이낸스) ----------
def fetch_prices():
    import yfinance as yf
    tickers = [t["ticker"] for t in config.TICKERS]
    df = yf.download(tickers, start=config.START_DATE, progress=False, auto_adjust=False, threads=False)["Close"]
    df = df.dropna(how="all")
    missing = [t for t in tickers if t not in df.columns or df[t].dropna().empty]
    if missing:
        raise RuntimeError("주가 없음: " + ", ".join(missing))
    df.index = pd.to_datetime(df.index).strftime("%Y-%m-%d")
    df.index.name = "date"
    df.round(4).to_csv(os.path.join(DATA, "prices.csv"))


# ---------- 2·3. 등급별 회사채 스프레드 (FRED) ----------
def fred_series(series_id):
    key = os.environ.get("FRED_API_KEY", "").strip()
    if key:
        r = get("https://api.stlouisfed.org/fred/series/observations",
                params={"series_id": series_id, "api_key": key, "file_type": "json",
                        "observation_start": config.START_DATE})
        obs = r.json()["observations"]
        s = pd.Series({o["date"]: o["value"] for o in obs})
    else:
        r = get(f"https://fred.stlouisfed.org/graph/fredgraph.csv?id={series_id}&cosd={config.START_DATE}",
                tries=1, timeout=60)
        d = pd.read_csv(io.StringIO(r.text))
        s = pd.Series(d.iloc[:, 1].values, index=d.iloc[:, 0].values)
    s = pd.to_numeric(s, errors="coerce").dropna() * 100  # % -> bp
    return s.round(1)


def fetch_spreads():
    out = {}
    errors = []
    for bucket, sid in config.SPREAD_SERIES.items():
        try:
            out[bucket] = fred_series(sid)
        except Exception as e:  # noqa: BLE001
            errors.append(f"{bucket}({sid}): {e}")
            if not out and not os.environ.get("FRED_API_KEY"):
                break  # 키 없이 첫 시도부터 막히면 나머지도 막히므로 시간 낭비하지 않음
    path = os.path.join(DATA, "spreads.csv")
    if out:
        new = pd.DataFrame(out)
        if os.path.exists(path):
            old = pd.read_csv(path, index_col=0)
            new = new.combine_first(old)
        new.index.name = "date"
        new.sort_index().to_csv(path)
    if errors:
        hint = "" if os.environ.get("FRED_API_KEY") else " (FRED API 키가 없어 대체 경로 사용 중 — README의 FRED 키 안내 참고)"
        raise RuntimeError("; ".join(errors)[:400] + hint)


# ---------- GPU 임대가격 (Price of Compute) ----------
def fetch_gpu():
    path = os.path.join(DATA, "gpu_prices.csv")
    old = pd.read_csv(path) if os.path.exists(path) else pd.DataFrame(columns=["date", "sku", "type", "usd"])
    rows = []
    for g in config.GPU_SKUS:
        j = get(f"https://priceofcompute.com/api/v1/history/{g['sku']}").json()
        for s in j.get("series", []):
            for p in s.get("points", []):
                rows.append({"date": p["day"], "sku": g["sku"], "type": s["pricing_type"],
                             "usd": p["usd_per_gpu_hr"]})
        time.sleep(1)
    new = pd.DataFrame(rows)
    allp = pd.concat([old, new]).drop_duplicates(["date", "sku", "type"], keep="last")
    allp.sort_values(["sku", "type", "date"]).to_csv(path, index=False)


# ---------- 화면용 data.json 만들기 ----------
def pct_change(s, days):
    s = s.dropna()
    if s.empty:
        return None
    last_date = pd.Timestamp(s.index[-1])
    past = s[pd.to_datetime(s.index) <= last_date - pd.Timedelta(days=days)]
    if past.empty:
        return None
    return round((s.iloc[-1] / past.iloc[-1] - 1) * 100, 2)


def risk_level(cds_bp, cds_date, spread_bp, m1):
    R = config.RISK
    level = 0
    reasons = []
    if cds_bp is not None and cds_date:
        age = (datetime.now(KST).date() - datetime.strptime(cds_date, "%Y-%m-%d").date()).days
        if age <= R["cds_max_age_days"]:
            if cds_bp >= R["cds_red"]:
                level = 2; reasons.append(f"CDS {cds_bp:.0f}bp ≥ {R['cds_red']}")
            elif cds_bp >= R["cds_yellow"]:
                level = max(level, 1); reasons.append(f"CDS {cds_bp:.0f}bp ≥ {R['cds_yellow']}")
    if spread_bp is not None:
        if spread_bp >= R["spread_red"]:
            level = 2; reasons.append(f"스프레드 {spread_bp:.0f}bp ≥ {R['spread_red']}")
        elif spread_bp >= R["spread_yellow"]:
            level = max(level, 1); reasons.append(f"스프레드 {spread_bp:.0f}bp ≥ {R['spread_yellow']}")
    if m1 is not None:
        if m1 <= R["m1_red"]:
            level = 2; reasons.append(f"1개월 주가 {m1:.1f}%")
        elif m1 <= R["m1_yellow"]:
            level = max(level, 1); reasons.append(f"1개월 주가 {m1:.1f}%")
    return ["green", "yellow", "red"][level], reasons


def build():
    prices = pd.read_csv(os.path.join(DATA, "prices.csv"), index_col=0)
    sp_path = os.path.join(DATA, "spreads.csv")
    spreads = pd.read_csv(sp_path, index_col=0) if os.path.exists(sp_path) else pd.DataFrame()
    cds = pd.read_csv(os.path.join(BASE, "cds_log.csv"), dtype=str).fillna("")
    cds["bp"] = pd.to_numeric(cds["bp"], errors="coerce")
    cds = cds.dropna(subset=["bp"])
    cds = cds[cds["source_url"].str.startswith("http")]  # 출처 없는 숫자는 쓰지 않음
    cds = cds.sort_values("date", kind="stable")

    out = {"generated_at": now_kst(), "tickers": [], "gpu": [], "risk_rules": config.RISK}
    for t in config.TICKERS:
        tk = t["ticker"]
        p = prices[tk].dropna() if tk in prices else pd.Series(dtype=float)
        sp = spreads[t["bucket"]].dropna() if t["bucket"] in spreads else pd.Series(dtype=float)
        c = cds[cds["ticker"] == tk]
        cds_last = c.iloc[-1] if len(c) else None
        m1 = pct_change(p, 30)
        sp_last = float(sp.iloc[-1]) if len(sp) else None
        cds_bp = float(cds_last["bp"]) if cds_last is not None else None
        cds_date = cds_last["date"] if cds_last is not None else None
        color, reasons = risk_level(cds_bp, cds_date, sp_last, m1)
        out["tickers"].append({
            **t,
            "price": round(float(p.iloc[-1]), 2) if len(p) else None,
            "price_date": p.index[-1] if len(p) else None,
            "w1": pct_change(p, 7), "m1": m1,
            "spread": sp_last, "spread_date": sp.index[-1] if len(sp) else None,
            "spread_series": config.SPREAD_SERIES[t["bucket"]],
            "cds": cds_bp, "cds_date": cds_date,
            "risk": color, "risk_reasons": reasons,
            "series": {
                "price": {"x": list(p.index), "y": [round(v, 2) for v in p.values]},
                "spread": {"x": list(sp.index), "y": [round(v, 1) for v in sp.values]},
                "cds": {"x": list(c["date"]), "y": [float(v) for v in c["bp"]],
                        "url": list(c["source_url"]), "note": list(c.get("note", [""] * len(c)))},
            },
        })

    gp = os.path.join(DATA, "gpu_prices.csv")
    if os.path.exists(gp):
        g = pd.read_csv(gp)
        for sku in config.GPU_SKUS:
            gs = g[g["sku"] == sku["sku"]]
            # 온디맨드(일반 클라우드) 우선, 없으면 커뮤니티(개인 공급자) 가격
            typ = "on_demand" if (gs["type"] == "on_demand").any() else "community"
            gs = gs[gs["type"] == typ].sort_values("date")
            s = pd.Series(gs["usd"].values, index=gs["date"].values)
            out["gpu"].append({
                **sku, "type": typ,
                "last": round(float(s.iloc[-1]), 2) if len(s) else None,
                "last_date": s.index[-1] if len(s) else None,
                "w1": pct_change(s, 7), "m1": pct_change(s, 30),
                "x": list(s.index), "y": [round(float(v), 3) for v in s.values],
            })

    with open(os.path.join(DOCS, "data.json"), "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, separators=(",", ":"))


def main():
    load_env()
    os.makedirs(DATA, exist_ok=True)
    status_path = os.path.join(DOCS, "status.json")
    try:
        with open(status_path, encoding="utf-8") as f:
            status = json.load(f)
    except Exception:  # noqa: BLE001
        status = {"sources": {}}
    status["last_run"] = now_kst()
    status["errors"] = []

    steps = [("주가(야후파이낸스)", fetch_prices), ("회사채 스프레드(FRED)", fetch_spreads),
             ("GPU 임대가격(Price of Compute)", fetch_gpu), ("화면 데이터 만들기", build)]
    for name, fn in steps:
        try:
            fn()
            status["sources"][name] = {"ok": True, "last_ok": now_kst()}
            print(f"[OK] {name}")
        except Exception as e:  # noqa: BLE001
            msg = f"{type(e).__name__}: {e}"[:500]
            prev = status["sources"].get(name, {})
            status["sources"][name] = {"ok": False, "last_ok": prev.get("last_ok"), "error": msg}
            status["errors"].append(f"{name} 실패 — {msg}")
            print(f"[실패] {name}: {msg}")
            traceback.print_exc()

    if not status["errors"]:
        status["last_success"] = status["last_run"]
    with open(status_path, "w", encoding="utf-8") as f:
        json.dump(status, f, ensure_ascii=False, indent=1)
    return 1 if status["errors"] else 0


if __name__ == "__main__":
    sys.exit(main())
