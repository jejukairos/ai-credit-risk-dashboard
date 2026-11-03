# 미국 장 마감(뉴욕 16:00) 20분 뒤부터, 마감마다 한 번만 통과(exit 0) — 아니면 건너뜀(exit 1). us_close_update.bat이 부름
# 작업 스케줄러가 한국 금·토 05:30(서머타임이면 마감 30분 뒤)·06:30(아니면 30분 뒤) 둘 다 부르므로 서머타임을 따로 계산하지 않아도 됨. 일~목은 07:30 한 번(사용자가 7:15~7:20에 컴퓨터를 켬)
import os
import sys
from datetime import datetime
from zoneinfo import ZoneInfo

ny = datetime.now(ZoneInfo("America/New_York"))
if ny.weekday() >= 5 or ny.strftime("%H%M") < "1620":  # 주말(뉴욕) · 아직 마감 전
    sys.exit(1)
mark = os.path.join(os.path.dirname(os.path.abspath(__file__)), "logs", "us_close_done.txt")
day = ny.strftime("%Y-%m-%d")
if os.path.exists(mark) and open(mark, encoding="utf-8").read().strip() == day:  # 이번 마감은 이미 갱신함
    sys.exit(1)
os.makedirs(os.path.dirname(mark), exist_ok=True)
with open(mark, "w", encoding="utf-8") as f:
    f.write(day)
