# app/incident.py
# 장애 기록을 읽고 쓰고, 장애 구간의 타임라인을 모은다.
#
# 이 모듈의 핵심은 assemble() 이다. 지금까지 이 앱의 데이터는 서로를 몰랐다.
#   events            무슨 일이 있었나   (/alarm/, /explore/)
#   resource_snapshots 무엇이 바뀌었나   (/resources/)
#   work_orders       누가 무엇을 했나  (/work/)
# 셋 다 시각이 찍혀 있는데 한 화면에서 겹쳐본 적이 없다.
# 장애를 조사할 때 제일 먼저 묻는 "장애 직전에 뭐가 바뀌었나" 에 답하려면
# 이 셋을 한 시간축에 세워야 한다.

from datetime import datetime, timedelta, timezone

from flask import current_app

# 장애 구간 앞뒤로 더 볼 여유. 원인은 보통 알람보다 먼저 있다.
LEAD_MINUTES = 30
TAIL_MINUTES = 30

# 내부 RCA 상태. 고객 제출본과 시점이 다르므로 라벨도 구분한다.
STATUS_LABEL = {"draft": "작성 중", "published": "내부 확정"}

CUSTOMER_STATUS_LABEL = {"none": "미작성", "draft": "작성 중", "sent": "고객사 제출됨"}

# 사람이 쓰는 칸. 앱은 절대 채우지 않는다.
NARRATIVE_FIELDS = ("impact", "cause", "action", "prevention")

FIELD_LABEL = {
    "impact": "영향",
    "cause": "원인",
    "action": "조치",
    "prevention": "재발 방지",
}

# 고객 제출본의 칸. 내부 칸과 별도로 둔다 - 같은 사실을 쓰더라도
# 표현과 입도가 달라지기 때문이다. 다만 기록과 근거는 하나다.
CUSTOMER_FIELDS = (
    "customer_timeline", "customer_impact", "customer_cause",
    "customer_action", "customer_prevention",
)

CUSTOMER_FIELD_LABEL = {
    "customer_timeline": "경과",
    "customer_impact": "영향",
    "customer_cause": "원인",
    "customer_action": "조치",
    "customer_prevention": "재발 방지",
}


class IncidentError(Exception):
    """장애 기록을 읽거나 쓰는 데 실패했을 때."""


def psycopg_uri():
    return current_app.config["SQLALCHEMY_DATABASE_URI"].replace(
        "postgresql+psycopg://", "postgresql://"
    )


def _rows(cur):
    cols = [d.name for d in cur.description]
    return [dict(zip(cols, r)) for r in cur.fetchall()]


def _connect():
    try:
        import psycopg
    except ImportError as e:
        raise IncidentError("psycopg 가 설치되어 있지 않습니다.") from e
    try:
        return psycopg.connect(psycopg_uri())
    except Exception as e:
        if type(e).__module__.split(".")[0] == "psycopg":
            raise IncidentError(f"DB 에 접속하지 못했습니다: {e}") from e
        raise


def _ensure_table(cur):
    cur.execute("SELECT to_regclass('public.incidents')")
    if cur.fetchone()[0] is None:
        raise IncidentError(
            "incidents 테이블이 없습니다. flask --app run init-db 를 실행하세요."
        )


# ----------------------------------------------------------------------
# 기록 자체
# ----------------------------------------------------------------------

def create(title, started_at, author, ended_at=None, severity="error",
           customer="", account_id="", region="", sources=None):
    """장애 기록을 만든다. 이 시점에는 서술 칸이 전부 비어 있다."""
    if not title.strip():
        raise IncidentError("제목을 입력하세요.")
    if started_at is None:
        raise IncidentError("장애 시작 시각을 입력하세요.")
    if ended_at is not None and ended_at < started_at:
        raise IncidentError("종료 시각이 시작 시각보다 앞설 수 없습니다.")

    with _connect() as conn, conn.cursor() as cur:
        _ensure_table(cur)
        cur.execute(
            """
            INSERT INTO incidents
                (title, started_at, ended_at, severity,
                 customer, account_id, region, author, sources)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
            RETURNING id
            """,
            (title.strip(), started_at, ended_at, severity,
             customer, account_id, region, author, sources or []),
        )
        return cur.fetchone()[0]


