# -*- coding: utf-8 -*-
"""CNN 공포·탐욕 지수만 빠르게 갱신 (30분마다 실행). + VKOSPI: 거래소 Open API가 전 거래일 값을 아침 8시쯤 올리면 바로 받아 넣음.
CNN 화면의 숫자(정수)나 단계(공포/중립 등)가 바뀌었을 때만 docs/data.json 을 고치고
종료코드 2를 돌려줘서 fg_update.bat 이 GitHub에 올리게 함. 안 바뀌었으면 0."""
import json
import os
import sys

import update

NOW = os.path.join(update.DATA, "fear_greed_now.json")
DOCS_DATA = os.path.join(update.DOCS, "data.json")
STATUS = os.path.join(update.DOCS, "status.json")


def _load(path):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except Exception:  # noqa: BLE001
        return None


def vkospi_catchup(data, status):
    # 아침 정기 갱신(7:30)은 거래소가 전날 VKOSPI를 올리기(8시쯤) 전이라, 8시 뒤 30분마다 전 거래일 값이 있는지 확인
    path = os.path.join(update.DATA, "vkospi.csv")
    last = lambda: open(path, encoding="utf-8").read().split()[-1].split(",")[0]
    now = update.datetime.now(update.KST)
    prev_bd = (update.pd.Timestamp(now.date()) - update.pd.offsets.BDay(1)).strftime("%Y-%m-%d")
    before = last()
    if now.hour < 8 or before >= prev_bd:
        return False
    update.fetch_vkospi_krx()
    if last() == before:
        return False
    print("VKOSPI", before, "->", last())
    data["vkospi"] = update.build_vkospi()
    status["sources"]["VKOSPI(한국거래소 Open API)"] = {"ok": True, "last_ok": update.now_kst()}
    return True


def main():
    update.load_env()
    data = _load(DOCS_DATA)
    if data is None:
        return 1
    status = _load(STATUS) or {"sources": {}}
    vk = False
    try:
        vk = vkospi_catchup(data, status)
    except Exception as e:  # noqa: BLE001
        print("VKOSPI 확인 실패:", e)
    before = _load(NOW)
    name = "공포·탐욕 지수(CNN)"
    changed = False
    try:
        update.fetch_fear_greed()
        now = _load(NOW)
        changed = before is None or round(before["score"]) != round(now["score"]) or before["rating"] != now["rating"]
        print(f"CNN {now['score']} {now['rating']} ({now['timestamp']}) - {'변경' if changed else '변화 없음'}")
    except Exception as e:  # noqa: BLE001
        print("실패:", e)
        status["sources"][name] = {"ok": False, "last_ok": status["sources"].get(name, {}).get("last_ok"),
                                   "error": f"{type(e).__name__}: {e}"[:300]}
        if not vk:
            with open(STATUS, "w", encoding="utf-8") as f:
                json.dump(status, f, ensure_ascii=False, indent=1)
            return 1
    if not (changed or vk):
        return 0
    if changed:
        data["fg"] = update.build_fg()
        status["sources"][name] = {"ok": True, "last_ok": update.now_kst()}
    with open(DOCS_DATA, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, separators=(",", ":"))
    with open(STATUS, "w", encoding="utf-8") as f:
        json.dump(status, f, ensure_ascii=False, indent=1)
    return 2


if __name__ == "__main__":
    sys.exit(main())
