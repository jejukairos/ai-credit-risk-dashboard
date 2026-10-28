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
    # 코스피 1990년~ 일별 종가 (네이버 금융 차트 데이터, 야후는 1996년 12월부터라 MDD 계산엔 부족)
    r = requests.get("https://api.finance.naver.com/siseJson.naver", headers=BROWSER_UA, timeout=60,
                     params={"symbol": "KOSPI", "requestType": 1, "startTime": "19900101",
                             "endTime": datetime.now().strftime("%Y%m%d"), "timeframe": "day"})
    # 행 모양: ["19900104", 시가, 고가, 저가, 종가, ...]
    rows = re.findall(r'\["(\d{8})",\s*[\d.]+,\s*[\d.]+,\s*[\d.]+,\s*([\d.]+)', r.text)
    if len(rows) < 8000 or rows[0][0] > "19900110":
        raise RuntimeError(f"네이버 코스피 장기 데이터가 이상함 ({len(rows)}행)")
    pd.DataFrame([{"date": f"{d[:4]}-{d[4:6]}-{d[6:]}", "kospi": float(v)} for d, v in rows]) \
        .to_csv(os.path.join(DATA, "kospi_long.csv"), index=False)


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


RATE_SERIES = {"us10y": "DGS10", "fedfunds": "DFF", "kr10y": "IRLTLT01KRM156N"}  # 미국 10년물·기준금리(일별), 한국 10년물(월별)


def fetch_rates():
    # 시장 국면 판단용 금리 (FRED, 단위 %)
    for name, sid in RATE_SERIES.items():
        (fred_series(sid, start="2020-01-01") / 100).rename(name).rename_axis("date").round(3).to_csv(os.path.join(DATA, f"rate_{name}.csv"))


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
    for fname, col in [("sp500.csv", "sp500"), ("us10y.csv", "us10y"), ("dxy.csv", "dxy"), ("usdkrw.csv", "usdkrw")]:
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
        if col in ("sp500", "usdkrw"):
            o["peak"] = {"date": s.idxmax(), "value": round(float(s.max()), 2)}
            o["mdd"] = round((last / float(s.max()) - 1) * 100, 2)
        out[col] = o
    return out or None


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
             ("내부자 매수·매도(Yahoo/SEC)", fetch_insiders), ("VIX 지수(Yahoo)", fetch_vix), ("VKOSPI(인베스팅닷컴)", fetch_vkospi), ("코스피 1990년~(네이버 금융)", fetch_kospi_long), ("거래대금(다음 금융)", fetch_trading_value),("MOVE 지수(Yahoo)", fetch_move), ("WTI 유가(Yahoo)", fetch_wti), ("미국채 10년물·달러인덱스·원달러 환율(Yahoo)", fetch_usmkt), ("코스피 선행 BPS(와이즈리포트, 상위 약 350개)", fetch_kospi_bps), ("시장 탭(밸류 신호등)", fetch_valmarket),("하이일드 스프레드(FRED)", fetch_hy), ("국민연금 자산배분(기금운용본부)", fetch_nps),
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
