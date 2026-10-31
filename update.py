# -*- coding: utf-8 -*-
"""데이터를 받아 docs/data.json 과 docs/status.json 을 만듭니다.

데이터 출처마다 따로 시도하고, 실패하면 data/ 폴더에 저장해 둔 지난 데이터를 그대로 쓰면서
status.json 에 오류를 기록합니다. 화면 상단에 그 오류가 표시됩니다.
"""
import io
import json
import os
import re
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
    # 코스피(고객예탁금 비교용) · S&P500(공포탐욕지수 이후 수익률 계산용), 2020년부터
    for sym, name in [("^KS11", "kospi"), ("^KQ11", "kosdaq"), ("^GSPC", "sp500")]:
        k = yf.download(sym, start="2020-01-01", progress=False, auto_adjust=False)["Close"].squeeze().dropna()
        if not k.empty:
            k.index = pd.to_datetime(k.index).strftime("%Y-%m-%d")
            k.rename(name).rename_axis("date").round(2).to_csv(os.path.join(DATA, f"{name}.csv"))


# ---------- 2·3. 등급별 회사채 스프레드 (FRED) ----------
def fred_series(series_id, start=None):
    start = start or config.START_DATE
    key = os.environ.get("FRED_API_KEY", "").strip()
    if key:
        r = get("https://api.stlouisfed.org/fred/series/observations",
                params={"series_id": series_id, "api_key": key, "file_type": "json",
                        "observation_start": start})
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


# ---------- D램 현물가격 (TrendForce/DRAMeXchange 무료 공개분) ----------
DRAM_ITEMS = {  # 대표 품목만: 현물 시세표 품목명 -> 저장 이름
    "DDR5 16Gb (2Gx8) 4800/5600": "DDR5 16Gb",
    "DDR4 8Gb (1Gx8) 3200": "DDR4 8Gb",
}


def fetch_dram():
    errors = []
    # 1) 주간 기사(Memory Spot Price Update)에서 과거 주력칩(DDR4 8Gb) 가격 — 새 기사만 추가
    try:
        sys.path.insert(0, os.path.join(BASE, "tools"))
        import backfill_dram
        backfill_dram.main()
    except Exception as e:  # noqa: BLE001
        errors.append(f"주간 기사: {e}")
    # 2) 오늘 현물 시세표 (매일 쌓아서 일별 이력 만들기)
    try:
        html = get("https://www.trendforce.com/price/dram/dram_spot").text
        t = re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", re.sub(r"<script[\s\S]*?</script>", " ", html)))
        m = re.search(r"Last Update (\d{4}-\d\d-\d\d)", t)
        if not m:
            raise RuntimeError("시세표 날짜를 못 찾음 (사이트 구조 변경?)")
        day = m.group(1)
        rows = []
        for item, name in DRAM_ITEMS.items():
            mm = re.search(re.escape(item) + r" ([\d.]+) ([\d.]+) ([\d.]+) ([\d.]+) ([\d.]+)", t)
            if mm:
                rows.append({"date": day, "item": name, "usd": float(mm.group(5))})
        if not rows:
            raise RuntimeError("현물 시세표에서 가격을 못 찾음 (사이트 구조 변경?)")
        # 계약(고정거래)가격 — 같은 페이지 아래 표, 반월(2주)마다 갱신. 대표 칩 DDR4 8Gb만
        cm = re.search(r"DRAM Contract Price.{0,200}?Last Update (\d{4}-\d\d-\d\d).{0,600}?DDR4 8Gb 1Gx8 ([\d.]+) ([\d.]+) ([\d.]+)", t)
        if cm:
            rows.append({"date": cm.group(1), "item": "DDR4 8Gb 계약", "usd": float(cm.group(4))})
        # DXI(D램 전체 지수) — DRAMeXchange 첫 화면
        try:
            h2 = get("https://www.dramexchange.com/").text
            t2 = re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", re.sub(r"<script[\s\S]*?</script>", " ", h2)))
            dm = re.search(r"DXI ([A-Za-z]+)\.?\s?(\d{1,2}), [\d:]+ \(GMT\+8\) ([\d.,]+)", t2)
            if dm:
                mon = ["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"].index(dm.group(1)[:3].lower()) + 1
                today = datetime.now(KST).date()
                yr = today.year if mon <= today.month else today.year - 1
                rows.append({"date": f"{yr}-{mon:02d}-{int(dm.group(2)):02d}", "item": "DXI",
                             "usd": float(dm.group(3).replace(",", ""))})
            else:
                errors.append("DXI 지수를 못 찾음")
        except Exception as e:  # noqa: BLE001
            errors.append(f"DXI: {e}")
        path = os.path.join(DATA, "dram_daily.csv")
        old = pd.read_csv(path) if os.path.exists(path) else pd.DataFrame(columns=["date", "item", "usd"])
        allr = pd.concat([old, pd.DataFrame(rows)]).drop_duplicates(["date", "item"], keep="last")
        allr.sort_values(["item", "date"]).to_csv(path, index=False)
    except Exception as e:  # noqa: BLE001
        errors.append(f"일별 시세표: {e}")
    if errors:
        raise RuntimeError("; ".join(errors)[:400])


# ---------- 미수금·반대매매·신용융자 (금융투자협회 FreeSIS) ----------
KOFIA_URL = "https://freesis.kofia.or.kr/meta/getMetaDataList.do"


def kofia_query(obj, start, end):
    body = {"dmSearch": {"tmpV40": "1000000", "tmpV41": "1", "tmpV1": "D",
                         "tmpV45": start, "tmpV46": end, "OBJ_NM": obj}}
    r = requests.post(KOFIA_URL, json=body, headers=UA, timeout=120)
    r.raise_for_status()
    return pd.DataFrame(r.json()["ds1"])


def fetch_kofia():
    path = os.path.join(DATA, "kofia.csv")
    old = pd.read_csv(path, dtype={"date": str}) if os.path.exists(path) else None
    start = "20150101" if old is None or old.empty else \
        (pd.Timestamp(old["date"].max()) - pd.Timedelta(days=40)).strftime("%Y%m%d")
    end = datetime.now(KST).strftime("%Y%m%d")
    # 증시자금추이 (단위 백만원): 투자자예탁금, 위탁매매 미수금, 실제 반대매매금액, 미수금 대비 반대매매 비중(%)
    a = kofia_query("STATSCU0100000060BO", start, end)
    a = a.rename(columns={"TMPV1": "date", "TMPV2": "deposit", "TMPV5": "misu", "TMPV6": "forced", "TMPV7": "ratio"})
    a = a[["date", "deposit", "misu", "forced", "ratio"]]
    # 신용공여 잔고 추이 (단위 백만원): 신용거래융자 전체/코스피/코스닥
    b = kofia_query("STATSCU0100000070BO", start, end)
    b = b.rename(columns={"TMPV1": "date", "TMPV2": "credit", "TMPV3": "credit_kospi", "TMPV4": "credit_kosdaq"})
    b = b[["date", "credit", "credit_kospi", "credit_kosdaq"]]
    new = a.merge(b, on="date", how="outer")
    new["date"] = pd.to_datetime(new["date"], format="%Y%m%d").dt.strftime("%Y-%m-%d")
    if old is not None:
        new = pd.concat([old, new]).drop_duplicates("date", keep="last")
    if new.empty:
        raise RuntimeError("금투협 응답이 비어 있음")
    new.sort_values("date").to_csv(path, index=False)
    # 신용잔고 증가율(1년 전 대비) 장기 차트용: 2015년 전 코스피 신용거래융자(1998-07~, 그 전·일부 날은 0이라 뺌). 지난 자료라 한 번만 받음
    op = os.path.join(DATA, "kofia_credit_old.csv")
    if not os.path.exists(op):
        o = kofia_query("STATSCU0100000070BO", "19980101", "20141231").rename(columns={"TMPV1": "date", "TMPV3": "credit_kospi"})
        o["credit_kospi"] = pd.to_numeric(o["credit_kospi"])
        o = o[o["credit_kospi"] > 0]
        o["date"] = pd.to_datetime(o["date"], format="%Y%m%d").dt.strftime("%Y-%m-%d")
        o[["date", "credit_kospi"]].sort_values("date").to_csv(op, index=False)


def build_credit_growth():
    # 신용잔고 1년 전 대비 증가율(%) + 지수. 미국 = FINRA 월말 신용잔고(1997~) · S&P500 월말 종가, 한국 = 금투협 코스피 신용거래융자
    # (1998-07~, 1년 전 그날이 없으면 그 전 날) · 코스피. 한국은 2년보다 오래된 기간을 주 1점(금요일)으로 줄여 보냄.
    # 한국을 코스피만으로 한 이유: 사용자 제공 그림 [표2]의 2026년 고점(약 160%)과 맞는 건 코스피 신용(162.7%), 전체는 107%
    out = {}
    mp, sp = os.path.join(DATA, "margin_debt.csv"), os.path.join(DATA, "sp500_monthly.csv")
    if os.path.exists(mp) and os.path.exists(sp):
        m = pd.read_csv(mp).set_index("month")["debit"]
        m.index = pd.PeriodIndex(m.index, freq="M")
        yoy = ((m / m.reindex(m.index - 12).values - 1) * 100).dropna()
        s = pd.read_csv(sp).set_index("month")["sp500"]
        s = s[s.index >= str(yoy.index[0])]
        end = lambda p: pd.Period(p, freq="M").end_time.strftime("%Y-%m-%d")  # noqa: E731  월말 날짜로 찍음
        out["us"] = {"x": [end(p) for p in yoy.index], "yoy": [round(float(v), 1) for v in yoy],
                     "ix": [end(p) for p in s.index], "iy": [round(float(v), 2) for v in s]}
    kp, op, lp = os.path.join(DATA, "kofia.csv"), os.path.join(DATA, "kofia_credit_old.csv"), os.path.join(DATA, "kospi_long.csv")
    if os.path.exists(kp) and os.path.exists(op) and os.path.exists(lp):
        c = pd.concat([pd.read_csv(op), pd.read_csv(kp)[["date", "credit_kospi"]]]).dropna().drop_duplicates("date", keep="last")
        c = c.assign(date=pd.to_datetime(c["date"])).set_index("date")["credit_kospi"].sort_index()
        c = c[c > 0]
        yoy = ((c / c.reindex(c.index - pd.DateOffset(years=1), method="ffill").values - 1) * 100)[c.index - pd.DateOffset(years=1) >= c.index[0]]
        k = pd.read_csv(lp, dtype={"date": str}).assign(date=lambda d: pd.to_datetime(d["date"])).set_index("date")["kospi"]
        k = k[k.index >= yoy.index[0]]
        cut = c.index[-1] - pd.DateOffset(years=2)
        thin = lambda v: pd.concat([v[v.index < cut].resample("W-FRI").last().dropna(), v[v.index >= cut]])  # noqa: E731
        yoy, k = thin(yoy), thin(k)
        out["kr"] = {"x": list(yoy.index.strftime("%Y-%m-%d")), "yoy": [round(float(v), 1) for v in yoy],
                     "ix": list(k.index.strftime("%Y-%m-%d")), "iy": [round(float(v), 2) for v in k]}
    return out or None


# ---------- CNN 공포·탐욕 지수 (CNN 사이트가 쓰는 공개 데이터 주소, 비공식) ----------
CNN_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36",
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "en-US,en;q=0.9",
    "Origin": "https://edition.cnn.com",
    "Referer": "https://edition.cnn.com/",
    "sec-ch-ua": '"Chromium";v="130", "Google Chrome";v="130", "Not?A_Brand";v="99"',
    "sec-ch-ua-mobile": "?0",
    "sec-ch-ua-platform": '"Windows"',
    "Sec-Fetch-Dest": "empty",
    "Sec-Fetch-Mode": "cors",
    "Sec-Fetch-Site": "cross-site",
}


def fetch_fear_greed():
    path = os.path.join(DATA, "fear_greed.csv")
    old = pd.read_csv(path, dtype={"date": str}) if os.path.exists(path) else None
    # 처음엔 2021년부터 전체, 그 뒤엔 최근 1년치만 받아서 합침
    starts = ["/2021-01-01"] if old is None or old.empty else [""]
    j = None
    for s in starts + [""]:
        r = requests.get("https://production.dataviz.cnn.io/index/fearandgreed/graphdata" + s,
                         headers=CNN_HEADERS, timeout=60)
        if r.status_code == 200:
            j = r.json()
            break
    if j is None:
        raise RuntimeError(f"CNN 응답 오류 {r.status_code}")
    rows = [{"date": datetime.fromtimestamp(p["x"] / 1000, timezone.utc).strftime("%Y-%m-%d"),
             "score": round(float(p["y"]), 1)} for p in j["fear_and_greed_historical"]["data"]]
    now = j["fear_and_greed"]
    ts = pd.Timestamp(now["timestamp"]).tz_convert("America/New_York").strftime("%Y-%m-%d")
    rows.append({"date": ts, "score": round(float(now["score"]), 1)})
    new = pd.DataFrame(rows)
    if old is not None:
        new = pd.concat([old, new])
    new.drop_duplicates("date", keep="last").sort_values("date").to_csv(path, index=False)
    with open(os.path.join(DATA, "fear_greed_now.json"), "w", encoding="utf-8") as f:
        json.dump({"score": round(float(now["score"]), 1), "rating": now["rating"], "timestamp": now["timestamp"],
                   "previous_close": now.get("previous_close"), "previous_1_week": now.get("previous_1_week"),
                   "previous_1_month": now.get("previous_1_month"), "previous_1_year": now.get("previous_1_year")}, f)


# ---------- Silicon Data 지수 (GPU 임대·LLM 토큰 가격, 공개 페이지에 실린 최근 30일치를 매일 저장) ----------
SILICON_SERIES = {  # 페이지 표기 -> 저장 이름
    "A100(SDA100RT)": "A100", "H100(SDH100RT)": "H100", "H200": "H200", "B200(SDB200RT)": "B200", "B300": "B300",
    "LLM Token(SDLLMTK)": "LLM Token", "Open LLM": "Open LLM", "Proprietary LLM": "Proprietary LLM",
}


def fetch_silicon():
    html = get("https://www.silicondata.com/products/silicon-index?gpu=h100").text.replace('\\"', '"')
    rows = []
    for m in re.finditer(r'"(?:title|name|label|ticker|symbol)":"([^"]{1,60})"[^\[]{0,400}?"points":\[(.*?)\]', html):
        name = SILICON_SERIES.get(m.group(1))
        if not name:
            continue
        for d, p in re.findall(r'"date":"(\d{4}-\d\d-\d\d)","price":([\d.]+)', m.group(2)):
            rows.append({"date": d, "series": name, "price": float(p)})
    got = {r["series"] for r in rows}
    if len(got) < 5:
        raise RuntimeError(f"Silicon Data 지수를 일부만 찾음({sorted(got)}) — 사이트 구조 변경?")
    path = os.path.join(DATA, "silicon.csv")
    old = pd.read_csv(path) if os.path.exists(path) else pd.DataFrame(columns=["date", "series", "price"])
    allr = pd.concat([old, pd.DataFrame(rows)]).drop_duplicates(["date", "series"], keep="last")
    allr.sort_values(["series", "date"]).to_csv(path, index=False)


# ---------- ADR(등락비율) — adrinfo.kr 차트 페이지에 실린 일별 값 (2019-10~) ----------
def fetch_adr():
    html = get("http://adrinfo.kr/chart").text
    rows = []
    for market in ("kospi", "kosdaq"):
        m = re.search(market + r"_adr\s*=\s*(\[.*?\])\s*;", html, re.S)  # 배열은 '[[..], [..], ];' 로 끝남
        if not m:
            raise RuntimeError(f"{market} ADR 데이터를 못 찾음 (사이트 구조 변경?)")
        for ms, v in re.findall(r"\[(\d{12,13}),\s*([\d.]+)\]", m.group(1)):  # 값 없는 날(null 등)은 건너뜀
            day = datetime.fromtimestamp(int(ms) / 1000, KST).strftime("%Y-%m-%d")
            rows.append({"date": day, "market": market, "adr": float(v)})
    path = os.path.join(DATA, "adr.csv")
    old = pd.read_csv(path) if os.path.exists(path) else pd.DataFrame(columns=["date", "market", "adr"])
    allr = pd.concat([old, pd.DataFrame(rows)]).drop_duplicates(["date", "market"], keep="last")
    allr.sort_values(["market", "date"]).to_csv(path, index=False)


# ---------- 네비우스 GPU 현물(스팟) 가격 — 공개 API, 인증 없음, 최근 30일 시간별 ----------
NEBIUS_API = "https://api.nebius.cloud/billing/v1alpha1/spot-price"


def _nebius_model(platform):
    m = re.search(r"gpu-([a-z]+\d+)", platform or "")
    return m.group(1).upper() if m else None


def fetch_nebius():
    start = (datetime.now(timezone.utc) - timedelta(days=29)).strftime("%Y-%m-%dT%H:00:00Z")
    j = get(NEBIUS_API + "/list-series", params={"filters.startTime": start, "aggregation.granularity": "GRANULARITY_HOUR_1",
                                                 "aggregation.method": "METHOD_AVG_PRICE", "pageSize": 100}, timeout=120).json()
    rows = []
    for s in j.get("items", []):
        model = _nebius_model(s["computeInstance"]["platform"])
        if model not in ("B300", "B200", "H200", "H100"):
            continue
        for p in s.get("points", []):
            rows.append({"time": p["timestamp"][:16], "model": model, "region": s["region"], "usd": float(p["value"])})
    if not rows:
        raise RuntimeError("네비우스 현물가격 응답이 비어 있음")
    path = os.path.join(DATA, "nebius_spot.csv")  # 시간별·지역별 기록을 계속 쌓음 (API는 30일만 줌)
    old = pd.read_csv(path) if os.path.exists(path) else pd.DataFrame(columns=["time", "model", "region", "usd"])
    allr = pd.concat([old, pd.DataFrame(rows)]).drop_duplicates(["time", "model", "region"], keep="last")
    allr.sort_values(["model", "region", "time"]).to_csv(path, index=False)


# ---------- 내부자(임원·이사) 매수·매도 (Yahoo Finance가 SEC Form 4 공시를 정리한 데이터) ----------
def _insider_tickers():
    return [t["ticker"] for t in config.TICKERS if "." not in t["ticker"]]  # 일본 등 해외 상장(9984.T)은 미국식 Form 4가 없음


def _insider_kind(text):
    s = (text or "").strip().lower()
    if s.startswith("sale"):
        return "sale"
    if s.startswith("purchase"):
        return "purchase"
    if s.startswith("stock award"):
        return "award"
    if s.startswith("stock gift"):
        return "gift"
    return "other"  # 설명 없음: 세금 납부용 처분·옵션 행사 등 (금액 미표기)


