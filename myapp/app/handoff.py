# app/handoff.py
# 알람 화면에서 작업/장애 화면으로 넘어갈 때 들고 갈 값.
#
# ── 왜 따로 두나 ────────────────────────────────────────────────────
# 넘길 값을 알람 템플릿에서 조립하면, 받는 쪽(work/incident)이 무엇을
# 기대하는지 HTML 안에 숨는다. 그러면 폼에 칸을 하나 더할 때 링크 쪽을
# 같이 고쳐야 한다는 걸 아무도 모른다. 여기 한곳에서 정한다.
#
# ── 왜 새 표를 안 만드나 ────────────────────────────────────────────
# 넘기는 값은 전부 이미 이벤트에 있다. 옮겨 적는 수고를 없애는 것이
# 목적이지, 새 사실을 만드는 것이 아니다. 장애 쪽에서 지문 하나만
# 실제로 저장한다(그건 '사람이 지목했다' 는 새 사실이다).

# 폼에 채워 넣을 수 있는 값들. 받는 화면이 이 이름으로 읽는다.
# 링크(쿼리스트링)와 폼 필드 이름을 같게 두면 중간에 이름을 번역하는
# 자리가 없어진다.
WORK_KEYS = ("title", "account_id", "region", "request", "fingerprint", "event_id")
INCIDENT_KEYS = ("title", "account_id", "region", "severity", "started_at",
                 "sources", "fingerprint", "event_id")

# 제목은 알람 메시지를 그대로 쓰되 길면 자른다. 제목 칸에 로그 한 줄이
# 통째로 들어가면 목록에서 아무것도 못 읽는다.
TITLE_MAX = 80


def _title(record):
    message = (record.get("message") or "").strip().splitlines()
    text = message[0] if message else "(내용 없음)"
    return text[:TITLE_MAX] + ("…" if len(text) > TITLE_MAX else "")


def _when(record):
    """datetime-local 입력이 읽는 형식. 초는 버린다.

    이벤트 시각은 UTC 로 저장되어 있고 폼도 UTC 로 받는다(화면에 그렇게
    적혀 있다). 여기서 시간대를 바꾸면 그 약속이 깨진다.
    """
    at = record.get("occurred_at")
    return at.strftime("%Y-%m-%dT%H:%M") if at else ""


def for_work(record):
    """이 알람으로 작업을 만들 때 폼에 채울 값."""
    return {
        "title": _title(record),
        "account_id": record.get("account_id") or "",
        "region": "",          # 이벤트에는 리전이 없다. 계정에서 고른다.
        # 요청 원문 자리에 알람을 그대로 넣는다. 고객 요청이 아니라
        # 알람에서 시작된 작업이라는 것이 증적에 남아야 한다.
        "request": (
            f"[알람에서 시작된 작업]\n"
            f"발생 {record.get('occurred_at')}\n"
            f"심각도 {record.get('severity')}\n"
            f"출처 {record.get('source')}\n"
            f"내용 {record.get('message')}"
        ),
        "fingerprint": record.get("fingerprint") or "",
        "event_id": record.get("event_id") or "",
    }


def for_incident(record):
    """이 알람으로 사후 보고서를 만들 때 폼에 채울 값."""
    return {
        "title": _title(record),
        "account_id": record.get("account_id") or "",
        "region": "",
        "severity": record.get("severity") or "error",
        # 장애 시작은 알람 발생 시각으로 둔다. 실제 시작은 대개 그보다
        # 앞이라 사람이 고치게 되지만, 빈 칸에서 시작하는 것보다 낫다.
        "started_at": _when(record),
        # 출처를 채워두면 타임라인이 그 서비스로 좁혀진다. 비워두면
        # 그 시간대에 우연히 같이 난 무관한 알람까지 들어온다.
        "sources": record.get("source") or "",
        "fingerprint": record.get("fingerprint") or "",
        "event_id": record.get("event_id") or "",
    }


def take(args, keys):
    """쿼리스트링에서 폼 기본값만 골라낸다.

    받는 쪽이 request.args 를 통째로 템플릿에 넘기면, 주소창에 아무 값이나
    넣어도 폼에 들어간다. 아는 이름만 통과시킨다.
    """
    return {k: (args.get(k) or "") for k in keys}
