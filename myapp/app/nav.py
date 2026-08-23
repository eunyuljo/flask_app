# app/nav.py
# 왼쪽 메뉴를 데이터로 둔다.
#
# ── 왜 템플릿에서 꺼냈나 ────────────────────────────────────────────
# 예전에는 base.html 에 <a> 태그 열여섯 개가 그대로 박혀 있었다.
# 기능을 하나 붙일 때마다 HTML 다섯 줄을 옳은 자리에 끼워 넣어야 했고,
# 권한 검사(관리자만 보이기)도 태그마다 따로 감쌌다.
#
# 여기에 한 줄 추가하면 메뉴가 생긴다. 순서도 이 파일에서만 바뀐다.
#
# ── 왜 블루프린트마다 선언하지 않고 여기 모았나 ──────────────────────
# 메뉴 순서는 앱 전체의 문제다. 각 블루프린트가 자기 메뉴를 선언하면
# "무엇이 먼저 오는가" 를 아무 데서도 볼 수 없게 된다. 등록은 흩어져
# 있어도(블루프린트) 순서는 한곳에 있는 편이 낫다.
#
# ── 카테고리를 나눈 기준 ────────────────────────────────────────────
# 예전에는 '관측' 하나에 열세 개가 들어 있었다. 그건 카테고리가 아니라
# '나머지 전부' 다. 지금은 MSP 업무의 흐름을 따라 나눈다.
#
#   고객사 ── 누구의 것인가 (고객사 축 화면)
#   알람   ── 무슨 일이 일어났나 (들어오는 것)
#   인프라 ── 지금 어떤 상태인가 (쌓아둔 것)
#   조사   ── 무엇으로 알아보나 (직접 캐묻는 도구)
#   대응   ── 지금 무엇을 하나 (사람이 하는 일)
#   보고   ── 밖으로 무엇을 내보내나 (산출물)
#   관리   ── 도구 자체
#
# 새 기능이 어디 들어갈지 대개 바로 정해진다.
#   변경 승인   -> 대응     만료 추적   -> 알람
#   비용        -> 인프라   용량 예측   -> 알람
#
# ── 조사와 대응을 왜 갈랐나 ─────────────────────────────────────────
# 대응에 여섯 개가 모였을 때 두 종류가 섞여 있었다. 당직 인계·런북·작업
# 기록은 '사람이 하는 운영 업무' 인데, AI 에이전트와 콘솔은 그게 아니라
# '무엇으로 알아보나' 다. 알람 밑에 있던 탐색도 같은 성격이었다.
# 준비된 화면을 보는 것과 직접 묻는 것은 다르고, MSP 업무도 실제로
# 알람 -> 조사 -> 대응 순으로 흐른다.

from app import users

CATEGORIES = [
    {"id": "customer",  "label": "고객사", "hint": "누구의 것인가"},
    {"id": "alarm",     "label": "알람",   "hint": "무슨 일이 일어났나"},
    {"id": "infra",     "label": "인프라", "hint": "지금 어떤 상태인가"},
    {"id": "inspect",   "label": "조사",   "hint": "무엇으로 알아보나"},
    {"id": "respond",   "label": "대응",   "hint": "지금 무엇을 하나"},
    {"id": "report",    "label": "보고",   "hint": "밖으로 무엇을 내보내나"},
    {"id": "manage",    "label": "관리",   "hint": "도구 자체"},
]

CATEGORY_IDS = [c["id"] for c in CATEGORIES]