def fetch_insiders():
    import yfinance as yf
    path = os.path.join(DATA, "insider_trades.csv")
    old = pd.read_csv(path, dtype={"date": str}) if os.path.exists(path) else None
    frames, errors = [], []
    for t in _insider_tickers():
        try:
            d = yf.Ticker(t).insider_transactions
            if d is None or d.empty:
                errors.append(f"{t}: 데이터 없음")
                continue
            d = d.rename(columns={"Start Date": "date", "Insider": "insider", "Position": "position", "Shares": "shares",
                                  "Value": "value", "Text": "text", "Ownership": "ownership"})
            d["ticker"] = t
            d["date"] = pd.to_datetime(d["date"]).dt.strftime("%Y-%m-%d")
            d["kind"] = d["text"].map(_insider_kind)
            frames.append(d[["ticker", "date", "insider", "position", "shares", "value", "text", "ownership", "kind"]])
        except Exception as e:  # noqa: BLE001
            errors.append(f"{t}: {e}")
        time.sleep(1)
    if not frames:
        raise RuntimeError("내부자 거래 데이터를 못 받음: " + "; ".join(errors)[:300])
    new = pd.concat(frames + ([old] if old is not None else []))
    new["text"] = new["text"].fillna("")
    # Yahoo는 최근 약 150건만 주므로 매일 받아 쌓아서 기간을 늘림
    new = new.drop_duplicates(["ticker", "date", "insider", "text", "shares", "value"], keep="first")
    new.sort_values(["ticker", "date"], ascending=[True, False]).to_csv(path, index=False)
    if errors:
        raise RuntimeError("; ".join(errors)[:300])


# ---------- VIX 지수 (Yahoo Finance) · 하이일드 스프레드 (FRED) ----------
def fetch_vix():
    import yfinance as yf
    v = yf.download("^VIX", start="2020-01-01", progress=False, auto_adjust=False)["Close"].squeeze().dropna()
    if v.empty:
        raise RuntimeError("VIX 데이터가 비어 있음")
    v.index = pd.to_datetime(v.index).strftime("%Y-%m-%d")
    v.rename("vix").rename_axis("date").round(2).to_csv(os.path.join(DATA, "vix.csv"))


def fetch_vkospi():
    # VKOSPI(코스피200 변동성지수): 인베스팅닷컴 공개 '과거 데이터' 페이지에 실린 최근 약 20거래일 종가를 매일 저장해 쌓음
    # (야후에는 없고, 한국거래소 데이터 사이트는 로그인이 필요)
    html = requests.get(VKOSPI_URL, headers=BROWSER_UA, timeout=60).text
    rows = [{"date": datetime.strptime(d, "%b %d, %Y").strftime("%Y-%m-%d"), "vkospi": float(v.replace(",", ""))}
            for d, v in re.findall(r'<time[^>]*dateTime="([^"]+)"[^<]*</time></td><td[^>]*>([\d.,]+)<', html)]
    if len(rows) < 5:
        raise RuntimeError("VKOSPI 표를 못 읽음 (사이트 구조 변경?)")
    path = os.path.join(DATA, "vkospi.csv")
    old = pd.read_csv(path, dtype={"date": str}) if os.path.exists(path) else pd.DataFrame(columns=["date", "vkospi"])
    pd.concat([old, pd.DataFrame(rows)]).drop_duplicates("date", keep="last").sort_values("date").to_csv(path, index=False)


def fetch_kospi_long():
    # 코스피 1990년~ 일별 종가 (네이버 금융 차트 데이터, 야후는 1996년 12월부터라 MDD 계산엔 부족).
    # 코스닥은 2019년~ (주변자금 섹션 '전체' 기간용, 야후 코스닥은 2020년부터)
    for sym, start, min_rows in (("KOSPI", "19900101", 8000), ("KOSDAQ", "20190101", 1000)):
        r = requests.get("https://api.finance.naver.com/siseJson.naver", headers=BROWSER_UA, timeout=60,
                         params={"symbol": sym, "requestType": 1, "startTime": start,
                                 "endTime": datetime.now().strftime("%Y%m%d"), "timeframe": "day"})
        # 행 모양: ["19900104", 시가, 고가, 저가, 종가, ...]
        rows = re.findall(r'\["(\d{8})",\s*[\d.]+,\s*[\d.]+,\s*[\d.]+,\s*([\d.]+)', r.text)
        if len(rows) < min_rows or rows[0][0] > start[:6] + "10":
            raise RuntimeError(f"네이버 {sym} 장기 데이터가 이상함 ({len(rows)}행)")
        pd.DataFrame([{"date": f"{d[:4]}-{d[4:6]}-{d[6:]}", sym.lower(): float(v)} for d, v in rows]) \
            .to_csv(os.path.join(DATA, f"{sym.lower()}_long.csv"), index=False)


TV_URL = "https://finance.daum.net/api/market_index/days"


def fetch_trading_value():
    # 코스피·코스닥 일별 거래대금(백만원) — 다음 금융 지수 일별 시세. 처음엔 2020년부터 받고, 이후엔 최근 100일만 받아 합침
    path = os.path.join(DATA, "trading_value.csv")
    old = pd.read_csv(path, dtype={"date": str}).set_index("date") if os.path.exists(path) else None
    h = {**BROWSER_UA, "Referer": "https://finance.daum.net/domestic/kospi"}
    if old is not None and "kospi_idx" not in old.columns:
        old = None  # 지수 열이 없던 옛 파일이면 처음부터 다시 받음
    cols = {}
    for mk in ("KOSPI", "KOSDAQ"):
        got, idx = {}, {}
        for page in range(1, 2 if old is not None else 30):
            rows = requests.get(TV_URL, headers=h, timeout=30,
                                params={"page": page, "perPage": 100, "market": mk, "pagination": "true"}).json()["data"]
            got.update({r["date"][:10]: float(r["accTradePrice"]) for r in rows})
            idx.update({r["date"][:10]: float(r["tradePrice"]) for r in rows})
            if not rows or rows[-1]["date"] < "2020-01-01":
                break
        if len(got) < 20:
            raise RuntimeError(f"{mk} 거래대금을 못 읽음")
        cols[mk.lower()] = pd.Series(got)
        cols[mk.lower() + "_idx"] = pd.Series(idx)
    new = pd.DataFrame(cols).rename_axis("date")
    new = new[new.index >= "2020-01-01"]
    if old is not None:
        new = new.combine_first(old)
    new.sort_index().to_csv(path)


def fetch_wti():
    import yfinance as yf
    v = yf.download("CL=F", start="2020-01-01", progress=False, auto_adjust=False)["Close"].squeeze().dropna()
    if v.empty:
        raise RuntimeError("WTI 데이터가 비어 있음")
    v.index = pd.to_datetime(v.index).strftime("%Y-%m-%d")
    v.rename("wti").rename_axis("date").round(2).to_csv(os.path.join(DATA, "wti.csv"))


def fetch_usmkt():
    # 미국채 10년물 금리(%, ^TNX)·달러인덱스(DX-Y.NYB)·원/달러 환율(KRW=X). S&P500은 fetch_prices가 sp500.csv로 받음
    import yfinance as yf
    for sym, name in [("^TNX", "us10y"), ("DX-Y.NYB", "dxy"), ("KRW=X", "usdkrw")]:
        v = yf.download(sym, start="2020-01-01", progress=False, auto_adjust=False)["Close"].squeeze().dropna()
        if v.empty:
            raise RuntimeError(f"{sym} 데이터가 비어 있음")
        v.index = pd.to_datetime(v.index).strftime("%Y-%m-%d")
        v.rename(name).rename_axis("date").round(3).to_csv(os.path.join(DATA, f"{name}.csv"))
    # S&P500 월말 종가 1997년~ (미국 신용잔고 증가율 차트용, 이번 달은 지금까지 마지막 종가)
    m = yf.download("^GSPC", start="1997-01-01", interval="1mo", progress=False, auto_adjust=False)["Close"].squeeze().dropna()
    m.index = pd.to_datetime(m.index).strftime("%Y-%m")
    m.rename("sp500").rename_axis("month").round(2).to_csv(os.path.join(DATA, "sp500_monthly.csv"))


# ---------- 미국 탭 위쪽: 오늘의 미국 증시 · 올해 주가 흐름 · ETF 고점 대비 하락률 · 원유/금/비트코인 · 국채 금리 ----------
def _yahoo_chart(sym, interval, rng):
    r = requests.get(f"https://query1.finance.yahoo.com/v8/finance/chart/{sym}", params={"interval": interval, "range": rng},
                     headers=BROWSER_UA, timeout=20)
    r.raise_for_status()
    return r.json()["chart"]["result"][0]


US_TODAY = [("^GSPC", "sp500"), ("^IXIC", "nasdaq"), ("^DJI", "dow"), ("IWM", "iwm"), ("CL=F", "oil"), ("GC=F", "gold"), ("BTC-USD", "btc")]


