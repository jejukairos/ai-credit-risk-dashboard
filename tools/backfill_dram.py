# -*- coding: utf-8 -*-
"""TrendForce 주간 'Memory Spot Price Update' 기사에서 D램 주력 칩 현물 평균가를 뽑아
data/dram_weekly.csv 에 저장합니다 (날짜, 칩, 가격, 출처URL). 처음 한 번 + 매일 갱신 때 새 기사만 추가."""
import os
import re
import sys
import time
from datetime import date, timedelta

import pandas as pd
import requests

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT = os.path.join(BASE, "data", "dram_weekly.csv")
UA = {"User-Agent": "Mozilla/5.0 (ai-credit-risk-dashboard)"}
SEARCH = "https://www.trendforce.com/news/{page}?s=memory+spot+price+update"
MONTHS = {m: i for i, m in enumerate(
    ["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"], 1)}

# 예: "(i.e., DDR4 1Gx8 3200MT/s) has increased by 1.78% from US$44.57 last week (Sept. 2) to US$45.36 this week (Sept. 8)"
PAT = re.compile(
    r"\(i\.e\.,?\s*(?P<chip>DDR\d[^)]*?)\).{0,120}?from US\$\s?(?P<a>\d+(?:\.\d+)?)\s*(?:last week\s*)?"
    r"(?:\((?P<da>[A-Za-z]+)\.?\s*(?P<dda>\d+)(?:th|st|nd|rd)?\))?"
    r".{0,40}?to US\$\s?(?P<b>\d+(?:\.\d+)?)\s*(?:this week\s*)?"
    r"(?:\((?P<db>[A-Za-z]+)\.?\s*(?P<ddb>\d+)(?:th|st|nd|rd)?\))?", re.S)


def text(html):
    t = re.sub(r"<script[\s\S]*?</script>|<style[\s\S]*?</style>", " ", html)
    t = re.sub(r"<[^>]+>", " ", t)
    t = t.replace("&nbsp;", " ").replace("&#8217;", "'").replace("&#8211;", "-")
    return re.sub(r"\s+", " ", t)


def to_date(mon, day, pub):
    m = MONTHS.get(mon[:3].lower())
    if m is None:
        return pub.isoformat()
    y = pub.year if m <= pub.month else pub.year - 1  # 1월 기사 속 '12월' 처리
    return date(y, m, int(day)).isoformat()


def list_articles(max_pages=80, stop_at=None):
    urls = []
    for p in range(1, max_pages + 1):
        page = "" if p == 1 else f"page/{p}/"
        r = requests.get(SEARCH.format(page=page), headers=UA, timeout=60)
        if r.status_code != 200:
            break
        found = re.findall(r'href="(https://www\.trendforce\.com/news/(\d{4})/(\d\d)/(\d\d)/[^"]*spot-price[^"]*)"', r.text)
        new = [f for f in dict.fromkeys(found) if f[0] not in [u[0] for u in urls]]
        if not new:
            break
        urls.extend(new)
        if stop_at and any(u[0] in stop_at for u in new):
            break
        time.sleep(1)
    return urls


def main(full=False):
    old = pd.read_csv(OUT) if os.path.exists(OUT) else pd.DataFrame(columns=["date", "chip", "usd", "source_url"])
    seen = set(old["source_url"])
    arts = list_articles(stop_at=None if full or not len(old) else seen)
    rows = []
    for url, y, m, d in arts:
        if url in seen:
            continue
        try:
            r = requests.get(url, headers=UA, timeout=60)
            t = text(r.text)
        except Exception as e:  # noqa: BLE001
            print("skip", url, e)
            continue
        pub = date(int(y), int(m), int(d))
        for mt in PAT.finditer(t):
            chip = re.sub(r"\s+", " ", mt["chip"]).replace(" MT/s", "MT/s").strip()
            # 기사에 날짜가 없으면(예전 형식) 이번 주 = 기사 발행일, 지난주 = 7일 전
            db = to_date(mt["db"], mt["ddb"], pub) if mt["db"] else pub.isoformat()
            da = to_date(mt["da"], mt["dda"], pub) if mt["da"] else (pub - timedelta(days=7)).isoformat()
            rows.append({"date": da, "chip": chip, "usd": float(mt["a"]), "source_url": url})
            rows.append({"date": db, "chip": chip, "usd": float(mt["b"]), "source_url": url})
            break
        else:
            print("no match", url)
        time.sleep(1)
    allr = pd.concat([old, pd.DataFrame(rows)]) if rows else old
    # 같은 날짜·칩은 최신 기사(this week 값) 우선
    allr = allr.drop_duplicates(["date", "chip"], keep="last").sort_values(["chip", "date"])
    allr.to_csv(OUT, index=False)
    print(f"articles={len(arts)} new_rows={len(rows)} total={len(allr)}")


if __name__ == "__main__":
    main(full="--full" in sys.argv)