# endpoint : 메뉴가 가리키는 곳. url_for 에 그대로 넘긴다.
# match    : 이 접두사로 시작하는 endpoint 를 보고 있으면 이 메뉴가 켜진다.
#            비우면 endpoint 의 블루프린트 이름을 쓴다.
#
#            접두사가 겹칠 수 있다. /report/msr 은 'report.' 와
#            'report.msr' 에 둘 다 걸린다. 이럴 때는 긴 쪽이 이긴다 -
#            그래야 한 블루프린트 안에 있는 화면을 따로 떼어 놓을 수 있다.
# role     : 이 역할 이상만 메뉴가 보인다. 비우면 로그인만 하면 보인다.
#            메뉴를 숨기는 것은 보안이 아니다. 실제 차단은 각 블루프린트의
#            before_request 가 한다. 여기서는 보이지 않게만 할 뿐이다.
ITEMS = [
    # ---- 고객사 ----
    {"endpoint": "customer.index", "label": "고객사 현황", "icon": "◉",
     "category": "customer", "match": ("customer.",),
     "hint": "고객사 하나의 상태를 한 화면에"},
    {"endpoint": "customer.readiness_page", "label": "온보딩 준비도", "icon": "◎",
     "category": "customer", "match": ("customer.readiness",),
     "hint": "이 고객사를 받을 준비가 됐는가"},
    {"endpoint": "customer.access_page", "label": "계정 접속", "icon": "⚿",
     "category": "customer", "match": ("customer.access",),
     "hint": "고객사 계정에 들어갈 수 있는가"},
    {"endpoint": "customer.settings_page", "label": "설정 현황", "icon": "⊞",
     "category": "customer", "match": ("customer.settings",),
     "hint": "고객사별로 무엇이 비어 있나"},
    {"endpoint": "customer.routines_page", "label": "정기 점검", "icon": "↻",
     "category": "customer", "match": ("customer.routines",),
     "hint": "약속한 주기 업무를 지키고 있는가"},

    # ---- 알람 ----
    {"endpoint": "dashboard.index", "label": "대시보드", "icon": "▤",
     "category": "alarm", "hint": "기간별 추이와 심각도 분포"},
    {"endpoint": "alarm.index", "label": "이벤트", "icon": "◈",
     "category": "alarm", "hint": "들어온 알람 목록과 제출"},
    {"endpoint": "noise.index", "label": "알람 노이즈", "icon": "≋",
     "category": "alarm", "hint": "시끄러운 알람 순위와 억제 규칙"},

    # ---- 인프라 ----
    {"endpoint": "resources.inventory_page", "label": "리소스 목록", "icon": "▣",
     "category": "infra", "match": ("resources.inventory",),
     "hint": "지금 무엇이 떠 있는가"},
    {"endpoint": "resources.index", "label": "리소스 변경", "icon": "▩",
     "category": "infra", "match": ("resources.",),
     "hint": "두 시점의 인프라 차이"},
    {"endpoint": "compliance.index", "label": "컴플라이언스", "icon": "✓",
     "category": "infra", "hint": "스냅샷 기준 모범사례 점검"},

    # ---- 대응 ----
    {"endpoint": "handover.index", "label": "당직 인계", "icon": "☾",
     "category": "respond", "hint": "지난 근무 구간 요약"},
    {"endpoint": "runbook.index", "label": "런북", "icon": "▦",
     "category": "respond", "match": ("runbook.",),
     "hint": "알람 종류별 대응 절차"},
    {"endpoint": "runbook.runs", "label": "런북 실행 기록", "icon": "▨",
     "category": "respond", "match": ("runbook.runs",),
     "hint": "그 절차가 실제로 통했는가"},
    {"endpoint": "work.index", "label": "작업 기록", "icon": "✎",
     "category": "respond", "hint": "작업 전/후 증적"},

    # ---- 조사 ----
    {"endpoint": "explore.index", "label": "탐색", "icon": "⌕",
     "category": "inspect", "hint": "질의어로 이벤트 찾기"},
    {"endpoint": "agent.index", "label": "AI 에이전트", "icon": "✦",
     "category": "inspect", "hint": "모델에게 물어보기"},
    {"endpoint": "console.index", "label": "콘솔", "icon": "❯",
     "category": "inspect", "role": "admin",
     "hint": "고객사 계정에 읽기 전용 AWS CLI"},

    # ---- 보고 ----
    {"endpoint": "incident.index", "label": "사후 보고서", "icon": "⚑",
     "category": "report", "hint": "장애 타임라인과 RCA"},
    {"endpoint": "report.index", "label": "리포트", "icon": "▥",
     "category": "report", "match": ("report.",),
     "hint": "기간별 운영 리포트"},
    {"endpoint": "report.msr_page", "label": "월간 리뷰", "icon": "▧",
     "category": "report", "match": ("report.msr",),
     "hint": "고객사 하나의 한 달치"},
    # SLA 는 리포트 블루프린트 안에 있지만 하는 일이 다르다. 다른 화면들이
    # 지나간 기간을 정리해 내보내는 것이라면, 여기는 목표 자체를 정하는
    # 곳이다. 메뉴에 없던 동안에는 온보딩 준비도에서 점검이 실패했을 때
    # 뜨는 링크가 유일한 입구였다 - 그 화면을 안 거친 사람은 존재를 알
    # 방법이 없었다.
    {"endpoint": "report.sla", "label": "SLA", "icon": "◔",
     "category": "report", "match": ("report.sla",),
     "hint": "고객사별 목표 설정과 달성률"},

    # ---- 관리 ----
    {"endpoint": "admin.index", "label": "관리자", "icon": "⚙",
     "category": "manage", "match": ("admin.",), "role": "admin",
     "hint": "설정·계정·감사 로그"},
    # 배치가 죽었는지 보는 화면이 관리자 뒤에 있으면, 정작 조용히 죽었을
    # 때 아무도 안 본다. 무서운 건 실패가 아니라 침묵이다.
    {"endpoint": "admin.health_page", "label": "운영 상태", "icon": "◍",
     "category": "manage", "match": ("admin.health",), "role": "admin",
     "hint": "배치 실행과 연동이 살아 있는가"},
]


def _match_prefixes(item):
    """이 메뉴가 켜지는 endpoint 접두사들."""
    if item.get("match"):
        return item["match"]
    # 안 적었으면 블루프린트 전체를 맡는다. "alarm.index" -> "alarm."
    return (item["endpoint"].split(".")[0] + ".",)


def active_endpoint(current):
    """지금 보고 있는 화면을 맡은 메뉴의 endpoint. 없으면 None.

    접두사가 겹치면 긴 쪽이 이긴다. 'report.msr_page' 는 'report.' 와
    'report.msr' 에 둘 다 걸리는데, 여기서 짧은 쪽을 고르면 월간 리뷰를
    보는 동안 '리포트' 메뉴에 불이 들어온다.
    """
    current = current or ""
    best, best_len = None, -1
    for item in ITEMS:
        for prefix in _match_prefixes(item):
            if current.startswith(prefix) and len(prefix) > best_len:
                best, best_len = item["endpoint"], len(prefix)
    return best


def menu(role, known_endpoints=None):
    """역할에 맞는 메뉴를 카테고리별로 묶어 돌려준다.

    known_endpoints: 앱에 실제로 등록된 endpoint 집합. 주면 없는 것은
        건너뛴다. 메뉴에 적어둔 화면을 나중에 떼어냈을 때, 사이드바
        하나 때문에 모든 페이지가 500 이 나는 것을 막는다.
    """
    groups = []
    for category in CATEGORIES:
        items = []
        for item in ITEMS:
            if item["category"] != category["id"]:
                continue
            if known_endpoints is not None and item["endpoint"] not in known_endpoints:
                continue
            need = item.get("role")
            if need and not users.can(role, need):
                continue
            items.append(item)
        if items:
            groups.append({**category, "items": items})
    return groups