def get(incident_id):
    """장애 기록 하나. 없으면 None."""
    with _connect() as conn, conn.cursor() as cur:
        _ensure_table(cur)
        cur.execute("SELECT * FROM incidents WHERE id = %s", (incident_id,))
        rows = _rows(cur)
    return rows[0] if rows else None


def recent(limit=30):
    """장애 목록. 최근 발생 순."""
    with _connect() as conn, conn.cursor() as cur:
        _ensure_table(cur)
        cur.execute(
            "SELECT * FROM incidents ORDER BY started_at DESC LIMIT %s", (limit,)
        )
        return _rows(cur)


def update_narrative(incident_id, fields):
    """사람이 쓴 칸을 저장한다.

    허용된 칸만 갱신한다. 폼에서 넘어온 키를 그대로 SQL 에 이어붙이면
    임의의 열을 고칠 수 있게 된다.
    """
    unknown = set(fields) - set(NARRATIVE_FIELDS)
    if unknown:
        raise IncidentError(f"고칠 수 없는 항목입니다: {', '.join(sorted(unknown))}")
    if not fields:
        return

    with _connect() as conn, conn.cursor() as cur:
        _ensure_table(cur)
        # 제출된 뒤에는 고치지 않는다. 고객사에 낸 문서가 조용히 바뀌면 안 된다.
        assignments = ", ".join(f"{k} = %s" for k in fields)
        cur.execute(
            f"UPDATE incidents SET {assignments}, updated_at = now() "
            f"WHERE id = %s AND status = 'draft' RETURNING id",
            list(fields.values()) + [incident_id],
        )
        if cur.fetchone() is None:
            current = get(incident_id)
            if current is None:
                raise IncidentError("장애 기록을 찾지 못했습니다.")
            raise IncidentError("이미 제출된 보고서는 고칠 수 없습니다.")


def publish(incident_id):
    """보고서를 제출 상태로 바꾼다.

    원인과 조치가 비어 있으면 막는다. 그 두 칸이 사후 보고서의 본체이고,
    비어 있는 채로 나가면 타임라인만 붙인 문서가 된다.
    """
    item = get(incident_id)
    if item is None:
        raise IncidentError("장애 기록을 찾지 못했습니다.")

    missing = [FIELD_LABEL[f] for f in ("cause", "action") if not item[f].strip()]
    if missing:
        raise IncidentError(
            f"{' / '.join(missing)} 칸이 비어 있습니다. "
            "이 칸들은 앱이 채울 수 없으므로 직접 작성해야 제출할 수 있습니다."
        )

    with _connect() as conn, conn.cursor() as cur:
        cur.execute(
            "UPDATE incidents SET status = 'published', published_at = now() "
            "WHERE id = %s AND status = 'draft' RETURNING id",
            (incident_id,),
        )
        if cur.fetchone() is None:
            raise IncidentError("이미 제출된 보고서입니다.")


# ----------------------------------------------------------------------
# 타임라인 조립
# ----------------------------------------------------------------------

def _window(item):
    """조사 구간. 장애 구간 앞뒤로 여유를 둔다."""
    start = item["started_at"] - timedelta(minutes=LEAD_MINUTES)
    end = (item["ended_at"] or datetime.now(timezone.utc)) + timedelta(minutes=TAIL_MINUTES)
    return start, end


