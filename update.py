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
    "H100(SDH100RT)": "H100", "H200": "H200", "B200(SDB200RT)": "B200", "B300": "B300",
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


def change_4w(s):
    """각 시점의 4주(28일) 전 대비 변화율(%) — 기울기가 완만해지는지 보는 용도"""
    s = s.dropna()
    idx = pd.to_datetime(s.index)
    out = []
    for i, d in enumerate(idx):
        past = s[idx <= d - pd.Timedelta(days=28)]
        # 4주 전 근처(±10일) 값이 없으면 계산하지 않음 (데이터 공백 구간)
        if past.empty or (d - pd.Timestamp(past.index[-1])).days > 38:
            out.append(None)
        else:
            out.append(round((s.iloc[i] / past.iloc[-1] - 1) * 100, 2))
    return out


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
        entry = {"item": item, "label": label, "source": src, **ser,
                 "last": ser["y"][-1], "last_date": ser["x"][-1],
                 "m1": pct_change(s, 30), "m3": pct_change(s, 91)}
        c = dl[dl["item"] == item + " 계약"].sort_values("date")
        if len(c):  # 같은 칩의 계약(고정거래)가격을 함께 표시
            entry["contract"] = {"x": list(c["date"]), "y": [float(v) for v in c["usd"]]}
        out.append(entry)
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

    def peak(col):  # 최근 1년 최고점과 지금의 차이
        row = one_y.loc[one_y[col].idxmax()]
        now = k.dropna(subset=[col]).iloc[-1]
        return {"date": row["date"], "value": round(row[col] / 1e6, 1),
                "chg": round((now[col] / row[col] - 1) * 100, 1)}
    kp_path = os.path.join(DATA, "kospi.csv")
    kospi = {"x": [], "y": []}
    if os.path.exists(kp_path):
        kp = pd.read_csv(kp_path, dtype={"date": str})
        kospi = {"x": list(kp["date"]), "y": [round(float(v), 2) for v in kp["kospi"]]}
    return {
        "kospi": kospi,
        "x": list(k["date"]),
        "misu": num("misu", 100, 0),          # 억원
        "forced": num("forced", 100, 0),      # 억원
        "ratio": [None if pd.isna(v) else float(v) for v in k["ratio"]],
        "credit": num("credit", 1e6, 2),      # 조원
        "deposit": num("deposit", 1e6, 2),    # 조원
        "last": {"date": last["date"], "misu": round(last["misu"] / 100), "forced": round(last["forced"] / 100),
                 "ratio": float(last["ratio"]), "credit": round(last["credit"] / 1e6, 2),
                 "deposit": round(last["deposit"] / 1e6, 2), "forced_avg_1y": round(forced_avg_1y / 100),
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
           "cds_recovery": config.CDS_RECOVERY}
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

    out["dram"] = build_dram()
    out["kofia"] = build_kofia()
    out["nps"] = build_nps()
    out["fg"] = build_fg()
    out["silicon"] = build_silicon()
    out["nebius"] = build_nebius()
    out["adr"] = build_adr()
    out["arr"] = build_arr()
    out["vix"] = build_vix()
    out["hy"] = build_hy()

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
             ("내부자 매수·매도(Yahoo/SEC)", fetch_insiders), ("VIX 지수(Yahoo)", fetch_vix), ("하이일드 스프레드(FRED)", fetch_hy), ("국민연금 자산배분(기금운용본부)", fetch_nps),
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