def fetch_us_today():
    # Yahoo Finance: 지수 4개(S&P500·나스닥·다우·러셀2000 ETF IWM)는 그날 5분 단위 + 종가, 원유·금·비트코인은 마지막 값.
    # 전일 = 하루 전 일봉 종가. 그날 것만 보여 주므로 받을 때마다 덮어씀
    out = {}
    for sym, k in US_TODAY:
        d = _yahoo_chart(sym, "1d", "5d")
        m, cl = d["meta"], [c for c in d["indicators"]["quote"][0]["close"] if c is not None]
        close, prev = float(m["regularMarketPrice"]), float(cl[-2])
        o = {"close": round(close, 2), "prev": round(prev, 2), "chg": round(close - prev, 2), "pct": round((close / prev - 1) * 100, 2),
             "kst": datetime.fromtimestamp(m["regularMarketTime"], KST).strftime("%m/%d %H:%M")}
        if k in ("sp500", "nasdaq", "dow", "iwm"):
            i = _yahoo_chart(sym, "5m", "1d")
            off, reg = i["meta"]["gmtoffset"], i["meta"]["currentTradingPeriod"]["regular"]
            ts = [(t, c) for t, c in zip(i["timestamp"], i["indicators"]["quote"][0]["close"]) if c is not None]
            et = lambda t: datetime.fromtimestamp(t + off, timezone.utc)  # noqa: E731  거래소(미국 동부) 시각
            o.update(date=et(ts[-1][0]).strftime("%Y-%m-%d"), time=et(ts[-1][0]).strftime("%H:%M"),
                     status="OPEN" if reg["start"] <= time.time() < reg["end"] else "CLOSE",
                     intraday={"t": [et(t).strftime("%H:%M") for t, _ in ts], "y": [round(c, 2) for _, c in ts]})
        out[k] = o
    with open(os.path.join(DATA, "us_today.json"), "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False)


FINVIZ_STATS = {"Advancing / Declining": ("adv", "dec"), "New High / New Low": ("high", "low"),
                "Above SMA50 / Below SMA50": ("above50", "below50"), "Above SMA200 / Below SMA200": ("above200", "below200")}


def fetch_us_breadth():
    # Finviz 첫 화면(finviz.com)의 미국 전체 종목(NYSE·Nasdaq·AMEX) 상승/하락, 52주 신고가/신저가, 50일·200일선 위/아래 종목 수와 %.
    # 그날 값만 보여 주므로 장 마감 뒤에만 날마다 쌓음. 날짜 = Yahoo S&P500 마지막 거래일(미국 날짜).
    # 같은 날은 처음 받은 값(16시 = 미국 새벽)을 지킴 — 20시(미국 장 시작 전)엔 프리마켓 값이 섞일 수 있어서
    m = _yahoo_chart("^GSPC", "5m", "1d")
    reg = m["meta"]["currentTradingPeriod"]["regular"]
    if reg["start"] <= time.time() < reg["end"] + 1800:
        return
    row = {"date": datetime.fromtimestamp(m["timestamp"][-1] + m["meta"]["gmtoffset"], timezone.utc).strftime("%Y-%m-%d")}
    t = requests.get("https://finviz.com/", headers=BROWSER_UA, timeout=30).text
    for name, lp, ln, rn, rp in re.findall(r'Total &lt;b&gt;(.*?)&lt;/b&gt; issues.*?translate="no">([\d.]+)% \((\d+)\)<.*?translate="no">\((\d+)\) ([\d.]+)%<', t, re.S):
        if name in FINVIZ_STATS:
            a, b = FINVIZ_STATS[name]
            row.update({a: int(ln), a + "_pct": float(lp), b: int(rn), b + "_pct": float(rp)})
    if len(row) != 17:
        raise RuntimeError(f"Finviz 종목 수를 못 읽음 ({(len(row) - 1) // 4}/4개)")
    path = os.path.join(DATA, "us_breadth.csv")
    df = pd.DataFrame([row])
    if os.path.exists(path):
        df = pd.concat([pd.read_csv(path, dtype={"date": str}), df]).drop_duplicates("date", keep="first")
    df.sort_values("date").to_csv(path, index=False)


US_YIELDS = {"3 Mo": "m3", "1 Yr": "y1", "2 Yr": "y2", "5 Yr": "y5", "10 Yr": "y10", "30 Yr": "y30"}


def fetch_us_yields():
    # 미국 국채 만기별 금리(미 재무부 Daily Treasury Par Yield Curve, 매일 장 마감 기준). 처음엔 2020년부터, 그 뒤엔 올해(1월엔 작년도)만 받아 합침
    path, now = os.path.join(DATA, "us_yields.csv"), datetime.now(KST)
    years = range(2020, now.year + 1) if not os.path.exists(path) else sorted({now.year, now.year if now.month > 1 else now.year - 1})
    frames = []
    for yr in years:
        r = get(f"https://home.treasury.gov/resource-center/data-chart-center/interest-rates/daily-treasury-rates.csv/{yr}/all",
                params={"type": "daily_treasury_yield_curve", "field_tdr_date_value": yr, "page": "", "_format": "csv"})
        d = pd.read_csv(io.StringIO(r.text))
        d["date"] = pd.to_datetime(d["Date"], format="%m/%d/%Y").dt.strftime("%Y-%m-%d")
        frames.append(d[["date", *US_YIELDS]].rename(columns=US_YIELDS))
    df = pd.concat(frames)
    if os.path.exists(path):
        df = pd.concat([pd.read_csv(path, dtype={"date": str}), df]).drop_duplicates("date", keep="last")
    df.sort_values("date").to_csv(path, index=False)


US_ETFS = ["SPY", "QQQ", "SOXX", "DIA", "MAGS", "DRAM", "AIPO", "LYTE", "NCLD"]


def fetch_us_etfs():
    # 미국 ETF 일별 종가·장중 최고가(Yahoo, 2020년 또는 상장 뒤부터): QQQ·SOXX 섹션, 올해 주가 흐름, 고점 대비 하락률에 씀
    import yfinance as yf
    d = yf.download(US_ETFS, start="2020-01-01", progress=False, auto_adjust=False, threads=False)
    miss = [t for t in US_ETFS if d["Close"][t].dropna().empty]
    if miss:
        raise RuntimeError("ETF 주가 없음: " + ", ".join(miss))
    df = pd.concat([d["Close"][US_ETFS], d["High"][US_ETFS].add_suffix("_high")], axis=1).round(2)
    df.index = pd.to_datetime(df.index).strftime("%Y-%m-%d")
    df.rename_axis("date").to_csv(os.path.join(DATA, "us_etfs.csv"))


def build_us_today():
    # 올해 수익률 = 마지막 종가 ÷ 작년 마지막 종가 − 1 (올해 상장이면 상장 첫날 종가)
    # 고점 대비 하락률 = 마지막 종가 ÷ 2020년(또는 상장) 이후 장중 최고가 − 1
    out = {}
    tp = os.path.join(DATA, "us_today.json")
    if os.path.exists(tp):
        out.update(json.load(open(tp, encoding="utf-8")))
    for name, key, n in [("us_breadth.csv", "breadth", 260), ("us_yields.csv", "yields", None)]:
        p = os.path.join(DATA, name)
        if os.path.exists(p):
            d = pd.read_csv(p, dtype={"date": str}).sort_values("date")
            d = d.tail(n) if n else d
            out[key] = {c: [None if pd.isna(v) else v for v in d[c].tolist()] for c in d.columns}
    ep = os.path.join(DATA, "us_etfs.csv")
    if os.path.exists(ep):
        e = pd.read_csv(ep, dtype={"date": str}).set_index("date").sort_index()
        yr, etf = e.index[-1][:4], {"date": e.index[-1]}
        for t in US_ETFS:
            c, hi = e[t].dropna(), e[t + "_high"].dropna()
            before = c[c.index < yr]
            base = float(before.iloc[-1]) if len(before) else float(c.iloc[0])
            etf[t] = {"last": float(c.iloc[-1]), "ytd": round((c.iloc[-1] / base - 1) * 100, 1), "new": not len(before),
                      "high": float(hi.max()), "high_date": hi.idxmax(), "dd": round((c.iloc[-1] / hi.max() - 1) * 100, 1)}
        p = e[["SPY", "QQQ", "SOXX"]].dropna()  # 주가 흐름 차트(화면 기간 버튼 첫날 = 0%로 화면에서 계산)
        etf["px"] = {"x": list(p.index), **{t: p[t].tolist() for t in p.columns}}
        out["etf"] = etf
    return out or None


def fetch_kr_breadth():
    # 시장 온도계의 '폭' 지표: 코스피·코스닥 보통주(우선주·ETF·스팩 제외, 네이버 시총 목록) 중
    # 20일선(종가 20일 평균) 아래 종목 비율, 60일 신고가 − 신저가 종목 수(그날 고가 ≥ 60일 최고 고가 / 저가 ≤ 60일 최저 저가).
    # 네이버 일별 시세를 종목마다 1번(최근 130거래일, 약 2,600번 · 10~20초) 받아 최근 70거래일치를 다시 계산해 합침.
    # shortcut: 지금 상장된 종목으로 과거도 계산(그사이 상장폐지된 종목 빠짐), 과거를 길게 볼 땐 날마다 쌓인 값을 씀
    from concurrent.futures import ThreadPoolExecutor
    now = datetime.now(KST)
    s = requests.Session()
    s.headers.update(BROWSER_UA)

    def one(code):
        for _ in range(3):
            try:
                r = s.get(f"https://fchart.stock.naver.com/sise.nhn?symbol={code}&timeframe=day&count=130&requestType=0", timeout=20)
                return [x.split("|") for x in re.findall(r'data="([^"]+)"', r.text)]
            except Exception:  # noqa: BLE001
                time.sleep(1)
        return []
    out = []
    for mk in ("KOSPI", "KOSDAQ"):
        codes = []
        for page in range(1, 60):
            st = s.get(f"https://m.stock.naver.com/api/stocks/marketValue/{mk}?page={page}&pageSize=60", timeout=30).json().get("stocks", [])
            if not st:
                break
            codes += [x["itemCode"] for x in st if x.get("stockEndType") == "stock" and x["itemCode"].endswith("0")
                      and not re.search(r"우[A-Z]?$|우\(|스팩", x.get("stockName", ""))]
        with ThreadPoolExecutor(6) as ex:
            rows = [r for r in ex.map(one, codes) if r]
        if len(rows) < len(codes) * 0.9:
            raise RuntimeError(f"{mk} 일별 시세를 너무 적게 받음 ({len(rows)}/{len(codes)})")
        days = sorted({r[0] for rs in rows for r in rs})[-70:]
        if now.strftime("%H%M") < "1540":  # 장 마감 전엔 오늘 값이 아직 움직이는 중
            days = [d for d in days if d < now.strftime("%Y%m%d")]
        acc = {d: [0, 0, 0, 0, 0] for d in days}  # 20일 대상, 20일선 아래, 60일 대상, 신고가, 신저가
        for rs in rows:
            pos = {r[0]: i for i, r in enumerate(rs)}
            cl, hi, lo = [float(r[4]) for r in rs], [float(r[2]) for r in rs], [float(r[3]) for r in rs]
            for d in days:
                i = pos.get(d)
                if i is None or i < 19:
                    continue
                a = acc[d]
                a[0] += 1
                a[1] += cl[i] < sum(cl[i - 19:i + 1]) / 20
                if i >= 59:
                    a[2] += 1
                    a[3] += hi[i] >= max(hi[i - 59:i + 1])
                    a[4] += lo[i] <= min(lo[i - 59:i + 1])
        out += [{"date": f"{d[:4]}-{d[4:6]}-{d[6:]}", "market": mk.lower(), "n20": a[0], "below20": a[1], "n60": a[2], "highs": a[3], "lows": a[4]}
                for d, a in acc.items() if a[0]]
    path = os.path.join(DATA, "kr_breadth.csv")
    df = pd.DataFrame(out)
    if os.path.exists(path):
        old = pd.read_csv(path, dtype={"date": str})
        df = pd.concat([old, df]).drop_duplicates(["date", "market"], keep="last")
    df.sort_values(["date", "market"]).to_csv(path, index=False)


def fetch_kr_flows():
    # 시장 온도계 '수급': 네이버 증권이 보여 주는 그날 코스피·코스닥 외국인·기관·개인 순매수(억원, 마감 직후엔 잠정치).
    # 그날 값만 주므로 날마다 쌓고, 같은 날은 나중에 받은 값(20시)으로 덮음. 장 마감 전엔 움직이는 중이라 저장 안 함
    now = datetime.now(KST)
    rows = []
    for mk in ("KOSPI", "KOSDAQ"):
        j = requests.get(f"https://m.stock.naver.com/api/index/{mk}/trend", headers=BROWSER_UA, timeout=20).json()
        d, num = j["bizdate"], (lambda k: int(j[k].replace(",", "")))
        if d == now.strftime("%Y%m%d") and now.strftime("%H%M") < "1540":
            return
        rows.append({"date": f"{d[:4]}-{d[4:6]}-{d[6:]}", "market": mk.lower(),
                     "foreign": num("foreignValue"), "inst": num("institutionalValue"), "indiv": num("personalValue")})
    path = os.path.join(DATA, "kr_flows.csv")
    df = pd.DataFrame(rows)
    if os.path.exists(path):
        df = pd.concat([pd.read_csv(path, dtype={"date": str}), df]).drop_duplicates(["date", "market"], keep="last")
    df.sort_values(["date", "market"]).to_csv(path, index=False)


def fetch_kr_today():
    # 한국 탭 맨 위 '오늘의 지수': 네이버 증권 시장 화면(stock.naver.com/market/stock/kr)과 같은 자료 —
    # 코스피·코스닥 종가·등락, 개인·외국인·기관 순매수(억원), 상승·보합·하락 종목 수(상한가·하한가), 그날 1분 단위 지수.
    # 그날 것만 주므로 받을 때마다 덮어씀
    num = lambda v: float(str(v).replace(",", "").replace("+", ""))  # noqa: E731
    out = {}
    for mk in ("KOSPI", "KOSDAQ"):
        b = get(f"https://m.stock.naver.com/api/index/{mk}/basic", timeout=20).json()
        it = get(f"https://m.stock.naver.com/api/index/{mk}/integration", timeout=20).json()
        day = b["localTradedAt"][:10].replace("-", "")
        mins = get(f"https://api.stock.naver.com/chart/domestic/index/{mk}/minute", timeout=20,
                   params={"startDateTime": day + "0900", "endDateTime": day + "1600"}).json()
        ud, dt = it["upDownStockInfo"], it["dealTrendInfo"]
        out[mk.lower()] = {
            "date": f"{day[:4]}-{day[4:6]}-{day[6:]}", "time": b["localTradedAt"][11:16], "status": b["marketStatus"],
            "close": num(b["closePrice"]), "chg": num(b["compareToPreviousClosePrice"]), "pct": num(b["fluctuationsRatio"]),
            "prev": num(next(x["value"] for x in it["totalInfos"] if x["code"] == "lastClosePrice")),
            "flow": {"indiv": int(num(dt["personalValue"])), "foreign": int(num(dt["foreignValue"])), "inst": int(num(dt["institutionalValue"]))},
            "updown": {k: int(num(ud[k + "Count"])) for k in ("upper", "rise", "steady", "fall", "lower")},
            "intraday": {"t": [m["localDateTime"][8:10] + ":" + m["localDateTime"][10:12] for m in mins],
                         "y": [m["currentPrice"] for m in mins]},
        }
    with open(os.path.join(DATA, "kr_today.json"), "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False)


# 투자자별 매매동향 열 이름 ← 네이버 투자자 코드(그 화면 묶음과 같게: 외국인 = 외국인+기타외국인, 투신(사모) = 투신+사모,
# 연기금등 = 연기금+국가·지자체). 기관계 = 금융투자~연기금등 합
INVESTORS = {"indiv": ["8000"], "foreign": ["9000", "9001"], "fin": ["1000"], "ins": ["2000"], "trust": ["3000", "3100"],
             "bank": ["4000"], "otherfin": ["5000"], "pension": ["6000", "7000"], "corp": ["7100"]}


def fetch_kr_investors():
    # 투자자별 매매동향(네이버 증권 stock.naver.com/market/stock/kr/trend/trader, KRX): 일별 순매수(억원).
    # 한 번에 최근 100거래일을 받아 쌓음(같은 날은 나중 값으로). 장 마감 전 오늘 값은 움직이는 중이라 뺌
    now = datetime.now(KST)
    rows = []
    for mk in ("KOSPI", "KOSDAQ"):
        j = get("https://stock.naver.com/api/domestic/market/trend/daily", timeout=30,
                params={"tradeType": "KRX", "marketType": mk, "bizdate": now.strftime("%Y%m%d"), "startIdx": 0, "pageSize": 100}).json()
        for d in j["content"]:
            if d["bizdate"] == now.strftime("%Y%m%d") and now.strftime("%H%M") < "1540":
                continue
            v = {a["investorGubun"]: int(a["diffValue"]) for a in d["netAmounts"]}
            r = {k: round(sum(v.get(c, 0) for c in cs) / 1e8, 2) for k, cs in INVESTORS.items()}
            r["inst"] = round(sum(r[k] for k in ("fin", "ins", "trust", "bank", "otherfin", "pension")), 2)
            rows.append({"date": f"{d['bizdate'][:4]}-{d['bizdate'][4:6]}-{d['bizdate'][6:]}", "market": mk.lower(), **r})
    if len(rows) < 100:
        raise RuntimeError(f"투자자별 매매동향을 너무 적게 받음 ({len(rows)}행)")
    path = os.path.join(DATA, "kr_investors.csv")
    df = pd.DataFrame(rows)
    if os.path.exists(path):
        df = pd.concat([pd.read_csv(path, dtype={"date": str}), df]).drop_duplicates(["date", "market"], keep="last")
    df.sort_values(["date", "market"]).to_csv(path, index=False)


def build_kr_investors():
    path = os.path.join(DATA, "kr_investors.csv")
    if not os.path.exists(path):
        return None
    df = pd.read_csv(path, dtype={"date": str})
    out = {}
    for mk in ("kospi", "kosdaq"):
        m = df[df["market"] == mk].sort_values("date").tail(260)
        lp = os.path.join(DATA, f"{mk}_long.csv")
        idx = pd.read_csv(lp, dtype={"date": str}).set_index("date")[mk] if os.path.exists(lp) else pd.Series(dtype=float)
        out[mk] = {"x": list(m["date"]), **{k: [round(float(v), 1) for v in m[k]] for k in ["inst", *INVESTORS]},
                   "idx": [None if pd.isna(idx.get(d)) else float(idx.get(d)) for d in m["date"]]}
    return out


def build_kr_thermo():
    # 시장 온도계: 온도 = (지수 ÷ 50일 평균 × 100 − 90) × 4 를 0~100도로 자름 → 50일 이격 110 = 80도(과열 경고선), 90 이하 = 0도.
    # 사용자가 보내 준 다른 채널 '시장 온도계'(10/28·10/29 값)와 같은 식임을 확인(README)
    tvp, bp = os.path.join(DATA, "trading_value.csv"), os.path.join(DATA, "kr_breadth.csv")
    if not os.path.exists(tvp):
        return None
    t = pd.read_csv(tvp, dtype={"date": str}).set_index("date").sort_index()
    b = pd.read_csv(bp, dtype={"date": str}) if os.path.exists(bp) else None
    fp = os.path.join(DATA, "kr_flows.csv")
    f = pd.read_csv(fp, dtype={"date": str}) if os.path.exists(fp) else None
    out = {}
    for c in ("kospi", "kosdaq"):
        idx = t[c + "_idx"].dropna()
        ma = idx.rolling(50).mean()
        disp = (idx / ma * 100).dropna()
        temp = ((disp - 90) * 4).clip(0, 100)
        o = {"x": list(disp.index), "temp": [round(float(v), 1) for v in temp], "disp": [round(float(v), 2) for v in disp],
             "last": round(float(idx.iloc[-1]), 2), "chg": round(float(idx.iloc[-1] / idx.iloc[-2] - 1) * 100, 2), "ma50": round(float(ma.iloc[-1]), 1)}
        if b is not None:
            m = b[b["market"] == c].sort_values("date")
            o["breadth"] = {"x": list(m["date"]), "below_pct": [round(v, 1) for v in m["below20"] / m["n20"] * 100],
                            "below": m["below20"].astype(int).tolist(), "n20": m["n20"].astype(int).tolist(),
                            "net": (m["highs"] - m["lows"]).astype(int).tolist(), "n60": m["n60"].astype(int).tolist()}
        if f is not None and (f["market"] == c).any():
            r = f[f["market"] == c].sort_values("date").iloc[-1]
            o["flow"] = {"date": r["date"], **{k: int(r[k]) for k in ("foreign", "inst", "indiv")}}
        out[c] = o
    return out


WISE = "https://navercomp.wisereport.co.kr/v2/company"


def fetch_kospi_bps():
    # 코스피 선행 BPS를 키움·FnGuide처럼 넓은 범위(시총 상위 약 350개 중 컨센서스 있는 종목)로 직접 계산, 하루 한 번.
    # 밸류 신호등은 상위 150개만 써서 대형주(PBR 높음) 쏠림으로 BPS가 약 3% 낮게 나옴 (2026-10-28: 4,907 vs 넓게 5,055, 키움 1배≈5,060~5,100)
    path = os.path.join(DATA, "kospi_fwd_bps.csv")
    old = pd.read_csv(path, dtype={"date": str}) if os.path.exists(path) else None
    now = datetime.now(KST)
    today = now.strftime("%Y-%m-%d")
    if old is not None and today in set(old["date"]):
        return
    s = requests.Session()
    s.headers.update(BROWSER_UA)
    stocks = []
    for page in range(1, 11):
        j = s.get(f"https://m.stock.naver.com/api/stocks/marketValue/KOSPI?page={page}&pageSize=60", timeout=30).json()
        for x in j.get("stocks", []):  # 우선주·ETF 빼고 보통주만 (밸류 신호등과 같은 기준)
            if x.get("stockEndType") == "stock" and not re.search(r"우[A-Z]?$|우\(", x.get("stockName", "")) and x["itemCode"].endswith("0"):
                stocks.append((x["itemCode"], float(str(x["marketValue"]).replace(",", ""))))
    y0 = now.year
    ys = [y0, y0 + 1, y0 + 2]
    w0 = max(0.0, (12 - (now.month - 1) - now.day / 31) / 12)  # 올해 남은 비중 (12개월 선행 = 올해·내년 가중 평균)
    cap, eq, ni, used, t0 = 0.0, {y: 0.0 for y in ys}, {y: 0.0 for y in ys}, 0, time.time()
    for code, mcap in stocks:
        if time.time() - t0 > 1500:
            break
        try:
            page = f"{WISE}/c1010001.aspx?cmp_cd={code}"
            enc = re.search(r"encparam\s*:\s*'([^']+)'", s.get(page, timeout=30).text).group(1)
            r = s.get(f"{WISE}/ajax/cF1001.aspx", headers={"Referer": page}, timeout=30,
                      params={"cmp_cd": code, "fin_typ": "0", "freq_typ": "Y", "extY": "0", "extQ": "0", "encparam": enc})
            r.encoding = "utf-8"
            tb = pd.read_html(io.StringIO(r.text))[-1]
            cols = [c[1] if isinstance(c, tuple) else c for c in tb.columns]
            names = tb.iloc[:, 0].astype(str).str.replace(r"\s+", "", regex=True)
            line = lambda label: tb.iloc[names[names == label].index[0]]  # noqa: E731
            e_row, n_row = line("자본총계(지배)"), line("당기순이익(지배)")
            yc = {int(str(c)[:4]): (i, "(E)" in str(c)) for i, c in enumerate(cols) if re.match(r"\d{4}/\d\d", str(c))}
            if any(y not in yc for y in ys) or not yc[y0 + 1][1]:
                continue
            e = [float(str(e_row.iloc[yc[y][0]]).replace(",", "")) for y in ys]
            n = [float(str(n_row.iloc[yc[y][0]]).replace(",", "")) for y in ys]
            if any(v != v for v in e + n):  # NaN
                continue
        except Exception:  # noqa: BLE001
            continue
        finally:
            time.sleep(0.3)
        cap += mcap
        used += 1
        for y, a, b in zip(ys, e, n):
            eq[y] += a
            ni[y] += b
    if used < 200:
        raise RuntimeError(f"컨센서스 있는 종목이 너무 적음 ({used}/{len(stocks)})")
    kospi = float(pd.read_csv(os.path.join(DATA, "kospi.csv"))["kospi"].iloc[-1])
    fwd_eq = w0 * eq[y0] + (1 - w0) * eq[y0 + 1]
    row = {"date": today, "used": used, "total": len(stocks), "kospi": kospi, "y0": y0,
           **{f"bps_y{i}": round(kospi * eq[y] / cap, 1) for i, y in enumerate(ys)},
           "bps_fwd": round(kospi * fwd_eq / cap, 1),
           "fwd_roe": round((w0 * ni[y0] + (1 - w0) * ni[y0 + 1]) / fwd_eq * 100, 1)}
    pd.concat([old, pd.DataFrame([row])]).to_csv(path, index=False)


def _kospi_bps_override(m):
    # 시장 탭의 코스피 PBR·BPS만 넓은 범위 계산값(fetch_kospi_bps)으로 바꿈. PER은 150개로도 1% 안쪽이라 밸류 신호등 값 그대로
    path = os.path.join(DATA, "kospi_fwd_bps.csv")
    k = m["kospi_forward"]
    if not os.path.exists(path) or not k.get("level"):
        return
    b = pd.read_csv(path, dtype={"date": str}).iloc[-1]
    if int(b["y0"]) != int(k["date"][:4]) or (pd.Timestamp(k["date"]) - pd.Timestamp(b["date"])).days > 5:
        return
    lvl, old_fb = k["level"], k["level"] / k["fwd_pbr"]
    labs = [f"{str(int(b['y0']) + i)[2:]}E" for i in range(3)]
    k["years"]["bps"] = {lab: round(float(b[f"bps_y{i}"]), 1) for i, lab in enumerate(labs)}
    k["years"]["pbr"] = {lab: round(lvl / float(b[f"bps_y{i}"]), 3) for i, lab in enumerate(labs)}
    # 소수 4자리: 화면은 2자리로 보이지만 '내 기준선'(지수 ÷ PBR)이 밴드 숫자와 반올림 차이 없이 맞게
    k["fwd_pbr"], k["fwd_roe"] = round(lvl / float(b["bps_fwd"]), 4), float(b["fwd_roe"])
    k["pbr_src"] = f"시총 상위 {int(b['total'])}개 중 {int(b['used'])}개 · {b['date']}"
    # 선행 PBR 밴드: 최근 1년은 '1년 전 값 → 지금 선행값'을 시간 비율로 이어 붙인 근사라, 끝점이 바뀐 만큼 같은 비율로 다시 이어 붙임
    B = m.get("kospi_fwd_bands")
    if B and B["f_bps"] and B["f_bps"][-1]:
        end = pd.Timestamp(B["dates"][-1])
        shift = float(b["bps_fwd"]) - old_fb
        for i, d in enumerate(B["dates"]):
            w = ((pd.Timestamp(d) - end).days + 364) / 364
            if w > 0 and B["f_bps"][i] is not None:
                B["f_bps"][i] = round(B["f_bps"][i] + shift * w, 2)
        B["note"] += " · PBR은 키움·FnGuide처럼 시총 상위 약 350개로 넓혀 계산"


VAL_REPO = "https://github.com/jejukairos/valuation-dashboard.git"


def fetch_valmarket():
    # '🚦 시장' 탭: 사용자의 다른 대시보드(밸류 신호등, 비공개 저장소)가 매일 07:30·18:30에 모은 시장 탭 숫자만 복사 → docs/market.json
    # 이 컴퓨터의 GitHub 로그인으로 받음. 보유종목 정보는 빼고 시장 탭에 쓰는 것만 옮김
    import subprocess
    repo = os.path.join(BASE, ".valuation_cache")

    def git(*a):
        return subprocess.run(["git", "-C", repo, *a], capture_output=True, check=True, timeout=300).stdout
    if not os.path.isdir(os.path.join(repo, ".git")):
        os.makedirs(repo, exist_ok=True)
        git("init", "-q")
        git("remote", "add", "origin", VAL_REPO)
    git("fetch", "-q", "--depth", "1", "--filter=blob:none", "origin", "main")
    d = json.loads(git("show", "FETCH_HEAD:site/data/dashboard.json").decode("utf-8"))
    s = d["settings"]
    out = {"updated_at": d["updated_at"], "rates": d["rates"],
           "market": {k: v for k, v in d["market"].items() if k != "manheim"},
           "settings": {k: s.get(k) for k in ("band", "index_scenarios", "kospi_pbr_marks", "kospi_bands", "kospi_notes")},
           # 크로스체크 표의 매그7 '이 사이트' 값
           "mag7_fwd_per": {c["code"]: c.get("fwd_per") for c in d["stocks"] if c.get("code") in ("GOOG", "NVDA", "MSFT", "META", "TSLA")}}
    if not out["market"].get("kospi_forward"):
        raise RuntimeError("밸류 신호등 시장 데이터가 비어 있음")
    _kospi_bps_override(out["market"])
    with open(os.path.join(DOCS, "market.json"), "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, separators=(",", ":"))


def fetch_move():
    import yfinance as yf
    v = yf.download("^MOVE", start="2002-01-01", progress=False, auto_adjust=False)["Close"].squeeze().dropna()
    if v.empty:
        raise RuntimeError("MOVE 데이터가 비어 있음")
    v.index = pd.to_datetime(v.index).strftime("%Y-%m-%d")
    v.rename("move").rename_axis("date").round(2).to_csv(os.path.join(DATA, "move.csv"))


def fetch_bubble():
    # 버블 후반부 강세장 비교(신한투자증권 원차트 재구성, 사용자 제공 26.10.30 업데이트판과 같은 방식): 나스닥(^IXIC)·필라델피아 반도체(^SOX)
    # 일별 종가(Yahoo, 1994-12~)는 매일, 1920년대 다우(FRED/NBER M1109BUSM293NNBR, 월별)는 지난 자료라 한 번만
    import yfinance as yf
    d = yf.download(["^IXIC", "^SOX"], start="1994-12-01", progress=False, auto_adjust=False, threads=False)["Close"].dropna(how="all")
    if d["^SOX"].dropna().empty or d["^IXIC"].dropna().empty:
        raise RuntimeError("나스닥·반도체 지수가 비어 있음")
    d.index = pd.to_datetime(d.index).strftime("%Y-%m-%d")
    d.rename(columns={"^IXIC": "ixic", "^SOX": "sox"}).rename_axis("date").round(2).to_csv(os.path.join(DATA, "bubble_idx.csv"))
    dp = os.path.join(DATA, "dow_1920s.csv")
    if not os.path.exists(dp):
        (fred_series("M1109BUSM293NNBR", start="1924-01-01") / 100).loc[:"1932-12-31"].rename("dow").rename_axis("date").round(2).to_csv(dp)


# 시작 달(그 달 첫 거래일 종가 = 100), 원차트 이름과 같게
BUBBLES = [("dow20", "광란의 20년대: 다우", "dow", "1924-07"), ("dot_ixic", "닷컴: 나스닥", "ixic", "1995-01"), ("dot_sox", "닷컴: 반도체(SOX)", "sox", "1995-01"),
           ("fang", "FANG: 나스닥", "ixic", "2016-07"), ("ai_sox", "AI: 반도체(SOX)", "sox", "2023-01"), ("ai_ixic", "AI: 나스닥", "ixic", "2023-01")]


def build_bubble():
    # x = 시작일부터 지난 햇수, y = 시작일 종가 = 100. 일별 자료는 주 1점(금요일) + 마지막 날. 시작 뒤 7년까지
    ip, dp = os.path.join(DATA, "bubble_idx.csv"), os.path.join(DATA, "dow_1920s.csv")
    if not (os.path.exists(ip) and os.path.exists(dp)):
        return None
    src = pd.read_csv(ip, dtype={"date": str}).set_index("date")
    src = {"ixic": src["ixic"].dropna(), "sox": src["sox"].dropna(), "dow": pd.read_csv(dp, dtype={"date": str}).set_index("date")["dow"].dropna()}
    out = []
    for key, name, col, start in BUBBLES:
        s = src[col][src[col].index >= start]
        s.index = pd.to_datetime(s.index)
        s = s[s.index < s.index[0] + pd.DateOffset(years=7)]
        base, t0 = float(s.iloc[0]), s.index[0]
        w = s if col == "dow" else pd.concat([s.resample("W-FRI").last().dropna().iloc[:-1], s.iloc[[-1]]])
        w = w[~w.index.duplicated(keep="last")].sort_index()
        pk = s.idxmax()
        out.append({"key": key, "name": name, "start": t0.strftime("%Y-%m-%d"), "d": list(w.index.strftime("%Y-%m-%d")),
                    "t": [round((i - t0).days / 365.25, 3) for i in w.index], "y": [round(float(v) / base * 100, 1) for v in w],
                    "peak": round(float(s.max()) / base * 100, 1), "peak_date": pk.strftime("%Y-%m-%d"), "peak_t": round((pk - t0).days / 365.25, 2)})
    return out


def fetch_liquidity():
    # 미국 M2 매달 계절조정(FRED M2SL, 연준 H.6, 십억 달러), 2006년부터
    (fred_series("M2SL", start="2006-01-01") / 100).rename("m2").rename_axis("date").round(1).to_csv(os.path.join(DATA, "m2.csv"))


def fetch_ici_mmf():
    # ICI(미국 자산운용협회) 주간 MMF 자산: 매주 목요일 발표(그 주 수요일 기준). 최근 20주가 든 엑셀(백만 달러)을 매일 받아
    # data/ici_mmf.csv(십억 달러)에 쌓음. 같은 주는 새 값으로(ICI가 지난주 숫자를 고치기도 함). 2026-06-17부터
    x = None
    for yr in (datetime.now(KST).year, 2025):  # 화면 링크 이름이 'mm_summary_data_2025.xls'(26.11 기준). 해가 바뀌어 이름이 바뀌어도 받게
        try:
            x = pd.read_excel(io.BytesIO(get(f"https://www.ici.org/mm_summary_data_{yr}.xls", tries=2, timeout=60).content), header=None)
            break
        except Exception:  # noqa: BLE001
            continue
    if x is None:
        raise RuntimeError("ICI 주간 MMF 엑셀을 받지 못함")
    head = x.iloc[4].astype(str)
    col = {k: head[head.str.startswith(w)].index[0] + 1 for k, w in [("total", "TOTAL"), ("inst", "INSTITUTIONAL"), ("retail", "RETAIL")]}  # 'TNA' 칸 = 이름 칸 오른쪽
    d = pd.to_datetime(x[0].astype(str).str.strip(), format="%m/%d/%Y", errors="coerce")  # 날짜 칸 끝에 빈칸이 붙어 있음
    rows = pd.DataFrame({"date": d.dt.strftime("%Y-%m-%d"), **{k: pd.to_numeric(x[c], errors="coerce") / 1000 for k, c in col.items()}})
    rows = rows.dropna().round(2)
    if len(rows) < 10 or ((rows["inst"] + rows["retail"] - rows["total"]).abs() > rows["total"] * 0.005).any():
        raise RuntimeError("ICI 주간 MMF 엑셀 구조가 바뀐 것 같음 (전체 ≠ 기관용 + 개인용)")
    path = os.path.join(DATA, "ici_mmf.csv")
    if os.path.exists(path):
        rows = pd.concat([pd.read_csv(path, dtype={"date": str}), rows]).drop_duplicates("date", keep="last")
    rows.sort_values("date").to_csv(path, index=False)


def build_liquidity():
    # MMF(ICI 주간)·M2(FRED 매달), 십억 달러. 증감·1년 전 대비는 화면에서 계산(README 공식)
    out = {}
    mp, gp = os.path.join(DATA, "ici_mmf.csv"), os.path.join(DATA, "m2.csv")
    if os.path.exists(mp):
        m = pd.read_csv(mp, dtype={"date": str}).sort_values("date")
        out["mmf"] = {"x": list(m["date"]), **{k: m[k].tolist() for k in ("total", "inst", "retail")}}
    if os.path.exists(gp):
        s = pd.read_csv(gp, dtype={"date": str}).set_index("date")["m2"].dropna()
        out["m2"] = {"x": list(s.index), "y": [round(float(v), 1) for v in s]}
    return out or None


def fetch_hy():
    # ICE BofA US High Yield Index Option-Adjusted Spread (블룸버그 하이일드 스프레드와 같은 계열의 무료 데이터, 단위 %)
    h = fred_series("BAMLH0A0HYM2") / 100
    path = os.path.join(DATA, "hy_spread.csv")
    new = h.rename("hy").rename_axis("date").to_frame()
    if os.path.exists(path):
        new = new.combine_first(pd.read_csv(path, index_col=0))  # FRED는 최근 3년만 주므로 옛 기록은 보존
    new.sort_index().round(2).to_csv(path)
    # 장기 비교용: Moody's Baa 회사채 - 10년 국채 스프레드 (1986~, 라이선스 제한 없음, 단위 %)
    b = fred_series("BAA10Y", start="1986-01-01") / 100
    b.rename("baa").rename_axis("date").round(2).to_csv(os.path.join(DATA, "baa_spread.csv"))


# ---------- 판독기 탭: AAII 심리 · CBOE 풋콜 · FINRA 신용잔고 · S&P500 이평선 위 종목 비율 · FactSet P/E ----------
# 일부 사이트는 프로그램 이름이 붙은 요청을 막아서 일반 브라우저처럼 요청
VKOSPI_URL = "https://www.investing.com/indices/kospi-volatility-historical-data"
BROWSER_UA ={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130 Safari/537.36"}


RATE_SERIES = {"us10y": "DGS10", "fedfunds": "DFF", "kr10y": "IRLTLT01KRM156N",  # 미국 10년물·실효 기준금리(일별), 한국 10년물(월별)
               "fedupper": "DFEDTARU", "fedlower": "DFEDTARL",  # 미국 기준금리 목표 범위 위·아래(일별)
               "t10y2y": "T10Y2Y"}  # 미국 장단기 금리차(10년 − 2년, %p) — 주식 4계 카드의 '임계 수준' 확인용


def fetch_rates():
    # 시장 국면 판단용 금리 (FRED, 단위 %)
    for name, sid in RATE_SERIES.items():
        (fred_series(sid, start="2020-01-01") / 100).rename(name).rename_axis("date").round(3).to_csv(os.path.join(DATA, f"rate_{name}.csv"))


KR_BASE_URL = "https://www.bok.or.kr/portal/singl/baseRate/list.do?dataSeCd=01&menuNo=200643"


def fetch_kr_base():
    # 한국은행 기준금리 변경 이력 (한국은행 공식 페이지의 표: '2026 08월 27일 3.00' 꼴)
    t = re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", get(KR_BASE_URL).text))
    rows = re.findall(r"(\d{4}) (\d{2})월 (\d{2})일 (\d+\.\d+)", t)
    if len(rows) < 10:
        raise RuntimeError("한국은행 기준금리 표를 읽지 못함")
    df = pd.DataFrame([(f"{y}-{m}-{d}", float(v)) for y, m, d, v in rows], columns=["date", "rate"])
    df.drop_duplicates("date").sort_values("date").to_csv(os.path.join(DATA, "kr_base_rate.csv"), index=False)


HYNIX_CORP = "00164779"  # 에스케이하이닉스(주) DART 고유번호
DART_REPRT = [("Q1", "11013"), ("Q2", "11012"), ("Q3", "11014"), ("Q4", "11011")]  # 1분기·반기·3분기·사업보고서


def fetch_hynix_inventory():
    # SK하이닉스 분기 매출·매출원가·재고자산(OpenDART 연결재무제표) → D램 칸 재고일수. 이미 받은 분기는 건너뜀
    key = os.environ.get("DART_API_KEY", "").strip()
    if not key:
        raise RuntimeError("DART_API_KEY 가 .env 에 없음")
    path = os.path.join(DATA, "hynix_quarterly.csv")
    old = pd.read_csv(path, dtype={"quarter": str, "rcept_no": str}) if os.path.exists(path) else None
    have = {} if old is None else {r["quarter"]: r for r in old.to_dict("records")}
    rows = []
    for y in range(2019, datetime.now(KST).year + 1):
        for q, code in DART_REPRT:
            qk = f"{y}{q}"
            if qk in have:
                continue
            r = get("https://opendart.fss.or.kr/api/fnlttSinglAcntAll.json", params={
                "crtfc_key": key, "corp_code": HYNIX_CORP, "bsns_year": y, "reprt_code": code, "fs_div": "CFS"}).json()
            if r.get("status") == "013":  # 아직 안 나온 보고서
                break
            if r.get("status") != "000":
                raise RuntimeError(f"DART {qk}: {r.get('status')} {r.get('message')}")
            acc = {}
            for it in r["list"]:
                aid = it["account_id"].replace("ifrs_", "ifrs-full_")  # 2019년 보고서는 옛 이름(ifrs_)
                ok = it["sj_div"] == "BS" if aid == "ifrs-full_Inventories" else it["sj_div"] in ("IS", "CIS")
                if ok and aid not in acc:
                    acc[aid] = float(it["thstrm_amount"].replace(",", ""))
            o = {"quarter": qk, "revenue": acc["ifrs-full_Revenue"], "cogs": acc["ifrs-full_CostOfSales"],
                 "inventory": acc["ifrs-full_Inventories"], "rcept_no": r["list"][0]["rcept_no"]}
            if q == "Q4":  # 사업보고서는 1년치 → 4분기 = 1년 − 1~3분기
                for k in ("revenue", "cogs"):
                    o[k] -= sum(float(have[f"{y}Q{i}"][k]) for i in (1, 2, 3))
            have[qk] = o
            rows.append(o)
        else:
            continue
        break
    if rows:
        df = pd.DataFrame(rows)
        if old is not None:
            df = pd.concat([old, df])
        df.sort_values("quarter").to_csv(path, index=False)


FF_CODES = "FGHJKMNQUVXZ"  # 선물 월물 코드 (1~12월)


def fetch_fed_futures():
    # 30일 연방기금 선물(CBOT ZQ) 월물 종가 → 기준금리 시장 예상(CME FedWatch 방식) 계산용.
    # 만기 지난 월물은 야후에서 사라지므로 받아 둔 값을 계속 쌓아 둠
    import yfinance as yf
    now = datetime.now(KST)
    syms = {}
    for i in range(-1, 16):
        y, m = divmod(now.year * 12 + now.month - 1 + i, 12)
        syms[f"ZQ{FF_CODES[m]}{y % 100:02d}.CBT"] = f"{y}-{m + 1:02d}"
    px = yf.download(list(syms), start="2025-01-01", progress=False, auto_adjust=False)["Close"]
    rows = [(d.strftime("%Y-%m-%d"), syms[c], round(float(p), 4)) for c in px.columns for d, p in px[c].dropna().items()]
    if len(rows) < 500:
        raise RuntimeError(f"연방기금 선물 자료가 너무 적음({len(rows)}행)")
    path = os.path.join(DATA, "fed_futures.csv")
    df = pd.DataFrame(rows, columns=["date", "month", "price"])
    if os.path.exists(path):
        df = pd.concat([pd.read_csv(path, dtype={"date": str, "month": str}), df]).drop_duplicates(["date", "month"], keep="last")
    df.sort_values(["date", "month"]).to_csv(path, index=False)


def fetch_aaii():
    # AAII 주간 투자자 심리 설문 (매주 목요일 발표). Bull-Bear Spread = 강세 응답 − 약세 응답
    r = requests.get("https://www.aaii.com/files/surveys/sentiment.xls", headers=BROWSER_UA, timeout=60)
    r.raise_for_status()
    df = pd.read_excel(io.BytesIO(r.content), sheet_name="SENTIMENT", header=None, engine="xlrd")
    df[0] = pd.to_datetime(df[0].where(df[0].map(lambda x: isinstance(x, (datetime, pd.Timestamp)))), errors="coerce")
    df = df[df[0].notna() & pd.to_numeric(df[1], errors="coerce").notna() & pd.to_numeric(df[6], errors="coerce").notna()]
    if df.empty:
        raise RuntimeError("AAII 표를 못 읽음 (파일 구조 변경?)")
    out = pd.DataFrame({"date": df[0].dt.strftime("%Y-%m-%d"), "bullish": df[1].astype(float) * 100,
                        "neutral": df[2].astype(float) * 100, "bearish": df[3].astype(float) * 100,
                        "spread": df[6].astype(float) * 100})
    out.drop_duplicates("date", keep="last").round(1).to_csv(os.path.join(DATA, "aaii.csv"), index=False)


def fetch_putcall():
    # CBOE 일별 옵션 통계의 '주식(EQUITY) 풋/콜 비율'. 하루치씩 주므로 없는 날짜만 받아 쌓음(처음엔 1년치)
    path = os.path.join(DATA, "putcall.csv")
    old = pd.read_csv(path, dtype={"date": str}) if os.path.exists(path) else pd.DataFrame(columns=["date", "equity", "total"])
    today = datetime.now(timezone(timedelta(hours=-5))).date()  # 미국 동부 기준
    d = today - timedelta(days=365)
    if len(old):
        d = max(d, datetime.strptime(old["date"].max(), "%Y-%m-%d").date() + timedelta(days=1))
    rows, errs = [], 0
    while d <= today:
        if d.weekday() < 5:
            r = requests.get(f"https://cdn.cboe.com/data/us/options/market_statistics/daily/{d.isoformat()}_daily_options",
                             headers=UA, timeout=30)
            if r.status_code == 200:  # 휴장일·아직 안 나온 날은 403
                ratios = {x.get("name"): x.get("value") for x in r.json().get("ratios", [])}
                try:
                    rows.append({"date": d.isoformat(), "equity": float(ratios["EQUITY PUT/CALL RATIO"]),
                                 "total": float(ratios.get("TOTAL PUT/CALL RATIO") or "nan")})
                except (KeyError, ValueError):
                    errs += 1
            elif r.status_code not in (403, 404):
                errs += 1
        d += timedelta(days=1)
    allr = pd.concat([old, pd.DataFrame(rows)]).drop_duplicates("date", keep="last").sort_values("date")
    if allr.empty or (today - datetime.strptime(allr["date"].max(), "%Y-%m-%d").date()).days > 7:
        raise RuntimeError(f"최근 풋/콜 비율을 못 받음 (오류 {errs}건)")
    allr.to_csv(path, index=False)


def xlsx_rows(content):
    """엑셀(xlsx) 첫 시트를 글자 목록으로 (openpyxl 없이)"""
    import xml.etree.ElementTree as ET
    import zipfile
    ns = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"
    z = zipfile.ZipFile(io.BytesIO(content))
    shared = []
    if "xl/sharedStrings.xml" in z.namelist():
        shared = ["".join(t.text or "" for t in si.iter(ns + "t")) for si in ET.fromstring(z.read("xl/sharedStrings.xml")).findall(ns + "si")]
    rows = []
    for row in ET.fromstring(z.read("xl/worksheets/sheet1.xml")).iter(ns + "row"):
        vals = []
        for c in row.findall(ns + "c"):
            v, t = c.find(ns + "v"), c.get("t")
            if t == "inlineStr":
                vals.append("".join(x.text or "" for x in c.iter(ns + "t")))
            elif v is None:
                vals.append(None)
            else:
                vals.append(shared[int(v.text)] if t == "s" else v.text)
        rows.append(vals)
    return rows


def fetch_margin():
    # FINRA 신용잔고(증권사 고객이 빌려서 산 주식 금액, 백만 달러), 매월 3주쯤 뒤 발표
    page = "https://www.finra.org/rules-guidance/key-topics/margin-accounts/margin-statistics"
    m = re.search(r'href="([^"]*margin-statistics[^"]*\.xlsx)"', requests.get(page, headers=BROWSER_UA, timeout=60).text)
    url = "https://www.finra.org" + m.group(1) if m and m.group(1).startswith("/") else (m.group(1) if m else
          "https://www.finra.org/sites/default/files/2021-03/margin-statistics.xlsx")
    r = requests.get(url, headers=BROWSER_UA, timeout=60)
    r.raise_for_status()
    rows = []
    for v in xlsx_rows(r.content):
        if len(v) < 2 or not v[0] or not v[1]:
            continue
        a = v[0].strip()
        try:
            if re.fullmatch(r"\d{4}-\d{2}", a):  # 2026-09
                month = a
            elif re.fullmatch(r"\d+(\.\d+)?", a):  # 엑셀 날짜 숫자
                month = (pd.Timestamp("1899-12-30") + pd.Timedelta(days=float(a))).strftime("%Y-%m")
            else:  # Sep-26
                month = datetime.strptime(a, "%b-%y").strftime("%Y-%m")
            rows.append({"month": month, "debit": float(v[1])})
        except ValueError:
            continue
    if len(rows) < 24:
        raise RuntimeError("FINRA 신용잔고 표를 못 읽음 (파일 구조 변경?)")
    pd.DataFrame(rows).drop_duplicates("month").sort_values("month").to_csv(os.path.join(DATA, "margin_debt.csv"), index=False)


def fetch_breadth():
    # S&P500 종목 중 50일·200일 이동평균선 위에 있는 종목 비율(%) — 지금 구성 종목(위키백과 목록)의 주가로 직접 계산
    import yfinance as yf
    html = get("https://en.wikipedia.org/wiki/List_of_S%26P_500_companies").text
    syms = pd.read_html(io.StringIO(html), attrs={"id": "constituents"})[0]["Symbol"].astype(str).str.replace(".", "-", regex=False).tolist()
    if len(syms) < 490:
        raise RuntimeError(f"S&P500 종목 목록이 {len(syms)}개뿐")
    start = (datetime.now() - timedelta(days=365 * 3)).strftime("%Y-%m-%d")
    px = yf.download(syms, start=start, progress=False, auto_adjust=True, threads=True)["Close"].dropna(axis=1, how="all")
    if px.shape[1] < len(syms) * 0.95:
        raise RuntimeError(f"주가를 받은 종목이 {px.shape[1]}/{len(syms)}개뿐")
    out = {}
    for w in (50, 200):
        sma = px.rolling(w, min_periods=w).mean()
        ok = sma.notna() & px.notna()
        n = ok.sum(axis=1)
        out[f"above{w}"] = ((px > sma) & ok).sum(axis=1) / n.where(n >= px.shape[1] * 0.9) * 100
    df = pd.DataFrame(out).dropna()
    df.index = pd.to_datetime(df.index).strftime("%Y-%m-%d")
    df.rename_axis("date").round(2).to_csv(os.path.join(DATA, "sp500_breadth.csv"))


FACTSET_PDF = ("https://advantage.factset.com/hubfs/Website/Resources%20Section/Research%20Desk/"
               "Earnings%20Insight/EarningsInsight_{}.pdf")


def fetch_pe():
    # FactSet 주간 'Earnings Insight' 보고서 첫 장의 S&P500 12개월 예상 P/E와 5년·10년 평균. 보고서마다 출처 URL 저장
    from pypdf import PdfReader
    path = os.path.join(DATA, "sp500_pe.csv")
    old = pd.read_csv(path, dtype={"date": str}) if os.path.exists(path) else pd.DataFrame(columns=["date", "pe", "avg5", "avg10", "url"])
    have = set(old["date"])
    today = datetime.now(KST).date()
    rows = []
    for wk in range(52):  # 최근 1년치 금요일(없으면 목요일) 보고서 중 아직 없는 것
        fri = today - timedelta(days=(today.weekday() - 4) % 7 + 7 * wk)
        if len(old) and wk >= 3 and fri.isoformat() < old["date"].max():
            break
        for d in (fri, fri - timedelta(days=1)):
            if d.isoformat() in have:
                break
            url = FACTSET_PDF.format(d.strftime("%m%d%y"))
            r = requests.get(url, headers=BROWSER_UA, timeout=60)
            if r.status_code != 200 or not r.content.startswith(b"%PDF"):
                continue
            txt = re.sub(r"\s+", " ", " ".join((p.extract_text() or "") for p in PdfReader(io.BytesIO(r.content)).pages[:2]))
            m = re.search(r"forward 12 ?- ?month P/E ratio for the S&P 500 is (\d+\.\d+).{0,120}?5-year average \((\d+\.\d+)\)"
                          r".{0,80}?10-year average \((\d+\.\d+)\)", txt)
            if m:
                rows.append({"date": d.isoformat(), "pe": float(m.group(1)), "avg5": float(m.group(2)),
                             "avg10": float(m.group(3)), "url": url})
            break
    allr = pd.concat([old, pd.DataFrame(rows)]).drop_duplicates("date", keep="last").sort_values("date")
    if allr.empty or (today - datetime.strptime(allr["date"].max(), "%Y-%m-%d").date()).days > 14:
        raise RuntimeError("최근 FactSet P/E 보고서를 못 읽음")
    allr.to_csv(path, index=False)


# ---------- 국민연금 자산배분 (기금운용본부 홈페이지, 매월 갱신) ----------
NPS_ASSETS = ["국내주식", "해외주식", "국내채권", "해외채권", "대체투자", "단기자금"]


def fetch_nps():
    html = get("https://fund.nps.or.kr/main.do").text
    t = re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", re.sub(r"<script[\s\S]*?</script>", " ", html)))
    m = re.search(r"기금 포트폴리오 (\d{4})년 (\d{1,2})월 말 기준", t)
    if not m:
        raise RuntimeError("기금 포트폴리오 기준월을 못 찾음 (사이트 구조 변경?)")
    month = f"{m.group(1)}-{int(m.group(2)):02d}"
    seg = t[m.end():m.end() + 800]
    rows = []
    for a in NPS_ASSETS:
        mm = re.search(a + r" ([\d.,]+) 조 원 ([\d.]+) %", seg)
        if mm:
            rows.append({"month": month, "asset": a, "trillion": float(mm.group(1).replace(",", "")),
                         "pct": float(mm.group(2))})
    if len(rows) < 5:
        raise RuntimeError("자산군별 비중을 못 찾음 (사이트 구조 변경?)")
    path = os.path.join(DATA, "nps.csv")
    old = pd.read_csv(path) if os.path.exists(path) else pd.DataFrame(columns=["month", "asset", "trillion", "pct"])
    allr = pd.concat([old, pd.DataFrame(rows)]).drop_duplicates(["month", "asset"], keep="last")
    allr.sort_values(["month", "asset"]).to_csv(path, index=False)
    # 올해 목표 포트폴리오
    tm = re.search(r"\((\d{4})년 목표 포트폴리오\)(.{0,400})", t)
    if tm:
        target = {"year": tm.group(1)}
        for a in NPS_ASSETS[:5]:
            mm = re.search(a + r" ([\d.]+)%", tm.group(2))
            if mm:
                target[a] = float(mm.group(1))
        with open(os.path.join(DATA, "nps_target.json"), "w", encoding="utf-8") as f:
            json.dump(target, f, ensure_ascii=False)


def fetch_nps_stakes():
    # 국민연금 지분율 변동: OpenDART 공시검색(최근 6개월, 주식등의대량보유상황보고서 D001)에서 보고자가 국민연금공단인 공시를 찾고,
    # 회사별 대량보유 현황 API(majorstock)에서 그 공시의 보유비율(stkrt)·직전 보고 대비 증감(stkrt_irds)을 받음. 이미 받은 공시는 다시 안 받음
    key = os.environ.get("DART_API_KEY", "").strip()
    if not key:
        raise RuntimeError("DART_API_KEY 가 .env 에 없음")
    path = os.path.join(DATA, "nps_stakes.csv")
    old = pd.read_csv(path, dtype=str) if os.path.exists(path) else pd.DataFrame(columns=["rcept_no"])
    now, found = datetime.now(KST), {}
    # 처음엔 6개월(회사 지정 없이 찾으면 한 번에 3개월까지라 두 번), 그 뒤엔 지난 31일만(전체 공시를 넘겨 봐야 해서 오래 걸림)
    for a, b in ([(184, 93), (92, 0)] if old.empty else [(31, 0)]):
        page = 1
        while True:
            j = get("https://opendart.fss.or.kr/api/list.json", timeout=30, params={
                "crtfc_key": key, "bgn_de": (now - timedelta(days=a)).strftime("%Y%m%d"), "end_de": (now - timedelta(days=b)).strftime("%Y%m%d"),
                "pblntf_ty": "D", "pblntf_detail_ty": "D001", "page_no": page, "page_count": 100}).json()
            if j.get("status") == "013":  # 조회된 데이터 없음
                break
            if j.get("status") != "000":
                raise RuntimeError(f"DART 공시검색 오류 {j.get('status')} {j.get('message')}")
            found.update({r["rcept_no"]: r["corp_code"] for r in j["list"] if "국민연금" in r["flr_nm"]})
            if page >= j["total_page"]:
                break
            page += 1
    rows = []
    for cc in sorted({c for r, c in found.items() if r not in set(old["rcept_no"])}):
        j = get("https://opendart.fss.or.kr/api/majorstock.json", timeout=30, params={"crtfc_key": key, "corp_code": cc}).json()
        rows += [{k: m.get(k, "") for k in ("rcept_no", "rcept_dt", "corp_code", "corp_name", "repror", "stkrt", "stkrt_irds", "report_resn")}
                 for m in j.get("list", []) if m["rcept_no"] in found and "국민연금" in m.get("repror", "")]
    if rows or not os.path.exists(path):
        pd.concat([old, pd.DataFrame(rows)]).drop_duplicates("rcept_no", keep="last").sort_values("rcept_no").to_csv(path, index=False)


def build_nps_stakes():
    # 최근 NPS 공시일로부터 92일 안의 공시 중 회사마다 가장 최근 것. 직전 = 지금 − 증감. 회사분할·합병·신규상장으로 생긴 변화는 뺌(사고판 게 아님).
    # 많이 늘린 10곳·많이 줄인 10곳(증감 %p 순)
    path = os.path.join(DATA, "nps_stakes.csv")
    if not os.path.exists(path):
        return None
    d = pd.read_csv(path, dtype=str)
    d["now"] = pd.to_numeric(d["stkrt"].str.replace(",", ""), errors="coerce")
    d["chg"] = pd.to_numeric(d["stkrt_irds"].str.replace(",", ""), errors="coerce")
    d = d.dropna(subset=["now", "chg"])
    if d.empty:
        return None
    last = pd.to_datetime(d["rcept_dt"]).max()
    d = d[pd.to_datetime(d["rcept_dt"]) >= last - pd.Timedelta(days=92)].sort_values("rcept_no").drop_duplicates("corp_code", keep="last")
    d = d[~d["report_resn"].fillna("").str.contains("분할|합병|신규")]
    rec = lambda x: [{"name": r.corp_name, "prev": round(r.now - r.chg, 2), "now": r.now, "chg": r.chg, "date": r.rcept_dt, "rcept_no": r.rcept_no}  # noqa: E731
                     for r in x.itertuples()]
    return {"date": last.strftime("%Y-%m-%d"), "n": len(d), "up": rec(d[d["chg"] > 0].nlargest(10, "chg")), "down": rec(d[d["chg"] < 0].nsmallest(10, "chg"))}


def change_n(s, days, tol=10):
    """각 시점의 days일 전 대비 변화율(%). 그 시점 근처(tol일 이내)에 값이 없으면 None"""
    s = s.dropna()
    idx = pd.to_datetime(s.index)
    out = []
    for i, d in enumerate(idx):
        past = s[idx <= d - pd.Timedelta(days=days)]
        if past.empty or (d - pd.Timestamp(past.index[-1])).days > days + tol:
            out.append(None)
        else:
            out.append(round((s.iloc[i] / past.iloc[-1] - 1) * 100, 2))
    return out


def change_4w(s):
    """각 시점의 4주(28일) 전 대비 변화율(%) — 기울기가 완만해지는지 보는 용도"""
    return change_n(s, 28)


# ---------- 화면용 data.json 만들기 ----------
def pct_change(s, days):
    s = s.dropna()
    if s.empty:
        return None
    last_date = pd.Timestamp(s.index[-1])
    past = s[pd.to_datetime(s.index) <= last_date - pd.Timedelta(days=days)]
    # 비교 시점 근처(10일 이내)에 값이 없으면 계산하지 않음 (띄엄띄엄한 데이터에서 엉뚱한 비교 방지)
    if past.empty or (last_date - pd.Timedelta(days=days) - pd.Timestamp(past.index[-1])).days > 10:
        return None
    return round((s.iloc[-1] / past.iloc[-1] - 1) * 100, 2)


def build_cds_board(cds):
    # 'CDS 단계' 표: 기록된 회사마다 가장 최근 CDS(같은 날이면 마지막 줄 = 종가)·단계(config.CDS_TIERS)·우리 기록 중 최고. CDS 높은 순
    names = {t["ticker"]: t["name"] for t in config.TICKERS} | config.CDS_EXTRA
    rows = []
    for tk, nm in names.items():
        c = cds[cds["ticker"] == tk]
        if not len(c):
            continue
        last, mx = c.iloc[-1], c.loc[c["bp"].idxmax()]
        rows.append({"ticker": tk, "name": nm, "bp": float(last["bp"]), "date": last["date"], "src": last["source_url"],
                     "tier": next(n for lo, n in config.CDS_TIERS if last["bp"] >= lo), "max": float(mx["bp"]), "max_date": mx["date"],
                     "risk": risk_level(float(last["bp"]), last["date"], None, None)[0]})  # 종목 카드 없는 회사는 CDS만으로 색
    return {"tiers": config.CDS_TIERS, "rows": sorted(rows, key=lambda r: -r["bp"])}


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


def _series(s):
    s = s.dropna().sort_index()
    return {"x": list(s.index), "y": [round(float(v), 3) for v in s.values], "chg4w": change_4w(s)}


def build_dram():
    out = []
    wk_path, dl_path = os.path.join(DATA, "dram_weekly.csv"), os.path.join(DATA, "dram_daily.csv")
    wk = pd.read_csv(wk_path) if os.path.exists(wk_path) else pd.DataFrame(columns=["date", "chip", "usd", "source_url"])
    dl = pd.read_csv(dl_path) if os.path.exists(dl_path) else pd.DataFrame(columns=["date", "item", "usd"])
    news_path = os.path.join(BASE, "dram_log.csv")
    news = pd.read_csv(news_path, dtype=str).fillna("") if os.path.exists(news_path) else pd.DataFrame(
        columns=["date", "item", "usd", "source_url", "note"])
    news = news[news["source_url"].str.startswith("http")]
    news["usd"] = pd.to_numeric(news["usd"], errors="coerce")
    specs = [
        ("DXI", "DXI 지수 (D램 전체 가격지수)", "DRAMeXchange 첫 화면 공개값(2026-10-16부터 매일 자동 기록, 과거치는 유료)"),
        ("DDR5 16Gb", "DDR5 16Gb 현물 (2Gx8, 현재 주력)", "DRAMeXchange 일별 시세(2026-10-16부터 자동 기록) + 뉴스 인용 수치"),
        ("DDR4 8Gb", "DDR4 8Gb 현물 (1Gx8, 과거 기준칩)",
         "TrendForce 주간 'Memory Spot Price Update' 기사(2024-11까지 2666MT/s, 이후 3200MT/s 기준) + DRAMeXchange 일별 시세"),
    ]
    for item, label, src in specs:
        parts = []
        if item == "DDR4 8Gb" and len(wk):
            parts.append(pd.Series(wk["usd"].values, index=wk["date"].values))
        d = dl[dl["item"] == item]
        if len(d):
            parts.append(pd.Series(d["usd"].values, index=d["date"].values))
        n = news[news["item"] == item]
        if len(n):
            parts.append(pd.Series(n["usd"].values, index=n["date"].values))
        if not parts:
            continue
        s = pd.concat(parts)
        s = s[~s.index.duplicated(keep="last")].sort_index()
        ser = _series(s)
        # 작년 같은 때 대비, 석 달 전 대비, 한 달 전 대비
        yoy, qoq, mom = change_n(s, 365, 14), change_n(s, 91, 14), change_n(s, 30, 10)
        entry = {"item": item, "label": label, "source": src, **ser,
                 "last": ser["y"][-1], "last_date": ser["x"][-1],
                 "m1": pct_change(s, 30), "m3": pct_change(s, 91),
                 "yoy": yoy, "qoq": qoq, "mom": mom, "yoy_last": yoy[-1], "qoq_last": qoq[-1], "mom_last": mom[-1]}
        # 같은 칩의 계약(고정거래)가격을 함께 표시: 시세표 자동 기록 + dram_log.csv(증권사 글 등) 기록
        c = pd.concat([dl[dl["item"] == item + " 계약"][["date", "usd"]], news[news["item"] == item + " 계약"][["date", "usd"]]])
        c = c.drop_duplicates("date", keep="last").sort_values("date")
        if len(c):
            entry["contract"] = {"x": list(c["date"]), "y": [float(v) for v in c["usd"]]}
        out.append(entry)
    return out


def _money_eras(ks, kp):
    # 시기별 숫자(README): 시작 = 그 시기 직전 거래일 값(시기끼리 이어지게), 끝·최고·최저 = 그 기간 일별 값 그대로.
    # 반대매매는 하루 최대·하루 평균(억원)·합계(조원), 코스피는 기간 안 가장 깊은 하락폭·하루 최대 하락
    last_day = ks.index[-1]
    out = []
    for s, e, name in config.MONEY_ERAS:
        e = e or last_day
        pe = kp.index[-1] if e == last_day else e  # 지금 시기의 코스피는 금투협(하루 늦음)보다 하루 더 최신
        w, p = ks.loc[s:e], kp.loc[s:pe]
        if w.empty or p.empty:
            continue

        def span(ser, div, nd, end=e):
            ser = ser.dropna()
            c, before = ser.loc[s:end], ser[ser.index < s]
            st = before.iloc[-1] if len(before) else c.iloc[0]
            return {"start": round(st / div, nd), "end": round(c.iloc[-1] / div, nd),
                    "max": [round(c.max() / div, nd), c.idxmax()], "min": [round(c.min() / div, nd), c.idxmin()]}
        ratio = (ks["credit"] / ks["deposit"] * 100).dropna()
        r = ratio.loc[s:e]
        f = w["forced"].dropna() / 100
        day = (kp.pct_change() * 100).loc[s:pe]
        k = span(kp, 1, 0, pe)
        out.append({"start": s, "end": e, "name": name, "ongoing": e == last_day,
                    "kospi": {**k, "chg": round((k["end"] / k["start"] - 1) * 100, 1),
                              "dd": round(float((p / p.cummax() - 1).min() * 100), 1),
                              "worst": [round(float(day.min()), 2), day.idxmin()]},
                    "deposit": span(ks["deposit"], 1e6, 1), "credit": span(ks["credit"], 1e6, 1),
                    "ratio": {"max": [round(r.max(), 1), r.idxmax()], "min": [round(r.min(), 1), r.idxmin()], "end": round(r.iloc[-1], 1)},
                    "forced": {"max": [int(round(f.max())), f.idxmax()], "avg": int(round(f.mean())), "sum": round(f.sum() / 1e4, 2)}})
    return out


def build_kofia():
    path = os.path.join(DATA, "kofia.csv")
    if not os.path.exists(path):
        return None
    k = pd.read_csv(path, dtype={"date": str}).sort_values("date")
    k = k[k["date"] >= "2015-01-01"]
    num = lambda c, div, nd=1: [None if pd.isna(v) else round(v / div, nd) for v in k[c]]  # noqa: E731
    last = k.dropna(subset=["forced"]).iloc[-1]
    one_y = k[k["date"] >= (pd.Timestamp(last["date"]) - pd.Timedelta(days=365)).strftime("%Y-%m-%d")]
    forced_avg_1y = one_y["forced"].mean()
    yr = last["date"][:4]  # 올해 1월 1일~최신일 반대매매 합계, 작년 1년 합계(백만원 → 조원)
    forced_ytd, forced_prev = (round(k.loc[k["date"].str[:4] == y, "forced"].sum() / 1e6, 2) for y in (yr, str(int(yr) - 1)))

    def peak(col):  # 최근 1년 최고점과 지금의 차이
        row = one_y.loc[one_y[col].idxmax()]
        now = k.dropna(subset=[col]).iloc[-1]
        return {"date": row["date"], "value": round(row[col] / 1e6, 1),
                "chg": round((now[col] / row[col] - 1) * 100, 1)}
    # 지수는 2019년~ (네이버 금융 장기 일별 종가) — 기간 버튼 '전체'와 시기별 흐름에 씀
    idx = {}
    for c in ("kospi", "kosdaq"):
        p = os.path.join(DATA, f"{c}_long.csv")
        if os.path.exists(p):
            s = pd.read_csv(p, dtype={"date": str}).set_index("date")[c]
            idx[c] = s[s.index >= "2019-01-01"]
    kospi = {"x": list(idx["kospi"].index), "y": [round(float(v), 2) for v in idx["kospi"]]} if "kospi" in idx else {"x": [], "y": []}
    # 월별 일평균 반대매매(억원): 그 달 거래일 반대매매금액의 평균. 2023-11부터
    km = k.dropna(subset=["forced"]).copy()
    km = km[km["date"] >= "2023-11-01"]
    mg = (km.assign(m=km["date"].str[:7]).groupby("m")["forced"].mean() / 100).round(0)
    ks = k.set_index("date")
    # 코스닥 신용융자 청산 신호: 하루 감소액(억원)이 기준 이상인 날, 그날과 5거래일 뒤 코스닥
    cq = ks["credit_kosdaq"].dropna()
    dq = (cq.diff() / 100).round(0)
    qp = os.path.join(DATA, "kosdaq.csv")
    q = pd.read_csv(qp, dtype={"date": str}).set_index("date")["kosdaq"] if os.path.exists(qp) else pd.Series(dtype=float)
    liq = []
    for d, v in dq[(dq <= -config.KOSDAQ_LIQ_EOK) & (dq.index >= one_y["date"].iloc[0])].items():
        i = q.index.searchsorted(d)
        p0 = float(q.iloc[i]) if i < len(q) else None
        p5 = float(q.iloc[i + 5]) if i + 5 < len(q) else None
        liq.append({"date": d, "drop": int(-v), "kosdaq": None if p0 is None else round(p0, 1),
                    "after5": None if p5 is None else round((p5 / p0 - 1) * 100, 1)})
    yr_q = cq[cq.index >= one_y["date"].iloc[0]]
    wk = lambda c: None if _level_at(ks[c].dropna(), 7) is None else round(_level_at(ks[c].dropna(), 7) / 1e6, 2)  # noqa: E731
    return {
        "monthly": {"x": list(mg.index), "y": [int(v) for v in mg.values]},
        "liq": {"threshold": config.KOSDAQ_LIQ_EOK, "events": liq, "last_date": cq.index[-1], "last_chg": int(dq.iloc[-1]),
                "balance": round(float(cq.iloc[-1]) / 1e6, 2), "min_1y": {"date": yr_q.idxmin(), "value": round(float(yr_q.min()) / 1e6, 2)},
                "max_1y": {"date": yr_q.idxmax(), "value": round(float(yr_q.max()) / 1e6, 2)}},
        "w1_val": {c: wk(c) for c in ("deposit", "credit", "credit_kospi", "credit_kosdaq")},
        "kospi": kospi,
        "kosdaq": {"x": list(idx["kosdaq"].index), "y": [round(float(v), 2) for v in idx["kosdaq"]]} if "kosdaq" in idx else None,
        "eras": _money_eras(ks, idx["kospi"]) if "kospi" in idx else [],
        # 2019년 이후 코스피가 하루에 가장 크게 빠진 날 5번
        "drops": [{"date": d, "chg": round(v, 2)} for d, v in (idx["kospi"].pct_change() * 100).nsmallest(5).items()] if "kospi" in idx else [],
        "x": list(k["date"]),
        "misu": num("misu", 100, 0),          # 억원
        "forced": num("forced", 100, 0),      # 억원
        "ratio": [None if pd.isna(v) else float(v) for v in k["ratio"]],
        "credit": num("credit", 1e6, 2),      # 조원
        "credit_kospi": num("credit_kospi", 1e6, 2),
        "credit_kosdaq": num("credit_kosdaq", 1e6, 2),
        "deposit": num("deposit", 1e6, 2),    # 조원
        "last": {"date": last["date"], "misu": round(last["misu"] / 100), "forced": round(last["forced"] / 100),
                 "ratio": float(last["ratio"]), "credit": round(last["credit"] / 1e6, 2),
                 "deposit": round(last["deposit"] / 1e6, 2), "forced_avg_1y": round(forced_avg_1y / 100),
                 "forced_ytd": forced_ytd, "forced_prev_year": forced_prev,
                 "deposit_peak": peak("deposit"), "credit_peak": peak("credit"),
                 "deposit_w1": pct_change(k.set_index("date")["deposit"], 7),
                 "credit_w1": pct_change(k.set_index("date")["credit"], 7)},
    }


def build_fg():
    path = os.path.join(DATA, "fear_greed.csv")
    if not os.path.exists(path):
        return None
    fg = pd.read_csv(path, dtype={"date": str}).sort_values("date")
    now = json.load(open(os.path.join(DATA, "fear_greed_now.json"), encoding="utf-8"))
    sp_path = os.path.join(DATA, "sp500.csv")
    sp = pd.read_csv(sp_path, dtype={"date": str}).set_index("date")["sp500"] if os.path.exists(sp_path) else pd.Series(dtype=float)
    spd = pd.to_datetime(sp.index)

    def sp_at(d, months):  # d 이후 months개월 뒤(또는 그 직전 거래일) 종가
        t = pd.Timestamp(d) + pd.DateOffset(months=months)
        if len(sp) == 0 or t > spd[-1]:
            return None
        return float(sp[spd <= t].iloc[-1])

    # 10 이하로 떨어진 '구간' 찾기 (10 이하 날들이 30일 안에 이어지면 같은 구간)
    low = fg[fg["score"] <= 10]
    episodes = []
    for d, s in zip(low["date"], low["score"]):
        if episodes and (pd.Timestamp(d) - pd.Timestamp(episodes[-1]["end"])).days <= 30:
            e = episodes[-1]
            e["end"] = d
            if s < e["min"]:
                e["min"], e["min_date"] = s, d
        else:
            episodes.append({"start": d, "end": d, "min": s, "min_date": d})
    runmax = sp.cummax() if len(sp) else sp

    def dd_at(d):  # d일에 S&P500이 직전 최고점(2020년~)보다 몇 % 낮았는지
        m = spd <= pd.Timestamp(d)
        if len(sp) == 0 or not m.any():
            return None
        return round(float(sp[m].iloc[-1] / runmax[m].iloc[-1] - 1) * 100, 1)

    for e in episodes:
        e["dd"] = dd_at(e["min_date"])
        base = sp_at(e["start"], 0)
        e["sp_start"] = base
        for m in (3, 6, 12):
            v = sp_at(e["start"], m)
            e[f"r{m}"] = round((v / base - 1) * 100, 1) if base and v else None
    return {"now": now, "x": list(fg["date"]), "y": [float(v) for v in fg["score"]], "episodes": episodes}


def _level_at(s, days):
    """마지막 날짜의 days일 전(또는 그 직전 거래일) 값. 7일 넘게 공백이면 None"""
    idx = pd.to_datetime(s.index)
    t = idx[-1] - pd.Timedelta(days=days)
    m = idx <= t
    if not m.any() or (t - idx[m][-1]).days > 7:
        return None
    return float(s[m].iloc[-1])


POS_KR = [("chief executive", "CEO"), ("chief financial", "CFO"), ("chief operating", "COO"), ("chief technology", "CTO"),
          ("president", "사장"), ("general counsel", "법무책임자"), ("director", "이사"), ("officer", "임원")]


def _pos_kr(p):
    s = (p or "").lower()
    for k, v in POS_KR:
        if k in s:
            return v
    return p or "기타"


def build_insider(tk, ins):
    """종목 하나의 내부자 매수·매도 요약 (없으면 None)"""
    if ins is None:
        return None
    df = ins[(ins["ticker"] == tk) & (ins["kind"].isin(["sale", "purchase"]))].copy()
    if df.empty:
        return {"rows": 0, "first": None}
    price = df["text"].str.extract(r"price ([\d.]+)(?: - ([\d.]+))?").astype(float)
    avg = price.mean(axis=1)
    df["amount"] = df["value"].where(df["value"].notna() & (df["value"] > 0), df["shares"] * avg)
    df["pos"] = df["position"].map(_pos_kr)
    today = datetime.now(KST).date()
    c90 = (today - timedelta(days=90)).isoformat()
    r90 = df[df["date"] >= c90]
    sell, buy = r90[r90["kind"] == "sale"], r90[r90["kind"] == "purchase"]
    cxo = r90[r90["pos"].isin(["CEO", "CFO"])]
    df["month"] = df["date"].str[:7]
    mon = df.groupby(["month", "kind"])["amount"].sum().unstack(fill_value=0)
    months = pd.period_range(df["month"].min(), today.strftime("%Y-%m"), freq="M").strftime("%Y-%m")
    mon = mon.reindex(months, fill_value=0)
    allrows = ins[ins["ticker"] == tk]
    recent = df.sort_values("date", ascending=False).head(10)
    return {
        "rows": int(len(allrows)), "first": allrows["date"].min(), "last": allrows["date"].max(),
        "sell90": round(float(sell["amount"].sum())), "sell90_n": int(len(sell)), "sell90_people": int(sell["insider"].nunique()),
        "buy90": round(float(buy["amount"].sum())), "buy90_n": int(len(buy)),
        "cxo_sell90": round(float(cxo[cxo["kind"] == "sale"]["amount"].sum())),
        "cxo_buy90": round(float(cxo[cxo["kind"] == "purchase"]["amount"].sum())),
        "cxo_n": int(len(cxo)),
        "months": list(months),
        "m_sell": [round(float(mon["sale"].get(m, 0)) / 1e6, 2) if "sale" in mon else 0 for m in months],
        "m_buy": [round(float(mon["purchase"].get(m, 0)) / 1e6, 2) if "purchase" in mon else 0 for m in months],
        "recent": [{"date": r["date"], "name": r["insider"].title(), "pos": r["pos"], "kind": r["kind"],
                    "shares": int(r["shares"]), "amount": None if pd.isna(r["amount"]) else round(float(r["amount"])),
                    "indirect": r["ownership"] == "I"} for _, r in recent.iterrows()],
    }


def build_silicon():
    path = os.path.join(DATA, "silicon.csv")
    if not os.path.exists(path):
        return None
    sd = pd.read_csv(path, dtype={"date": str})
    out = []
    for name in SILICON_SERIES.values():
        g = sd[sd["series"] == name].sort_values("date")
        if g.empty:
            continue
        s = pd.Series(g["price"].values, index=g["date"].values)
        out.append({"name": name, "x": list(s.index), "y": [float(v) for v in s.values],
                    "last": float(s.iloc[-1]), "last_date": s.index[-1],
                    "w1": pct_change(s, 7), "m1": pct_change(s, 30)})
    return out


def build_nebius():
    path = os.path.join(DATA, "nebius_spot.csv")
    if not os.path.exists(path):
        return None
    n = pd.read_csv(path)
    n["t"] = pd.to_datetime(n["time"])
    out = []
    for model in ["B300", "B200", "H200", "H100"]:
        g = n[n["model"] == model]
        if g.empty:
            continue
        avg = g.groupby("t")["usd"].mean().sort_index()  # 지역 평균
        # 30일이 넘게 쌓이면 화면은 일별 평균으로 (데이터 가볍게)
        ser = avg if (avg.index[-1] - avg.index[0]).days <= 35 else avg.resample("D").mean().dropna()
        last_t = avg.index[-1]
        past = avg[avg.index <= last_t - pd.Timedelta(days=7)]
        cur = g[g["t"] == last_t]
        out.append({"model": model, "x": [t.strftime("%Y-%m-%d %H:%M") for t in ser.index], "y": [round(float(v), 3) for v in ser.values],
                    "last": round(float(avg.iloc[-1]), 2), "last_time": last_t.strftime("%Y-%m-%d %H:%M"),
                    "min": round(float(cur["usd"].min()), 2), "max": round(float(cur["usd"].max()), 2), "regions": int(cur["region"].nunique()),
                    "d7": None if past.empty else round((float(avg.iloc[-1]) / float(past.iloc[-1]) - 1) * 100, 1)})
    return out


def build_adr():
    path = os.path.join(DATA, "adr.csv")
    if not os.path.exists(path):
        return None
    a = pd.read_csv(path, dtype={"date": str})
    out = {}
    for market in ("kospi", "kosdaq"):
        g = a[a["market"] == market].sort_values("date")
        ip = os.path.join(DATA, f"{market}.csv")
        idx = {"x": [], "y": []}
        if os.path.exists(ip):
            k = pd.read_csv(ip, dtype={"date": str})
            idx = {"x": list(k["date"]), "y": [round(float(v), 2) for v in k[market]]}
        out[market] = {"x": list(g["date"]), "y": [round(float(v), 2) for v in g["adr"]], "index": idx,
                       "recent": [{"date": d, "adr": round(float(v), 1)} for d, v in zip(g["date"].tail(10), g["adr"].tail(10))]}
    return out


def build_opinions():
    path = os.path.join(BASE, "opinion_log.csv")
    if not os.path.exists(path):
        return None
    o = pd.read_csv(path, dtype=str).fillna("")
    o = o[o["source_url"].str.startswith("http")].sort_values("date", ascending=False, kind="stable")
    cut = (datetime.now(KST).date() - timedelta(days=60)).isoformat()
    recent = o[o["date"] >= cut]
    return {"rows": o.to_dict("records"),
            "count60": {s: int((recent["stance"] == s).sum()) for s in ("강세", "중립", "약세")}}


def build_arr():
    path = os.path.join(BASE, "arr_log.csv")
    if not os.path.exists(path):
        return None
    a = pd.read_csv(path, dtype=str).fillna("")
    a = a[a["source_url"].str.startswith("http")]  # 출처 없는 숫자는 쓰지 않음
    a["arr_b"] = pd.to_numeric(a["arr_b"], errors="coerce")
    a = a.dropna(subset=["arr_b"]).sort_values("date", kind="stable")
    return [{"company": c, "rows": g[["date", "arr_b", "kind", "source_url", "note"]].to_dict("records")}
            for c, g in a.groupby("company", sort=False)]


def build_vix():
    path = os.path.join(DATA, "vix.csv")
    if not os.path.exists(path):
        return None
    v = pd.read_csv(path, dtype={"date": str}).sort_values("date")
    s = v.set_index("date")["vix"]
    yr = s[pd.to_datetime(s.index) >= pd.Timestamp(s.index[-1]) - pd.Timedelta(days=365)]
    return {"x": list(s.index), "y": [round(float(a), 2) for a in s.values],
            "last": round(float(s.iloc[-1]), 2), "last_date": s.index[-1], "prev": round(float(s.iloc[-2]), 2),
            "max_1y": {"date": yr.idxmax(), "value": round(float(yr.max()), 1)},
            "days_above_20_1y": int((yr >= 20).sum()), "days_1y": int(len(yr))}


def build_vkospi():
    path = os.path.join(DATA, "vkospi.csv")
    if not os.path.exists(path):
        return None
    s = pd.read_csv(path, dtype={"date": str}).sort_values("date").set_index("date")["vkospi"]
    if len(s) < 2:
        return None
    yr = s[pd.to_datetime(s.index) >= pd.Timestamp(s.index[-1]) - pd.Timedelta(days=365)]
    return {"x": list(s.index), "y": [round(float(a), 2) for a in s.values],
            "last": round(float(s.iloc[-1]), 2), "last_date": s.index[-1], "prev": round(float(s.iloc[-2]), 2),
            "max_1y": {"date": yr.idxmax(), "value": round(float(yr.max()), 2)}, "since": s.index[0], "url": VKOSPI_URL}


def build_mdd():
    path = os.path.join(DATA, "kospi_long.csv")
    if not os.path.exists(path):
        return None
    s = pd.read_csv(path, dtype={"date": str}).set_index("date")["kospi"]
    peak = s.cummax()
    dd = (s / peak - 1) * 100
    # 화면용은 주 단위(그 주에서 가장 깊이 빠진 날)로 줄임 — 바닥값은 그대로 남음
    di = pd.Series(dd.values, index=pd.to_datetime(dd.index))
    wk = di.resample("W-FRI").min().dropna()
    x = [d.strftime("%Y-%m-%d") for d in wk.index[:-1]] + [dd.index[-1]]
    # 하락기 = 최고점을 새로 찍은 날부터 다음 최고점 전까지. 그 안의 가장 깊은 바닥이 '큰 폭락'이면 기록
    grp = (s >= peak).cumsum()
    big, recent = [], []
    di_s = pd.Series(s.values, index=pd.to_datetime(s.index))
    since20 = (pd.Timestamp(s.index[-1]) - pd.DateOffset(years=20)).strftime("%Y-%m-%d")

    def after(b, months):  # 바닥 날짜에서 n달 뒤(그날 또는 다음 거래일) 종가까지 변화율(%)
        later = di_s[pd.Timestamp(b) + pd.DateOffset(months=months):]
        return round(float(later.iloc[0] / s[b] - 1) * 100, 1) if len(later) else None
    for g, part in dd.groupby(grp):
        if part.min() <= -10 and part.index[0] >= since20:  # 최근 20년, 10% 넘게 빠진 하락기
            b = part.idxmin()
            nxt = grp[grp == g + 1]
            recent.append({"peak_date": part.index[0], "peak": float(s[part.index[0]]), "date": b, "low": float(s[b]),
                           "mdd": round(float(part.min()), 1), "recovered": nxt.index[0] if len(nxt) else None,
                           "after": {k: after(b, n) for k, n in (("1m", 1), ("3m", 3), ("6m", 6), ("1y", 12))}})
        if part.min() <= -config.KOSPI_MDD_BIG and g < grp.iloc[-1]:  # 지금 진행 중인 하락기는 cur_low로 따로
            b = part.idxmin()
            nxt = grp[grp == g + 1]
            big.append({"date": b, "mdd": round(float(part.min()), 1), "peak_date": part.index[0],
                        "peak": float(s[part.index[0]]), "low": float(s[b]),
                        "name": config.KOSPI_MDD_NAMES.get(b[:4], ""),
                        "recovered": nxt.index[0] if len(nxt) else None})
    now = float(dd.iloc[-1])
    cur = dd[grp == grp.iloc[-1]]
    zone = next(n for lim, n in config.KOSPI_MDD_ZONES if -now < lim)
    return {"x": x, "y": [round(float(v), 1) for v in wk.values[:-1]] + [round(now, 1)],
            "last": round(now, 1), "last_date": dd.index[-1], "close": float(s.iloc[-1]),
            "peak": float(peak.iloc[-1]), "peak_date": s[s == peak.iloc[-1]].index[0],
            "cur_low": {"date": cur.idxmin(), "mdd": round(float(cur.min()), 1), "low": float(s[cur.idxmin()])},
            "zone": zone, "zones": config.KOSPI_MDD_ZONES, "big": big, "recent": recent, "big_cut": config.KOSPI_MDD_BIG,
            "deeper_pct": round(float((dd < now).mean() * 100), 1), "since": dd.index[0]}


def build_tv():
    path = os.path.join(DATA, "trading_value.csv")
    if not os.path.exists(path):
        return None
    t = pd.read_csv(path, dtype={"date": str}).set_index("date").sort_index()
    a = t[["kospi", "kosdaq"]].rolling(5).mean() / 1e6  # 5거래일 평균, 백만원 → 조원
    yr = a[pd.to_datetime(a.index) >= pd.Timestamp(a.index[-1]) - pd.Timedelta(days=365)]
    out = {"x": list(t.index), "last_date": t.index[-1], "url": "https://finance.daum.net/domestic/kospi"}
    r2 = lambda s: [None if pd.isna(v) else round(float(v), 2) for v in s]
    for c in ("kospi", "kosdaq"):
        pk = yr[c].idxmax()
        after = yr[c][pk:]  # 최근 1년 최고점 이후 가장 낮았던 5일 평균 = '거래대금 바닥'
        low = after.idxmin()
        out[c] = {"avg5": r2(a[c]), "idx": r2(t[c + "_idx"]), "last": round(float(a[c].iloc[-1]), 2),
                  "day": round(float(t[c].iloc[-1]) / 1e6, 2), "day_prev": round(float(t[c].iloc[-2]) / 1e6, 2),
                  "idx_chg": round(float(t[c + "_idx"].iloc[-1] / t[c + "_idx"].iloc[-2] - 1) * 100, 2),
                  "peak": {"date": pk, "value": round(float(yr[c].max()), 2)},
                  "low": {"date": low, "value": round(float(after.min()), 2)},
                  "new_low": bool(low in a.index[-5:])}  # 바닥이 최근 5거래일 안에 생겼으면 '바닥을 새로 깨는 중'
    return out


def build_wti():
    path = os.path.join(DATA, "wti.csv")
    if not os.path.exists(path):
        return None
    s = pd.read_csv(path, dtype={"date": str}).sort_values("date").set_index("date")["wti"]
    di = pd.to_datetime(s.index)
    yr = s[di >= di[-1] - pd.Timedelta(days=365)]
    m1 = s[di <= di[-1] - pd.Timedelta(days=30)]
    return {"x": list(s.index), "y": [round(float(a), 2) for a in s.values],
            "last": round(float(s.iloc[-1]), 2), "last_date": s.index[-1], "prev": round(float(s.iloc[-2]), 2),
            "m1": round(float(s.iloc[-1] / m1.iloc[-1] - 1) * 100, 1) if len(m1) else None,
            "max_1y": {"date": yr.idxmax(), "value": round(float(yr.max()), 2)},
            "min_1y": {"date": yr.idxmin(), "value": round(float(yr.min()), 2)}}


def build_usmkt():
    # 2020년~ 차트(화면 기간 버튼으로 자름) + 지금 값, 전일·1주 전·1달 전 대비 변화(금리는 %p 차이, 나머지는 %), S&P500은 최고점 대비 하락률
    out = {}
    for fname, col in [("sp500.csv", "sp500"), ("us10y.csv", "us10y"), ("dxy.csv", "dxy"), ("usdkrw.csv", "usdkrw"), ("us_etfs.csv", "QQQ"), ("us_etfs.csv", "SOXX")]:
        path = os.path.join(DATA, fname)
        if not os.path.exists(path):
            continue
        s = pd.read_csv(path, dtype={"date": str}).sort_values("date").set_index("date")[col].dropna()
        di, end = pd.to_datetime(s.index), pd.to_datetime(s.index[-1])
        last = float(s.iloc[-1])

        def chg(before):
            b = s[di <= before]
            if not len(b):
                return None
            return round(last - float(b.iloc[-1]), 3) if col == "us10y" else round((last / float(b.iloc[-1]) - 1) * 100, 2)
        o = {"x": list(s.index), "y": [round(float(a), 2) for a in s.values],"last": round(last, 3), "last_date": s.index[-1],
             "d1": chg(pd.to_datetime(s.index[-2])), "w1": chg(end - pd.DateOffset(weeks=1)), "m1": chg(end - pd.DateOffset(months=1))}
        if col in ("QQQ", "SOXX"):  # 토스증권과 같게: 5일 = 5거래일 전 종가, 1·3개월 = 오늘(한국 날짜)에서 30·90일 전 그날(쉬는 날이면 그 전 거래일) 종가
            today = pd.Timestamp(datetime.now(KST).date())
            o.update(d5=chg(pd.to_datetime(s.index[-6])), m1=chg(today - pd.Timedelta(days=30)), m3=chg(today - pd.Timedelta(days=90)))
        if col in ("sp500", "usdkrw"):
            o["peak"] = {"date": s.idxmax(), "value": round(float(s.max()), 2)}
            o["mdd"] = round((last / float(s.max()) - 1) * 100, 2)
        out[col.lower()] = o
    return out or None


def build_hynix_inv():
    # 재고일수(주) = 분기 말 재고자산 ÷ 그 분기 매출 × 13주 (하나증권 'SK하이닉스 재고일수 추이'와 같은 방식, README)
    path = os.path.join(DATA, "hynix_quarterly.csv")
    if not os.path.exists(path):
        return None
    d = pd.read_csv(path, dtype={"quarter": str, "rcept_no": str}).sort_values("quarter")
    jo = lambda c: [round(v / 1e12, 2) for v in d[c]]
    return {"q": [f"{s[2:4]}.{s[5]}Q" for s in d["quarter"]], "revenue": jo("revenue"), "inventory": jo("inventory"),
            "weeks": [round(i / r * 13, 1) for i, r in zip(d["inventory"], d["revenue"])],
            "url": [f"https://dart.fss.or.kr/dsaf001/main.do?rcpNo={n}" for n in d["rcept_no"]]}


# ---------- 주식 공급: 미국 기업 주식 순발행(연준) · 미국 IPO(나스닥 IPO 달력) ----------
def fetch_equity_supply():
    # 순발행 = 새로 찍은 주식(IPO·증자) − 사라진 주식(자사주 매입·현금 인수합병). 연준 자금순환표(Z.1) 미국 비금융 기업, 분기 합계(백만 달러)
    net = fred_series("BOGZ1FU103164103Q", "2019-01-01") / 100  # fred_series는 %→bp(×100)로 돌려주므로 되돌림
    net.rename("usd_mil").rename_axis("date").to_csv(os.path.join(DATA, "us_equity_net.csv"))
    # IPO: 나스닥 IPO 달력에서 공모가가 정해진(priced) 건. 처음엔 2019년부터 채우고, 이후엔 지난달~다음 달만 다시 받음
    path, upath = os.path.join(DATA, "us_ipo.csv"), os.path.join(DATA, "us_ipo_upcoming.csv")
    old = pd.read_csv(path, dtype={"deal": str, "date": str}) if os.path.exists(path) else None
    this = pd.Period(datetime.now(KST).strftime("%Y-%m"), "M")
    usd = lambda s: float(s.replace("$", "").replace(",", "")) if s else None
    day = lambda s: pd.to_datetime(s, format="%m/%d/%Y").strftime("%Y-%m-%d")
    rows, up = [], []
    for m in pd.period_range("2019-01" if old is None else this - 1, this + 1, freq="M"):
        d = requests.get(f"https://api.nasdaq.com/api/ipo/calendar?date={m}", headers={**BROWSER_UA, "Accept": "application/json"},
                         timeout=30).json()["data"]
        for r in (d.get("priced") or {}).get("rows") or []:
            rows.append({"deal": r["dealID"], "date": day(r["pricedDate"]), "name": r["companyName"],
                         "ticker": r["proposedTickerSymbol"], "usd": usd(r["dollarValueOfSharesOffered"])})
        for r in ((d.get("upcoming") or {}).get("upcomingTable") or {}).get("rows") or []:
            up.append({"date": day(r["expectedPriceDate"]), "name": r["companyName"], "ticker": r["proposedTickerSymbol"],
                       "price": r["proposedSharePrice"], "usd": usd(r["dollarValueOfSharesOffered"])})
        time.sleep(0.5)
    df = pd.DataFrame(rows)
    if old is not None:
        df = pd.concat([old, df]).drop_duplicates("deal", keep="last")
    df.sort_values(["date", "deal"]).to_csv(path, index=False)
    pd.DataFrame(up, columns=["date", "name", "ticker", "price", "usd"]).drop_duplicates().to_csv(upath, index=False)  # 예정 목록은 매번 새로


def build_equity_supply():
    # 순발행은 분기 그대로(억 달러). IPO는 달마다 공모 금액 합계(억 달러)·건수, 올해 누적 = 1월~지금 합계 (README)
    np_, ip, upath = (os.path.join(DATA, f) for f in ("us_equity_net.csv", "us_ipo.csv", "us_ipo_upcoming.csv"))
    if not os.path.exists(np_) or not os.path.exists(ip):
        return None
    n = pd.read_csv(np_, dtype={"date": str})
    i = pd.read_csv(ip, dtype={"date": str})
    i["m"] = i["date"].str[:7]
    months = [str(p) for p in pd.period_range(i["m"].min(), datetime.now(KST).strftime("%Y-%m"), freq="M")]
    g = i.groupby("m")["usd"].agg(["sum", "count"]).reindex(months, fill_value=0)
    yr = datetime.now(KST).year
    ysum = lambda y: (round(i.loc[i["date"].str[:4] == str(y), "usd"].sum() / 1e8), int((i["date"].str[:4] == str(y)).sum()))
    u = pd.read_csv(upath, dtype=str).fillna("") if os.path.exists(upath) else pd.DataFrame()
    if len(u):  # 예정일이 며칠 지난 건 빼기(가격이 정해지면 위 priced 쪽으로 옮겨 감)
        u = u[u["date"] >= (datetime.now(KST) - timedelta(days=3)).strftime("%Y-%m-%d")]
    return {"net": {"x": list(n["date"]), "y": [round(v / 100) for v in n["usd_mil"]]},
            "ipo": {"x": [m + "-01" for m in months], "y": [round(v / 1e8, 1) for v in g["sum"]], "n": [int(c) for c in g["count"]]},
            "ytd": ysum(yr), "prev": ysum(yr - 1), "year": yr,
            "upcoming": u.sort_values("date").to_dict("records") if len(u) else []}


def _steps(s):
    # 기준금리처럼 계단 모양인 값 → 바뀐 날만 [날짜, 값] (바뀐 날엔 이전 값·새 값 두 점이라 선이 계단으로 그려짐)
    out, prev = [], None
    for d, v in s.items():
        if prev is None:
            out.append([d, v])
        elif v != prev:
            out += [[d, prev], [d, v]]
        prev = v
    return out


def build_policy():
    # 미국 기준금리 목표 범위(FRED) · 한국 기준금리(한국은행) — 시장 탭 '1. 금리'의 국채 금리 아래 칸
    out = {}
    up, lo = _csv_series("rate_fedupper.csv", "fedupper"), _csv_series("rate_fedlower.csv", "fedlower")
    if up is not None and lo is not None:
        h = _steps(up.round(2))
        h.append([up.index[-1], round(float(up.iloc[-1]), 2)])
        out["us"] = {"latest": round(float(up.iloc[-1]), 2), "lower": round(float(lo.iloc[-1]), 2), "date": up.index[-1], "hist": h,
                     "source": "FRED (DFEDTARU·DFEDTARL)"}
    kr = _csv_series("kr_base_rate.csv", "rate")
    if kr is not None:
        today = datetime.now(KST).strftime("%Y-%m-%d")
        h = _steps(kr)
        h.append([today, float(kr.iloc[-1])])
        out["kr"] = {"latest": float(kr.iloc[-1]), "date": kr.index[-1], "hist": h, "source": "한국은행", "url": KR_BASE_URL}
    ts = _csv_series("rate_t10y2y.csv", "t10y2y")
    if ts is not None:
        out["t10y2y"] = {"latest": round(float(ts.iloc[-1]), 2), "date": ts.index[-1]}
    return out or None


def _fw_chain(px, t, effr, upper):
    """CME FedWatch 방식: 회의마다 기준금리(목표 범위 위쪽) 확률 분포.
    px = {"YYYY-MM": 그 달 평균 예상 금리(100 − 선물가격)}. 회의 다음 달에 회의가 없으면 그 달 값이 곧 회의 뒤 금리,
    아니면 회의 달 평균 = (회의일까지 일수 × 회의 전 금리 + 나머지 일수 × 회의 뒤 금리) ÷ 그 달 일수 를 풀어서 구함.
    회의 뒤 − 회의 전 변화를 25bp 단위 둘로 나눠(예: +0.82번 → 0번 18%·1번 82%) 앞 회의 분포에 곱해 나감"""
    mmonths = {d[:7] for d in config.FOMC_DATES}
    dist, rb, out = {round(upper, 2): 1.0}, effr, []
    for ds in [d for d in config.FOMC_DATES if d > t]:
        D = pd.Timestamp(ds)
        ym, nxt = ds[:7], (D + pd.offsets.MonthBegin(1)).strftime("%Y-%m")
        if nxt not in mmonths and nxt in px:
            ra = px[nxt]
        elif ym in px:
            N = D.days_in_month
            ra = (px[ym] - D.day / N * rb) / ((N - D.day) / N)
        else:
            break
        step = (ra - rb) / 0.25
        k = int(step // 1)
        p = step - k
        nd = {}
        for lv, q in dist.items():
            for lv2, w in ((lv + k * 0.25, 1 - p), (lv + (k + 1) * 0.25, p)):
                if w > 1e-9:
                    nd[round(lv2, 2)] = nd.get(round(lv2, 2), 0) + q * w
        dist, rb = nd, ra
        out.append((ds, dist))
    return out


def build_fedwatch():
    path = os.path.join(DATA, "fed_futures.csv")
    effr, up = _csv_series("rate_fedfunds.csv", "fedfunds"), _csv_series("rate_fedupper.csv", "fedupper")
    if not os.path.exists(path) or effr is None or up is None:
        return None
    f = pd.read_csv(path, dtype={"date": str, "month": str})
    by = {d: dict(zip(g["month"], (100 - g["price"]).round(4))) for d, g in f.groupby("date")}
    dates = sorted(by)

    def at(t):
        return _fw_chain(by[t], t, float(effr[effr.index <= t].iloc[-1]), float(up[up.index <= t].iloc[-1]))

    def mean(dist):
        return round(sum(l * q for l, q in dist.items()), 3)
    t = dates[-1]
    now = at(t)[:8]
    if not now:
        return None
    levels = sorted({l for _, dist in now for l, q in dist.items() if q >= 0.01})  # 어느 회의든 1% 넘는 금리만 칸으로
    paths = {"now": {"date": t, "x": [d for d, _ in now], "y": [mean(x) for _, x in now]}}
    for key, days in (("w1", 7), ("m1", 30)):
        c = [d for d in dates if d <= (pd.Timestamp(t) - pd.Timedelta(days=days)).strftime("%Y-%m-%d")]
        r = at(c[-1])[:8] if c else []
        if r:
            paths[key] = {"date": c[-1], "x": [d for d, _ in r], "y": [mean(x) for _, x in r]}
    # 여태까지: 약 3·6·12개월 뒤 월물의 예상 평균 금리(100 − 가격) 흐름과 실제 실효 기준금리(EFFR)
    T = pd.Timestamp(t)
    lines = []
    for mo in (3, 6, 12):
        ym = (T + pd.DateOffset(months=mo)).strftime("%Y-%m")
        g = f[f["month"] == ym]
        if len(g):
            lines.append({"month": ym, "x": list(g["date"]), "y": [round(100 - p, 3) for p in g["price"]]})
    e = effr[effr.index >= dates[0]]
    return {"date": t, "upper": round(float(up[up.index <= t].iloc[-1]), 2), "effr": round(float(effr[effr.index <= t].iloc[-1]), 2),
            "levels": levels, "table": [{"date": d, "p": [round(dist.get(l, 0) * 100, 1) for l in levels], "exp": mean(dist)} for d, dist in now],
            "paths": paths, "lines": lines, "effr_hist": {"x": list(e.index), "y": [round(float(v), 2) for v in e.values]}}


def build_move():
    path = os.path.join(DATA, "move.csv")
    if not os.path.exists(path):
        return None
    s = pd.read_csv(path, dtype={"date": str}).sort_values("date").set_index("date")["move"]
    yr = s[pd.to_datetime(s.index) >= pd.Timestamp(s.index[-1]) - pd.Timedelta(days=365)]
    return {"x": list(s.index), "y": [round(float(a), 2) for a in s.values],
            "last": round(float(s.iloc[-1]), 2), "last_date": s.index[-1], "prev": round(float(s.iloc[-2]), 2),
            "max_1y": {"date": yr.idxmax(), "value": round(float(yr.max()), 1)},
            "median_all": round(float(s.median()), 1), "since": s.index[0],
            "pct_rank": round(float((s < s.iloc[-1]).mean() * 100))}


def build_hy():
    path = os.path.join(DATA, "hy_spread.csv")
    if not os.path.exists(path):
        return None
    h = pd.read_csv(path, dtype={"date": str}).sort_values("date").dropna()
    s = h.set_index("date")["hy"]
    yr = s[pd.to_datetime(s.index) >= pd.Timestamp(s.index[-1]) - pd.Timedelta(days=365)]
    last = float(s.iloc[-1])
    d = lambda days: None if _level_at(s, days) is None else round(last - _level_at(s, days), 2)  # noqa: E731
    sd = pd.to_datetime(s.index)

    def peak(ser, idx, a, b):
        w = ser[(idx >= pd.Timestamp(a)) & (idx <= pd.Timestamp(b))]
        return None if w.empty else {"date": w.idxmax(), "value": round(float(w.max()), 2)}

    events = []
    for a, b, label in config.HY_EVENTS:
        p = peak(s, sd, a, b)
        if p:
            events.append({"label": label, **p})
    # 장기(1986~) Baa 스프레드: 주간(금요일)으로 줄여서 화면 데이터를 가볍게
    long, long_events = None, []
    bpath = os.path.join(DATA, "baa_spread.csv")
    if os.path.exists(bpath):
        bb = pd.read_csv(bpath, dtype={"date": str}).dropna()
        bs = pd.Series(bb["baa"].values, index=pd.to_datetime(bb["date"]))
        wk = bs.resample("W-FRI").last().dropna()
        long = {"x": [d.strftime("%Y-%m-%d") for d in wk.index], "y": [round(float(v), 2) for v in wk.values]}
        for a, b, label, desc, hy_txt, hy_url in config.LONG_EVENTS:
            pb = peak(bs, bs.index, a, b)
            if not pb:
                continue
            pb["date"] = pb["date"].strftime("%Y-%m-%d")
            ph = peak(s, sd, a, b)  # 최근 3년 안의 사건이면 하이일드 실제 최고치도 계산
            long_events.append({"label": label, "desc": desc, "baa": pb,
                                "hy": f"{ph['value']:.2f}% ({ph['date'][5:].replace('-', '/')})" if ph else hy_txt,
                                "hy_url": "https://fred.stlouisfed.org/series/BAMLH0A0HYM2" if ph else hy_url})
    return {"x": list(s.index), "y": [round(float(a), 2) for a in s.values], "last": round(last, 2),
            "last_date": s.index[-1], "d1w": d(7), "d1m": d(30),
            "min_1y": {"date": yr.idxmin(), "value": round(float(yr.min()), 2)},
            "max_1y": {"date": yr.idxmax(), "value": round(float(yr.max()), 2)},
            "box_top": config.HY_BOX_TOP, "events": events, "long": long, "long_events": long_events,
            "baa_last": None if long is None else long["y"][-1]}


def reader_score(v, rule):
    """config 점수표(cuts)로 1~5점"""
    for i, c in enumerate(rule["cuts"]):
        if (v <= c) if rule.get("low") else (v >= c):
            return 5 - i
    return 1


def reader_bands(rule):
    """화면에 보여줄 5점→1점 구간 글자"""
    c = [f"{x:g}" for x in rule["cuts"]]
    if rule.get("low"):
        return [f"{c[0]} 이하"] + [f"{c[i]}~{c[i + 1]}" for i in range(3)] + [f"{c[3]} 초과"]
    return [f"{c[0]} 이상"] + [f"{c[i + 1]}~{c[i]}" for i in range(3)] + [f"{c[3]} 미만"]


def reader_label(avg):
    """엑셀과 같은 판정: 1 이하 공포, 2 이하 불안, 3 이하 중립, 4 이하 과열, 그 위 광기"""
    for lim, name in zip((1, 2, 3, 4), config.READER_LABELS):
        if avg <= lim:
            return name
    return config.READER_LABELS[4]


def _csv_series(name, col, since=None):
    path = os.path.join(DATA, name)
    if not os.path.exists(path):
        return None
    d = pd.read_csv(path, dtype={"date": str}).dropna(subset=[col]).sort_values("date")
    s = pd.Series(d[col].values.astype(float), index=d["date"].values)
    return s[s.index >= since] if since else s


def _item(key, s, rule, digits=2, extra=None):
    """지표 1개: 최근 값·점수·구간·차트용 기록"""
    if s is None or s.empty:
        return {"key": key, "value": None}
    v = round(float(s.iloc[-1]), digits)
    sc = reader_score(v, rule)
    return {"key": key, "value": v, "date": s.index[-1], "score": sc, "label": config.READER_LABELS[sc - 1],
            "bands": reader_bands(rule), "cuts": rule["cuts"], "low": bool(rule.get("low")),
            "x": list(s.index), "y": [round(float(a), digits) for a in s.values], **(extra or {})}


def _hy_item():
    s = _csv_series("hy_spread.csv", "hy")
    if s is None or s.empty:
        return {"key": "hy", "value": None}
    r = config.HY_READER
    yr = s[pd.to_datetime(s.index) >= pd.Timestamp(s.index[-1]) - pd.Timedelta(days=365)]
    last, lo, hi = float(s.iloc[-1]), float(yr.min()), float(yr.max())
    if last <= r["low"]:
        sc, why = 5, f"{r['low']:g}% 이하"
    elif hi >= r["high"] and last <= hi - r["step"]:
        sc, why = 1, f"{r['high']:g}%를 넘었다가 1년 고점 {hi:.2f}%에서 {r['step']:g}%p 이상 내려옴"
    elif last > r["high"]:
        sc, why = 2, f"{r['high']:g}%를 넘어 오르는 중"
    elif last >= lo + r["step"]:
        sc, why = 4, f"1년 최저점 {lo:.2f}%보다 {r['step']:g}%p 이상 오름"
    else:
        sc, why = 3, f"{r['low']:g}~{r['high']:g}% 박스권 (1년 최저점 {lo:.2f}% 근처)"
    return {"key": "hy", "value": round(last, 2), "date": s.index[-1], "score": sc, "label": config.READER_LABELS[sc - 1],
            "why": why, "bands": [f"{r['low']:g}% 이하", f"1년 바닥 +{r['step']:g}%p", f"{r['low']:g}~{r['high']:g}% 박스",
                                  f"{r['high']:g}% 넘어 상승", f"{r['high']:g}% 넘은 뒤 고점 −{r['step']:g}%p"],
            "cuts": [r["low"], r["high"]], "x": list(s.index), "y": [round(float(a), 2) for a in s.values]}


def _summary(items):
    sc = [i["score"] for i in items if i.get("value") is not None and i.get("in_score", True)]
    if not sc:
        return None
    avg = round(sum(sc) / len(sc), 2)
    return {"score": avg, "label": reader_label(avg), "n": len(sc),
            "n_all": sum(1 for i in items if i.get("in_score", True))}


def build_readers():
    # 역발상 판독기: 사람들 마음(심리)이 한쪽으로 쏠렸는지
    margin = None
    mpath = os.path.join(DATA, "margin_debt.csv")
    if os.path.exists(mpath):
        m = pd.read_csv(mpath).sort_values("month")
        yoy = (m["debit"] / m["debit"].shift(12) - 1) * 100  # 1년(12개월) 전 대비 %
        ok = yoy.notna() & (m["month"] >= "1998-01")
        margin = pd.Series(yoy[ok].values, index=(pd.to_datetime(m["month"][ok]) + pd.offsets.MonthEnd(0)).dt.strftime("%Y-%m-%d").values)
        last_debit = float(m["debit"].iloc[-1])
        debits = [round(float(a) / 1000, 1) for a in m["debit"][ok]]  # 표용, 십억 달러
    contra = [
        _item("aaii", _csv_series("aaii.csv", "spread", since="2015-01-01"), config.CONTRA_RULES["aaii"], 1),
        _item("putcall", _csv_series("putcall.csv", "equity"), config.CONTRA_RULES["putcall"], 2),
        _item("vix", _csv_series("vix.csv", "vix"), config.CONTRA_RULES["vix"], 2),
        _item("margin", margin, config.CONTRA_RULES["margin"], 1,
              {"debit_b": round(last_debit / 1000, 1), "debits": debits} if margin is not None else None),
        _hy_item(),
    ]
    # 차트 판독기: S&P500 차트가 얼마나 달아올랐는지 (이격도·RSI는 S&P500 지수 종가로 계산)
    chart = []
    sp = _csv_series("sp500.csv", "sp500")
    br_since = None
    for key in ("above200", "above50"):
        b = _csv_series("sp500_breadth.csv", key)
        chart.append(_item(key, b, config.CHART_RULES[key], 1))
        br_since = br_since or (b.index[0] if b is not None and len(b) else None)
    direction = None
    if sp is not None and len(sp) > 260:
        ma200, ma50 = sp.rolling(200).mean(), sp.rolling(50).mean()
        gap200, gap50 = ((sp / ma200 - 1) * 100).dropna(), ((sp / ma50 - 1) * 100).dropna()
        ch = sp.diff()
        n = config.RSI_DAYS  # 와일더 방식 RSI: 오른 폭·내린 폭의 지수평균 비율
        up = ch.clip(lower=0).ewm(alpha=1 / n, adjust=False, min_periods=n).mean()
        dn = (-ch.clip(upper=0)).ewm(alpha=1 / n, adjust=False, min_periods=n).mean()
        rsi = (100 - 100 / (1 + up / dn)).dropna()
        chart += [_item("gap200", gap200, config.CHART_RULES["gap200"], 2),
                  _item("rsi", rsi, config.CHART_RULES["rsi"], 2),
                  _item("gap50", gap50, config.CHART_RULES["gap50"], 2, {"in_score": False})]
        # 방향성 (엑셀 '시장 방향성' 표): 지수가 200일선 위인지, 200일선·50일선이 20거래일 전보다 올라갔는지
        direction = {"date": sp.index[-1], "close": round(float(sp.iloc[-1]), 2),
                     "ma200": round(float(ma200.iloc[-1]), 2), "ma50": round(float(ma50.iloc[-1]), 2),
                     "above200": bool(sp.iloc[-1] > ma200.iloc[-1]),
                     "ma200_up": bool(ma200.iloc[-1] > ma200.iloc[-21]), "ma50_up": bool(ma50.iloc[-1] > ma50.iloc[-21])}
    else:
        chart += [{"key": k, "value": None} for k in ("gap200", "rsi")]
    # 장기(실적): S&P500 12개월 예상 P/E
    pe = None
    ppath = os.path.join(DATA, "sp500_pe.csv")
    if os.path.exists(ppath):
        p = pd.read_csv(ppath, dtype={"date": str}).sort_values("date")
        if len(p):
            pe = _item("pe", pd.Series(p["pe"].values.astype(float), index=p["date"].values), config.PE_RULE, 1)
            last = p.iloc[-1]
            pe.update({"avg5": float(last["avg5"]), "avg10": float(last["avg10"]), "url": last["url"],
                       "urls": list(p["url"]), "earn_yield": round(100 / float(last["pe"]), 2)})
    return {"contra": {"items": contra, "summary": _summary(contra)},
            "chart": {"items": chart, "summary": _summary(chart), "direction": direction, "pe": pe,
                      "breadth_since": br_since}}


def _arrow(chg, cuts):
    """변화량 → (화살표, 방향 +1/0/−1)"""
    if chg is None:
        return None, None
    if chg >= cuts[0]:
        return "↑", 1
    if chg >= cuts[1]:
        return "↗", 1
    if chg > cuts[2]:
        return "→", 0
    if chg > cuts[3]:
        return "↘", -1
    return "↓", -1


def _chg(s, days, pct):
    """s의 마지막 값과 days일 전(그 직전 값) 비교. pct=True면 %, 아니면 차이"""
    if s is None or len(s) < 2:
        return None
    idx = pd.to_datetime(s.index)
    prev = s[idx <= idx[-1] - pd.Timedelta(days=days)]
    if prev.empty:
        return None
    a, b = float(s.iloc[-1]), float(prev.iloc[-1])
    return round((a / b - 1) * 100, 1) if pct else round(a - b, 2)


def _match_phase(signs):
    """(금리, 실적, 주가) 방향과 가장 가까운 국면. 자료 없는 칸(None)은 빼고 비교, 동점이면 둘 다"""
    dist = {p: sum(abs(s - e) for s, e in zip(signs, pat) if s is not None) for p, pat in config.PHASES.items()}
    best = min(dist.values())
    return [p for p, d in dist.items() if d == best]


def build_phase():
    sp = _csv_series("sp500.csv", "sp500")
    pe_path = os.path.join(DATA, "sp500_pe.csv")
    eps = None
    if sp is not None and os.path.exists(pe_path):  # 예상 EPS = 보고서 전날 S&P500 종가 ÷ 예상 P/E
        spi = pd.Series(sp.values, index=pd.to_datetime(sp.index))
        pe = pd.read_csv(pe_path, dtype={"date": str}).sort_values("date")
        vals = {}
        for d, v in zip(pe["date"], pe["pe"]):
            before = spi[spi.index < pd.Timestamp(d)]
            if len(before):
                vals[d] = round(float(before.iloc[-1]) / float(v), 1)
        eps = pd.Series(vals)
    us10, kr10 = _csv_series("rate_us10y.csv", "us10y"), _csv_series("rate_kr10y.csv", "kr10y")
    ff, ks = _csv_series("rate_fedfunds.csv", "fedfunds"), _csv_series("kospi.csv", "kospi")
    out = {}
    for mkt, rate, earn, px in (("us", us10, eps, sp), ("kr", kr10, None, ks)):
        m = {"rate_last": None if rate is None else round(float(rate.iloc[-1]), 2), "rate_date": None if rate is None else rate.index[-1],
             "price_last": None if px is None else round(float(px.iloc[-1]), 1), "price_date": None if px is None else px.index[-1],
             "eps_last": None if earn is None or earn.empty else float(earn.iloc[-1]), "eps_date": None if earn is None or earn.empty else earn.index[-1]}
        for lbl, days in (("3m", 91), ("1m", 30)):
            r, e, p = _chg(rate, days, False), _chg(earn, days, True), _chg(px, days, True)
            ar, ae, ap = _arrow(r, config.PHASE_RATE_CUTS), _arrow(e, config.PHASE_PCT_CUTS), _arrow(p, config.PHASE_PCT_CUTS)
            m[lbl] = {"rate": r, "eps": e, "price": p, "arrows": [ar[0], ae[0], ap[0]],
                      "phase": _match_phase((ar[1], ae[1], ap[1])) if None not in (ar[1], ap[1]) else []}
        if px is not None:  # 최고점 대비 하락률(%) — 주식 4계 카드에서 주가 방향(−10% 이하면 ↓)
            m["dd"] = {"pct": round((float(px.iloc[-1]) / float(px.max()) - 1) * 100, 1), "peak": round(float(px.max()), 1), "peak_date": px.idxmax()}
        out[mkt] = m
    if ff is not None:
        out["us"]["fedfunds"] = round(float(ff.iloc[-1]), 2)
        out["us"]["fedfunds_6m"] = _chg(ff, 182, False)
    if eps is not None and len(eps):
        out["us"]["eps_first"] = {"date": eps.index[0], "value": float(eps.iloc[0])}
    return out


def build_nps():
    path = os.path.join(DATA, "nps.csv")
    if not os.path.exists(path):
        return None
    n = pd.read_csv(path)
    latest = n["month"].max()
    cur = n[n["month"] == latest]
    tpath = os.path.join(DATA, "nps_target.json")
    target = json.load(open(tpath, encoding="utf-8")) if os.path.exists(tpath) else {}
    hist = {a: {"x": list(g.sort_values("month")["month"]), "y": list(g.sort_values("month")["pct"])}
            for a, g in n.groupby("asset")}
    return {"month": latest, "assets": cur[["asset", "trillion", "pct"]].to_dict("records"),
            "total": round(float(cur["trillion"].sum()), 1), "target": target, "history": hist,
            "band": config.NPS_BAND}


def build():
    prices = pd.read_csv(os.path.join(DATA, "prices.csv"), index_col=0)
    sp_path = os.path.join(DATA, "spreads.csv")
    spreads = pd.read_csv(sp_path, index_col=0) if os.path.exists(sp_path) else pd.DataFrame()
    cds = pd.read_csv(os.path.join(BASE, "cds_log.csv"), dtype=str).fillna("")
    cds["bp"] = pd.to_numeric(cds["bp"], errors="coerce")
    cds = cds.dropna(subset=["bp"])
    # 출처 없는 숫자는 쓰지 않음 (http 링크, 또는 'user:' = 사용자가 직접 준 자료 화면)
    cds = cds[cds["source_url"].str.startswith("http") | cds["source_url"].str.startswith("user:")]
    cds = cds.sort_values("date", kind="stable")
    ins_path = os.path.join(DATA, "insider_trades.csv")
    ins = pd.read_csv(ins_path, dtype={"date": str}).fillna({"text": ""}) if os.path.exists(ins_path) else None

    out = {"generated_at": now_kst(), "tickers": [], "gpu": [], "risk_rules": config.RISK,
           "cds_recovery": config.CDS_RECOVERY, "hidden": getattr(config, "HIDDEN_SECTIONS", [])}
    for t in config.TICKERS:
        tk = t["ticker"]
        p = prices[tk].dropna() if tk in prices else pd.Series(dtype=float)
        sp = spreads[t["bucket"]].dropna() if t["bucket"] in spreads else pd.Series(dtype=float)
        c = cds[cds["ticker"] == tk]
        cl = c.drop_duplicates("date", keep="last")
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
            "insider": build_insider(tk, ins) if "." not in tk else {"rows": -1},
            "series": {
                "price": {"x": list(p.index), "y": [round(v, 2) for v in p.values]},
                "spread": {"x": list(sp.index), "y": [round(v, 1) for v in sp.values]},
                # 차트 선은 날짜당 1개(같은 날 여러 기사면 마지막=종가 기준). 출처 목록은 전부 보여줌
                "cds": {"x": list(cl["date"]), "y": [float(v) for v in cl["bp"]], "url": list(cl["source_url"])},
                "cds_sources": {"x": list(c["date"]), "y": [float(v) for v in c["bp"]],
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

    out["cds_board"] = build_cds_board(cds)
    out["credit_growth"] = build_credit_growth()
    out["liquidity"] = build_liquidity()
    out["nps_stakes"] = build_nps_stakes()
    out["bubble"] = build_bubble()
    out["dram"] = build_dram()
    out["kofia"] = build_kofia()
    out["nps"] = build_nps()
    out["fg"] = build_fg()
    out["silicon"] = build_silicon()
    out["nebius"] = build_nebius()
    out["adr"] = build_adr()
    out["opinions"] = build_opinions()
    out["arr"] = build_arr()
    out["vix"] = build_vix()
    out["vkospi"] = build_vkospi()
    out["mdd"] = build_mdd()
    out["tv"] = build_tv()
    out["wti"] = build_wti()
    out["usmkt"] = build_usmkt()
    out["us_today"] = build_us_today()
    out["policy"] = build_policy()
    out["hynix_inv"] = build_hynix_inv()
    out["supply"] = build_equity_supply()
    out["kr_thermo"] = build_kr_thermo()
    tp = os.path.join(DATA, "kr_today.json")
    out["kr_today"] = json.load(open(tp, encoding="utf-8")) if os.path.exists(tp) else None
    out["kr_investors"] = build_kr_investors()
    out["fedwatch"] = build_fedwatch()
    out["move"] = build_move()
    out["hy"] = build_hy()
    out["readers"] = build_readers()
    out["phase"] = build_phase()

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
             ("GPU 임대가격(Price of Compute)", fetch_gpu), ("GPU·LLM 토큰 지수(Silicon Data)", fetch_silicon),
             ("GPU 현물가격(네비우스)", fetch_nebius), ("D램 현물가격(TrendForce)", fetch_dram),
             ("미수금·반대매매(금융투자협회)", fetch_kofia), ("ADR 등락비율(adrinfo)", fetch_adr), ("공포·탐욕 지수(CNN)", fetch_fear_greed),
             ("내부자 매수·매도(Yahoo/SEC)", fetch_insiders), ("VIX 지수(Yahoo)", fetch_vix), ("VKOSPI(인베스팅닷컴)", fetch_vkospi), ("코스피 1990년~(네이버 금융)", fetch_kospi_long), ("거래대금(다음 금융)", fetch_trading_value),("MOVE 지수(Yahoo)", fetch_move), ("WTI 유가(Yahoo)", fetch_wti), ("미국채 10년물·달러인덱스·원달러 환율(Yahoo)", fetch_usmkt), ("오늘의 미국 증시: 지수·원유·금·비트코인(Yahoo)", fetch_us_today), ("미국 전체 종목 상승/하락·신고가/신저가·이평선(Finviz)", fetch_us_breadth), ("미국 국채 만기별 금리(미 재무부)", fetch_us_yields), ("미국 ETF: QQQ·SOXX·AI 인프라 등(Yahoo)", fetch_us_etfs),("연방기금 선물(Yahoo, 기준금리 시장 예상)", fetch_fed_futures), ("한국 기준금리(한국은행)", fetch_kr_base), ("SK하이닉스 재고자산(OpenDART)", fetch_hynix_inventory), ("주식 공급: 미국 순발행(FRED)·IPO(나스닥)", fetch_equity_supply),("시장 온도계 폭(네이버 전종목 일별 시세)", fetch_kr_breadth), ("시장 온도계 수급(네이버 증권)", fetch_kr_flows), ("오늘의 지수·상승/하락 종목 수(네이버 증권)", fetch_kr_today), ("투자자별 매매동향(네이버 증권)", fetch_kr_investors),("코스피 선행 BPS(와이즈리포트, 상위 약 350개)", fetch_kospi_bps), ("시장 탭(밸류 신호등)", fetch_valmarket),("하이일드 스프레드(FRED)", fetch_hy), ("미국 M2(FRED)", fetch_liquidity), ("버블 비교: 나스닥·반도체(Yahoo)·1920년대 다우(FRED)", fetch_bubble), ("미국 MMF 주간(ICI)", fetch_ici_mmf), ("국민연금 자산배분(기금운용본부)", fetch_nps), ("국민연금 지분율 변동(OpenDART)", fetch_nps_stakes),
             ("투자자 심리 설문(AAII)", fetch_aaii), ("풋/콜 비율(CBOE)", fetch_putcall), ("신용잔고(FINRA)", fetch_margin),
             ("S&P500 이평선 위 종목 비율(Yahoo)", fetch_breadth), ("S&P500 예상 P/E(FactSet)", fetch_pe), ("금리(FRED)", fetch_rates),
             ("화면 데이터 만들기", build)]
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