def assemble(item):
    """장애 구간의 알람 / 리소스 변경 / 작업을 한 시간축에 세운다."""
    start, end = _window(item)

    # 범위를 두 겹으로 좁힌다.
    #   1) 계정 - 이 장애가 난 계정의 이벤트만 (events.account_id)
    #   2) 출처 - 그 안에서도 관련 서비스만 (사람이 지정)
    # 계정 조건이 생기기 전에는 2)만으로 좁혀야 했다. 이제 계정을 지정한
    # 장애는 자동으로 다른 계정의 알람이 빠진다.
    #
    # 계정을 지정했는데 그 계정 이벤트가 하나도 없으면, 계정을 실어 보내지
    # 않는 환경일 수 있다. 그때는 계정 조건을 풀고 출처만으로 좁힌다 -
    # 여기서 빈 타임라인을 내면 사람은 '장애 때 아무 일도 없었다' 로 읽는다.
    sources = item.get("sources") or []
    account_id = item.get("account_id") or ""

    where, args = [], []
    account_scoped = False
    if account_id:
        with _connect() as probe_conn, probe_conn.cursor() as probe:
            probe.execute(
                "SELECT 1 FROM events WHERE account_id = %s LIMIT 1", (account_id,)
            )
            account_scoped = probe.fetchone() is not None
    if account_scoped:
        where.append(" AND account_id = %s")
        args.append(account_id)
    if sources:
        where.append(" AND source = ANY(%s)")
        args.append(sources)

    source_sql = "".join(where)
    source_arg = args

    with _connect() as conn, conn.cursor() as cur:
        _ensure_table(cur)

        # ---- 알람 ----
        cur.execute(
            f"""
            SELECT occurred_at, severity, source, event_type, message, fingerprint
              FROM events
             WHERE occurred_at BETWEEN %s AND %s{source_sql}
             ORDER BY occurred_at
             LIMIT 300
            """,
            [start, end] + source_arg,
        )
        events = _rows(cur)

        # 지문별 묶음. 300건을 그대로 문서에 넣으면 읽을 수 없다.
        cur.execute(
            f"""
            SELECT fingerprint,
                   count(*) AS c,
                   min(occurred_at) AS first_seen,
                   max(occurred_at) AS last_seen,
                   (array_agg(message ORDER BY occurred_at))[1] AS sample,
                   (array_agg(severity ORDER BY
                     CASE severity WHEN 'critical' THEN 1 WHEN 'error' THEN 2
                                   WHEN 'warning' THEN 3 ELSE 4 END))[1] AS severity,
                   (array_agg(source ORDER BY occurred_at))[1] AS source
              FROM events
             WHERE occurred_at BETWEEN %s AND %s{source_sql}
             GROUP BY fingerprint
             ORDER BY c DESC
             LIMIT 20
            """,
            [start, end] + source_arg,
        )
        by_kind = _rows(cur)

        # ---- 작업 ----
        # 이 구간에 만들어졌거나 확정된 작업. 사람이 한 일이 원인일 수 있다.
        #
        # 있는지 먼저 확인하고 나서 질의한다. _has_table 도 같은 커서로 질의하므로,
        # 순서가 뒤바뀌면 앞의 결과를 덮어쓰고 to_regclass 결과를 읽게 된다.
        works = []
        has_work = _has_table(cur, "work_orders")
        has_snapshots = _has_table(cur, "resource_snapshots")
        if has_work:
            # 계정을 지정했으면 그 계정의 작업만 본다. 작업 기록에는 계정이
            # 있으므로 좁힐 수 있다(이벤트와 달리). 다른 고객사 계정에서
            # 같은 시간에 한 작업이 이 장애의 타임라인에 들어올 이유가 없다.
            scope_sql = " AND account_id = %s" if item["account_id"] else ""
            scope_arg = [item["account_id"]] if item["account_id"] else []
            cur.execute(
                f"""
                SELECT id, ticket, title, customer, account_id, region,
                       operator, status, created_at, closed_at,
                       before_snapshot_id, after_snapshot_id
                  FROM work_orders
                 WHERE (created_at BETWEEN %s AND %s
                        OR closed_at BETWEEN %s AND %s){scope_sql}
                 ORDER BY created_at
                """,
                [start, end, start, end] + scope_arg,
            )
            works = _rows(cur)

        # ---- 리소스 변경 ----
        # 구간을 감싸는 스냅샷 두 개를 찾는다.
        #   before: 구간 시작 이전의 가장 마지막 것
        #   after : 구간 종료 이후의 가장 처음 것 (없으면 구간 안의 마지막 것)
        # 이 둘을 비교하면 '장애 전후로 인프라가 어떻게 달라졌나' 가 나온다.
        snap_before = snap_after = None
        if item["account_id"] and item["region"] and has_snapshots:
            cur.execute(
                """
                SELECT snapshot_id, collected_at FROM resource_snapshots
                 WHERE complete AND account_id = %s AND region = %s AND collected_at <= %s
                 ORDER BY collected_at DESC LIMIT 1
                """,
                (item["account_id"], item["region"], start),
            )
            row = cur.fetchone()
            snap_before = {"snapshot_id": row[0], "collected_at": row[1]} if row else None

            cur.execute(
                """
                SELECT snapshot_id, collected_at FROM resource_snapshots
                 WHERE complete AND account_id = %s AND region = %s AND collected_at >= %s
                 ORDER BY collected_at ASC LIMIT 1
                """,
                (item["account_id"], item["region"], start),
            )
            row = cur.fetchone()
            snap_after = {"snapshot_id": row[0], "collected_at": row[1]} if row else None

    # 두 스냅샷이 다를 때만 비교한다. 같으면 구간 안에 수집이 없었다는 뜻이다.
    rdiff, diff_note = None, None
    if snap_before and snap_after and snap_before["snapshot_id"] != snap_after["snapshot_id"]:
        from app.resources import diff, ResourceError
        try:
            rdiff = diff(psycopg_uri(),
                         base_id=snap_before["snapshot_id"],
                         target_id=snap_after["snapshot_id"])
        except ResourceError as e:
            diff_note = str(e)
    elif not item["account_id"]:
        diff_note = "계정을 지정하지 않아 리소스 변경을 볼 수 없습니다."
    else:
        diff_note = (
            "이 구간을 감싸는 스냅샷이 한 개뿐입니다. "
            "수집 주기보다 장애 구간이 짧으면 변경을 잡아낼 수 없습니다."
        )

    # ---- 한 시간축으로 합치기 ----
    timeline = []
    for e in events:
        timeline.append({
            "at": e["occurred_at"], "kind": "alarm",
            "severity": e["severity"],
            "text": e["message"],
            "detail": f"{e['source']} · {e['event_type']}",
        })
    for w in works:
        timeline.append({
            "at": w["created_at"], "kind": "work", "severity": "info",
            "text": f"작업 시작: {w['title']}",
            "detail": f"#{w['id']} · {w['operator']}" + (f" · {w['ticket']}" if w["ticket"] else ""),
        })
        if w["closed_at"] and start <= w["closed_at"] <= end:
            timeline.append({
                "at": w["closed_at"], "kind": "work", "severity": "info",
                "text": f"작업 증적 확정: {w['title']}",
                "detail": f"#{w['id']} · {w['operator']}",
            })
    if rdiff:
        # 스냅샷은 '두 시점 사이' 만 알려주므로 정확한 변경 시각을 모른다.
        # 뒤 스냅샷의 수집 시각에 세우고, 구간이라는 사실을 함께 적는다.
        at = snap_after["collected_at"]
        s = rdiff["summary"]
        timeline.append({
            "at": at, "kind": "change", "severity": "warning",
            "text": f"리소스 변경 {s['added'] + s['removed'] + s['modified']}건 "
                    f"(생성 {s['added']} / 삭제 {s['removed']} / 변경 {s['modified']})",
            "detail": f"{snap_before['collected_at']:%m-%d %H:%M} ~ {at:%m-%d %H:%M} 사이",
        })
    timeline.sort(key=lambda t: t["at"])

    return {
        "window": {"start": start, "end": end,
                   "lead": LEAD_MINUTES, "tail": TAIL_MINUTES},
        "sources": sources,
        "account_scoped": account_scoped,
        "scope_account": account_id,
        "events": events,
        "event_count": len(events),
        "by_kind": by_kind,
        "works": works,
        "snap_before": snap_before,
        "snap_after": snap_after,
        "rdiff": rdiff,
        "diff_note": diff_note,
        "timeline": timeline,
    }


