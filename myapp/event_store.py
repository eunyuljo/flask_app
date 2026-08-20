# event_store.py
# Lambda 가 정규화해서 돌려준 이벤트를 화면에 보여주기 위해 잠시 담아두는 저장소.
# 진짜 원본은 Lambda 가 DB 에 넣는다. 여기 있는 건 '방금 처리한 것들'을 보여주기 위한 사본이다.

import threading
from collections import deque

# maxlen 을 주면 오래된 항목이 자동으로 밀려난다. 무한정 쌓여서 메모리를 먹는 일이 없다.
_MAX_ITEMS = 100
_events = deque(maxlen=_MAX_ITEMS)

# Flask 개발 서버도 요청을 여러 스레드로 처리한다.
# deque 자체는 append 가 원자적이지만, '읽어서 세는' 작업까지 묶으려면 잠금이 필요하다.
_lock = threading.Lock()


def add(record, delivery):
    """정규화된 레코드 하나를 기록한다.

    record   : Lambda 가 돌려준 표준 이벤트
    delivery : 적재/알람 결과 등 처리 부가정보
    """
    with _lock:
        _events.appendleft({"record": record, "delivery": delivery})


def recent(limit=20):
    """최근 이벤트를 새 것부터 돌려준다."""
    with _lock:
        return list(_events)[:limit]


def stats():
    """관리자 화면에 쓸 간단한 집계."""
    with _lock:
        items = list(_events)

    counts = {"critical": 0, "error": 0, "warning": 0, "info": 0}
    alarmed = 0
    for item in items:
        severity = item["record"].get("severity", "info")
        if severity in counts:
            counts[severity] += 1
        if item["delivery"].get("alarm", {}).get("alarmed"):
            alarmed += 1

    return {
        "total": len(items),
        "by_severity": counts,
        "alarmed": alarmed,
        "capacity": _MAX_ITEMS,
    }


def clear():
    """저장소를 비운다."""
    with _lock:
        _events.clear()
