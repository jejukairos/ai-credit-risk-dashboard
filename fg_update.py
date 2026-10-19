# -*- coding: utf-8 -*-
"""CNN 공포·탐욕 지수만 빠르게 갱신 (30분마다 실행).
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


def main():
    update.load_env()
    before = _load(NOW)
    name = "공포·탐욕 지수(CNN)"
    status = _load(STATUS) or {"sources": {}}
    try:
        update.fetch_fear_greed()
    except Exception as e:  # noqa: BLE001
        print("실패:", e)
        status["sources"][name] = {"ok": False, "last_ok": status["sources"].get(name, {}).get("last_ok"),
                                   "error": f"{type(e).__name__}: {e}"[:300]}
        with open(STATUS, "w", encoding="utf-8") as f:
            json.dump(status, f, ensure_ascii=False, indent=1)
        return 1
    now = _load(NOW)
    changed = before is None or round(before["score"]) != round(now["score"]) or before["rating"] != now["rating"]
    print(f"CNN {now['score']} {now['rating']} ({now['timestamp']}) - {'변경' if changed else '변화 없음'}")
    if not changed:
        return 0
    data = _load(DOCS_DATA)
    if data is None:
        return 1
    data["fg"] = update.build_fg()
    with open(DOCS_DATA, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, separators=(",", ":"))
    status["sources"][name] = {"ok": True, "last_ok": update.now_kst()}
    with open(STATUS, "w", encoding="utf-8") as f:
        json.dump(status, f, ensure_ascii=False, indent=1)
    return 2


if __name__ == "__main__":
    sys.exit(main())