def _has_table(cur, name):
    cur.execute("SELECT to_regclass(%s)", (f"public.{name}",))
    return cur.fetchone()[0] is not None


# ----------------------------------------------------------------------
# 고객 제출본
# ----------------------------------------------------------------------
# 내부 RCA 와 기록은 공유하고 출력만 나눈다. 새 테이블을 만들지 않는 이유는
# 근거가 두 벌이 되면 나중에 어긋났을 때 어느 쪽이 맞는지 알 수 없어서다.
# 고객 제출본은 내부 기록에서 '골라내고 다듬은 파생물' 이지 별개의 사실이 아니다.

def update_customer(incident_id, fields):
    """고객 제출본의 칸을 저장한다. 허용된 칸만 갱신한다."""
    unknown = set(fields) - set(CUSTOMER_FIELDS)
    if unknown:
        raise IncidentError(f"고칠 수 없는 항목입니다: {', '.join(sorted(unknown))}")
    if not fields:
        return

    with _connect() as conn, conn.cursor() as cur:
        _ensure_table(cur)
        assignments = ", ".join(f"{k} = %s" for k in fields)
        # 아직 안 만들었으면 작성 중으로 올린다.
        cur.execute(
            f"UPDATE incidents SET {assignments}, "
            f"customer_status = CASE WHEN customer_status = 'none' THEN 'draft' "
            f"                       ELSE customer_status END, "
            f"updated_at = now() "
            f"WHERE id = %s AND customer_status <> 'sent' RETURNING id",
            list(fields.values()) + [incident_id],
        )
        if cur.fetchone() is None:
            current = get(incident_id)
            if current is None:
                raise IncidentError("장애 기록을 찾지 못했습니다.")
            raise IncidentError("이미 고객사에 제출된 보고서는 고칠 수 없습니다.")


def send_customer(incident_id):
    """고객 제출본을 제출 상태로 바꾼다.

    내부 RCA 의 확정과 별개다. 보통 내부에서 먼저 정리하고 며칠 뒤 나간다.
    """
    item = get(incident_id)
    if item is None:
        raise IncidentError("장애 기록을 찾지 못했습니다.")

    missing = [
        CUSTOMER_FIELD_LABEL[f]
        for f in ("customer_timeline", "customer_cause")
        if not item[f].strip()
    ]
    if missing:
        raise IncidentError(
            f"{' / '.join(missing)} 칸이 비어 있습니다. "
            "이 둘이 고객 보고서의 본체입니다."
        )

    with _connect() as conn, conn.cursor() as cur:
        cur.execute(
            "UPDATE incidents SET customer_status = 'sent', customer_sent_at = now() "
            "WHERE id = %s AND customer_status <> 'sent' RETURNING id",
            (incident_id,),
        )
        if cur.fetchone() is None:
            raise IncidentError("이미 제출된 보고서입니다.")


def draft_customer(item, data):
    """고객 제출본 초안을 만든다. 저장하지 않고 값만 돌려준다.

    타임라인은 원본을 마일스톤으로 접는다. 알람 17줄을 그대로 내보낼 수 없고,
    그렇다고 사람이 빈 칸에서 시작하게 할 이유도 없다.

    여기서 만드는 문장은 '시각 + 중립적인 사실' 까지다.
    "원인 조사 착수" 같은 것은 앱이 알 수 없으므로 사람이 써넣어야 한다.
    내부 식별자(i-..., sg-..., 티켓 번호, 작업자 이름)는 절대 넣지 않는다.
    그걸 빼는 게 이 문서를 따로 만드는 이유이기 때문이다.
    """
    lines = []
    events = data["events"]

    if events:
        first, last = events[0], events[-1]
        lines.append((first["occurred_at"], "모니터링 알람 감지"))
        if last["occurred_at"] != first["occurred_at"]:
            lines.append((last["occurred_at"], "마지막 알람 발생"))

    for w in data["works"]:
        # 티켓 번호와 작업자는 뺀다. 작업이 있었다는 사실만 남긴다.
        lines.append((w["created_at"], f"작업 수행: {w['title']}"))

    if data["rdiff"] and data["snap_after"]:
        lines.append((data["snap_after"]["collected_at"], "설정 변경 확인"))

    lines.sort(key=lambda x: x[0])
    timeline = "\n".join(f"{at:%H:%M}  {text}" for at, text in lines)
    if timeline:
        timeline += "\n\n(위는 기록에서 뽑은 시각입니다. 문장을 고치고, "
        timeline += "조사 착수·복구 확인 같은 항목을 직접 채워 주세요.)"

    # 서술은 내부 내용을 복사해 넣는다. 빈 칸에서 다시 쓰는 게 아니라
    # 다듬는 일이 되도록. 내부 식별자가 섞여 있으므로 화면에서 경고한다.
    return {
        "customer_timeline": timeline,
        "customer_impact": item["impact"],
        "customer_cause": item["cause"],
        "customer_action": item["action"],
        "customer_prevention": item["prevention"],
    }
