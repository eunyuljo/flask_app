# app/noise.py
# 알람 노이즈를 보고 줄이는 쪽을 담당한다.
#   - 어떤 알람이 얼마나 시끄러운가 (지문별 랭킹)
#   - 그 알람에 억제 규칙과 런북이 붙어 있는가
#   - 규칙을 만들고 지우기
#
# 실제 억제 판정은 여기가 아니라 api/normalize_handler.py 에서 한다.
# 알람은 Lambda 가 보내므로 판정도 거기서 일어나야 한다.
# 이 모듈은 사람이 보고 규칙을 정하는 쪽만 맡는다.

from flask import current_app


class NoiseError(Exception):
    """집계나 규칙 저장에 실패했을 때."""


def psycopg_uri():
    return current_app.config["DATABASE_URI"].replace(
        "postgresql+psycopg://", "postgresql://"
    )


def _rows(cur):
    cols = [d.name for d in cur.description]
    return [dict(zip(cols, r)) for r in cur.fetchall()]


def _connect():
    try:
        import psycopg
    except ImportError as e:
        raise NoiseError("psycopg 가 설치되어 있지 않습니다.") from e
    try:
        return psycopg.connect(psycopg_uri())
    except Exception as e:
        if type(e).__module__.split(".")[0] == "psycopg":
            raise NoiseError(f"DB 에 접속하지 못했습니다: {e}") from e
        raise


# 심각도를 '나쁜 순' 으로 세우는 SQL 조각.
# max(severity) 는 문자열 비교라 알파벳 순으로 warning 이 이긴다
# (critical < error < info < warning). 묶음의 심각도를 그렇게 뽑으면
# critical 이 섞인 알람이 warning 으로 보인다.
SEVERITY_RANK = ("CASE severity WHEN 'critical' THEN 1 WHEN 'error' THEN 2 "
                 "WHEN 'warning' THEN 3 ELSE 4 END")


def ranking(hours=168, limit=30):
    """시끄러운 알람 순위.

    '건수' 만으로는 부족하다. 500번 났어도 절차가 있고 억제 규칙이 걸려
    있으면 관리되고 있는 것이고, 20번 났는데 아무것도 없으면 그게 문제다.
    그래서 런북 유무와 규칙 유무를 같은 줄에 붙여서 본다.
    """
    with _connect() as conn, conn.cursor() as cur:
        cur.execute("SELECT to_regclass('public.events')")
        if cur.fetchone()[0] is None:
            raise NoiseError("events 테이블이 없습니다. flask --app run init-db 를 실행하세요.")

        cur.execute(
            f"""
            SELECT e.fingerprint,
                   count(*) AS c,
                   -- event_type 은 지문의 재료라(sha256(event_type|source|key))
                   -- 한 지문 안에서 항상 같다. min() 으로 뽑아도 정확하다.
                   -- 절차가 아니라 어댑터로 고쳐야 하는 것(unparsed)을
                   -- 커버리지 판정에서 가려내는 데 쓴다.
                   min(e.event_type) AS event_type,
                   (array_agg(e.message  ORDER BY e.occurred_at DESC))[1] AS sample,
                   -- 알람 이름. 사람이 목록에서 알아보는 것은 이 값이지
                   -- 메시지가 아니다. CloudWatch 메시지는 NewStateReason 이라
                   -- "Threshold Crossed: 1 datapoint [243.2 ...]" 로 시작한다 -
                   -- 측정값이라 쓸모는 있지만 한눈에 구분이 안 된다.
                   (array_agg(e.meta->>'alarmname' ORDER BY e.occurred_at DESC))[1]
                       AS alarm_name,
                   (array_agg(e.source   ORDER BY e.occurred_at DESC))[1] AS source,
                   (array_agg(e.severity ORDER BY {SEVERITY_RANK.replace('severity', 'e.severity')}))[1]
                       AS severity,
                   min(e.occurred_at) AS first_seen,
                   max(e.occurred_at) AS last_seen,
                   r.window_minutes,
                   r.muted,
                   r.note,
                   (rb.fingerprint IS NOT NULL) AS has_runbook,
                   s.sent_count,
                   s.suppressed_count
              FROM events e
              LEFT JOIN alarm_rules r  ON r.fingerprint = e.fingerprint
              LEFT JOIN alarm_state s  ON s.fingerprint = e.fingerprint
              -- 런북은 고객사별로 여러 개일 수 있다. 있는지만 보면 되므로
              -- DISTINCT 로 접어서 조인한다. 그냥 조인하면 건수가 부풀려진다.
              LEFT JOIN (SELECT DISTINCT fingerprint FROM runbooks) rb
                     ON rb.fingerprint = e.fingerprint
             WHERE e.occurred_at >= now() - make_interval(hours => %s)
             GROUP BY e.fingerprint, r.window_minutes, r.muted, r.note,
                      rb.fingerprint, s.sent_count, s.suppressed_count
             ORDER BY c DESC, last_seen DESC
             LIMIT %s
            """,
            (hours, limit),
        )
        return _rows(cur)


def summary(hours=168):
    """전체 그림. 상위 몇 종이 전체의 몇 %를 차지하는지."""
    with _connect() as conn, conn.cursor() as cur:
        cur.execute(
            """
            WITH per_kind AS (
                SELECT e.fingerprint,
                       count(*) AS c,
                       (rb.fingerprint IS NOT NULL) AS has_runbook
                  FROM events e
                  LEFT JOIN (SELECT DISTINCT fingerprint FROM runbooks) rb
                         ON rb.fingerprint = e.fingerprint
                 WHERE e.occurred_at >= now() - make_interval(hours => %s)
                 GROUP BY e.fingerprint, rb.fingerprint
            )
            SELECT
                COALESCE(sum(c), 0)                                        AS total,
                count(*)                                                   AS kinds,
                -- 상위 5종이 차지하는 비중. 이 값이 높으면 몇 개만 잡아도
                -- 노이즈가 크게 줄어든다는 뜻이다.
                COALESCE((SELECT sum(c) FROM (
                    SELECT c FROM per_kind ORDER BY c DESC LIMIT 5
                ) t), 0)                                                   AS top5,
                -- 커버리지를 두 가지로 센다. 종 기준과 건수 기준이 다르다.
                --   종 기준  : 절차를 몇 종에 썼나 (해야 할 일의 양)
                --   건수 기준: 당직자가 받는 알람 중 얼마나 절차가 있나 (실제 체감)
                -- 시끄러운 알람 한 종에 절차를 쓰면 건수 기준만 확 오른다.
                -- 두 숫자가 벌어져 있으면 그 사실 자체가 정보다.
                count(*) FILTER (WHERE has_runbook)                        AS covered_kinds,
                COALESCE(sum(c) FILTER (WHERE has_runbook), 0)             AS covered_events
              FROM per_kind
            """,
            (hours,),
        )
        total, kinds, top5, covered_kinds, covered_events = cur.fetchone()

        cur.execute("SELECT to_regclass('public.alarm_rules')")
        rules = muted = 0
        suppressed = 0
        if cur.fetchone()[0] is not None:
            cur.execute("SELECT count(*), count(*) FILTER (WHERE muted) FROM alarm_rules")
            rules, muted = cur.fetchone()
            cur.execute("SELECT COALESCE(sum(suppressed_count), 0) FROM alarm_state")
            suppressed = cur.fetchone()[0]

    return {
        "hours": hours,
        "total": total,
        "kinds": kinds,
        "top5": top5,
        "top5_pct": round(top5 * 100 / total) if total else 0,
        "covered_kinds": covered_kinds,
        "covered_events": covered_events,
        "covered_kinds_pct": round(covered_kinds * 100 / kinds) if kinds else 0,
        "covered_events_pct": round(covered_events * 100 / total) if total else 0,
        "rules": rules,
        "muted": muted,
        "suppressed": suppressed,
    }


def save_rule(fingerprint, window_minutes, muted, note, author, sample=""):
    """억제 규칙을 만들거나 갱신한다."""
    if not fingerprint.strip():
        raise NoiseError("지문이 없습니다.")
    try:
        window_minutes = int(window_minutes or 0)
    except (TypeError, ValueError):
        raise NoiseError("억제 창은 숫자여야 합니다.")
    if window_minutes < 0:
        raise NoiseError("억제 창은 0 이상이어야 합니다.")

    # 아무 효과도 없는 규칙은 만들지 않는다. 목록만 늘리고,
    # "규칙이 걸려 있다" 는 착각을 준다.
    if window_minutes == 0 and not muted:
        raise NoiseError(
            "억제 창이 0 이고 muted 도 아니면 아무것도 억제하지 않습니다. "
            "분을 지정하거나 muted 를 켜세요."
        )

    with _connect() as conn, conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO alarm_rules (fingerprint, window_minutes, muted, note, author, sample)
            VALUES (%s, %s, %s, %s, %s, %s)
            ON CONFLICT (fingerprint) DO UPDATE SET
                window_minutes = EXCLUDED.window_minutes,
                muted          = EXCLUDED.muted,
                note           = EXCLUDED.note,
                author         = EXCLUDED.author,
                sample         = EXCLUDED.sample,
                updated_at     = now()
            """,
            (fingerprint.strip(), window_minutes, bool(muted),
             note.strip(), author, sample.strip()[:200]),
        )


def delete_rule(fingerprint):
    """억제 규칙을 지운다. 발송 이력(alarm_state)은 남긴다."""
    with _connect() as conn, conn.cursor() as cur:
        cur.execute(
            "DELETE FROM alarm_rules WHERE fingerprint = %s RETURNING fingerprint",
            (fingerprint,),
        )
        if cur.fetchone() is None:
            raise NoiseError("규칙을 찾지 못했습니다.")


# ----------------------------------------------------------------------
# 이 알람이 실제로 장애로 이어졌는가
# ----------------------------------------------------------------------
# 순위표는 '얼마나 시끄러운가' 까지만 말한다. 억제할지 말지를 정하려면
# 다른 물음이 필요하다 - 이 알람이 실제 장애의 신호였던 적이 있는가.
#
# 500번 났어도 한 번도 장애로 이어지지 않았다면 억제를 검토할 만하고,
# 20번 났는데 그중 한 번이 결제 장애였다면 절대 억제하면 안 된다.
# 지금은 그 구분이 화면 어디에도 없다.

def incident_links(fingerprints=None):
    """지문별로 장애와 이어진 횟수. {지문: {...}}

    표가 없으면 빈 dict 다. 이 정보를 못 읽는다고 노이즈 화면이 안 뜨면
    곤란하다 - 순위표만으로도 쓸모가 있다.

    기간으로 자르지 않는다. 반년 전에 장애를 냈던 알람도 여전히 장애를
    낼 수 있는 알람이다.
    """
    with _connect() as conn, conn.cursor() as cur:
        cur.execute("SELECT to_regclass('public.incident_fingerprints')")
        if cur.fetchone()[0] is None:
            return {}

        where, params = "", []
        if fingerprints:
            where = " WHERE f.fingerprint = ANY(%s)"
            params.append(list(set(fingerprints)))

        cur.execute(
            f"""
            SELECT f.fingerprint,
                   count(*)                                        AS incidents,
                   count(*) FILTER (WHERE i.status = 'published')   AS published,
                   -- 사람이 "이 알람 때문에 쓴다" 고 지목한 것.
                   -- 시간이 겹쳐서 딸려온 것과 근거의 무게가 다르다.
                   count(*) FILTER (WHERE f.origin)                 AS origin,
                   max(i.started_at)                                AS last_at,
                   (array_agg(i.title ORDER BY i.started_at DESC))[1] AS last_title,
                   (array_agg(i.id ORDER BY i.started_at DESC))[1]    AS last_id
              FROM incident_fingerprints f
              JOIN incidents i ON i.id = f.incident_id
              {where}
             GROUP BY f.fingerprint
            """,
            params,
        )
        return {r["fingerprint"]: r for r in _rows(cur)}


# 억제를 검토할 만한 최소 건수. 이보다 적으면 시끄럽다고 하기 어렵다.
NOISY_ENOUGH = 20


def advise(rows, links):
    """순위표에 판단을 붙인다.

    rows: ranking() 결과, links: incident_links() 결과.

    두 가지를 찾는다.
      risky   : 장애로 이어진 적이 있는데 억제(muted)되어 있다
      suppress: 시끄러운데 장애로 이어진 적이 없고 규칙도 없다

    앞의 것이 훨씬 중요하다. 뒤의 것은 '검토해 보라' 지만, 앞의 것은
    다음 장애의 첫 신호를 우리가 스스로 껐다는 뜻이다.
    """
    out = []
    for row in rows:
        link = links.get(row["fingerprint"])
        caused = bool(link and link["incidents"])
        muted = bool(row.get("muted"))
        suppressed = row.get("window_minutes") is not None

        verdict, why = None, ""
        if caused and muted:
            verdict = "risky"
            why = (f"장애 {link['incidents']}건과 이어진 알람인데 꺼져 있습니다. "
                   "다음 장애의 첫 신호를 우리가 껐을 수 있습니다.")
        elif caused:
            verdict = "keep"
            why = (f"장애 {link['incidents']}건과 이어졌습니다"
                   + (f" (사람이 지목한 것 {link['origin']}건)"
                      if link["origin"] else "")
                   + ". 억제 대상이 아닙니다.")
        elif row["c"] >= NOISY_ENOUGH and not suppressed:
            verdict = "suppress"
            why = (f"{row['c']}회 났지만 장애로 이어진 적이 없습니다. "
                   "억제 규칙을 검토할 만합니다.")

        # 절차 축 판정을 같은 줄에 붙인다. 정렬에는 쓰지 않는다 -
        # 이 목록은 '얼마나 시끄러운가' 순이고, 절차를 쓸 순서는
        # uncovered() 가 따로 낸다.
        cov, cov_why = coverage(row, link)
        out.append({**row, "link": link, "verdict": verdict, "why": why,
                    "coverage": cov, "coverage_why": cov_why})

    # 위험한 억제를 맨 위로. 아래로 스크롤해야 보이면 안 본다.
    order = {"risky": 0, "suppress": 1, "keep": 2, None: 3}
    out.sort(key=lambda r: (order[r["verdict"]], -r["c"]))
    return out


# ----------------------------------------------------------------------
# 절차 축 판정 — 이 알람에 절차가 있어야 하는가
# ----------------------------------------------------------------------
# 위의 advise() 는 억제 축만 본다. "이 알람을 꺼도 되는가."
# 여기는 다른 물음이다. "이 알람이 왔을 때 무엇을 해야 하는지 적혀 있는가."
#
# 두 축을 한 verdict 에 섞지 않는다. 같은 알람이 '억제해도 된다' 이면서
# 동시에 '절차가 필요하다' 일 수 있고, 하나로 뭉개면 둘 다 못 읽는다.
#
# ── 이 판정을 만든 이유 ─────────────────────────────────────────────
# ranking() 은 진작부터 has_runbook 을 뽑고 있었다. 화면도 줄마다
# "절차 없음" 을 적고 있었다. 그런데 30줄을 눈으로 훑어야 보였고,
# 어느 것부터 써야 하는지는 아무 데서도 말해주지 않았다.
#
# 그래서 runbooks 테이블이 0건이다. 절차를 쓰는 화면도, AI 초안도 이미
# 있는데 아무도 안 썼다. 어디서 시작할지 몰라서다.

# 이만큼 반복되면 절차를 쓸 값어치가 있다.
# NOISY_ENOUGH(20) 와 다른 값을 쓴다. 억제를 검토하려면 '시끄럽다' 는
# 증거가 꽤 있어야 하지만, 절차는 세 번만 반복돼도 쓰는 편이 낫다.
# 문턱을 같이 두면 20회 미만은 영영 절차가 안 생긴다.
WORTH_A_RUNBOOK = 3

# 한 번만 나도 절차가 있어야 하는 심각도.
# 새벽에 처음 보는 critical 앞에서 검색을 시작하게 두지 않는다.
ALWAYS_WORTH = ("critical", "error")

# 절차가 아니라 어댑터로 고쳐야 하는 것. 내용을 못 읽은 페이로드에
# 대응 절차를 쓰라고 하면 안 된다.
NOT_A_RUNBOOK_TARGET = ("unparsed", "diagnose", "console")

# 급한 순서. 숫자가 작을수록 먼저 쓴다.
COVERAGE_ORDER = {"proven": 0, "urgent": 1, "frequent": 2, "covered": 8, None: 9}

# 같은 판정 안에서는 심각도가 건수보다 먼저다.
# 건수로만 줄을 세우면 3번 난 error 셋이 3번 난 critical 위에 오는데,
# 절차를 하나만 쓸 시간이 있다면 critical 부터 써야 한다.
SEVERITY_SORT = {"critical": 0, "error": 1, "warning": 2, "info": 3}


def coverage(row, link=None):
    """이 알람에 절차가 있어야 하는가, 있는가. (판정, 이유)

    부수효과가 없는 순수 함수다. row 는 ranking() 한 줄, link 는
    incident_links() 의 그 지문 항목(없으면 None).

    판정
      covered  : 절차가 있다
      proven   : 장애로 이어졌는데 절차가 없다      <- 제일 급하다
      urgent   : 심각도가 높은데 절차가 없다
      frequent : 반복되는데 절차가 없다
      None     : 드물고 가벼우니 아직 쓸 이유가 없다
    """
    if row.get("event_type") in NOT_A_RUNBOOK_TARGET:
        # 절차의 문제가 아니다. 여기에 '절차 없음' 을 띄우면 진짜 빈
        # 자리가 이것들에 묻힌다.
        return None, ""

    if row.get("has_runbook"):
        return "covered", "절차가 있습니다."

    count = row.get("c") or 0
    severity = str(row.get("severity") or "").lower()
    incidents = (link or {}).get("incidents") or 0

    if incidents:
        return "proven", (
            f"장애 {incidents}건과 이어진 알람인데 절차가 없습니다. "
            "무엇을 해야 하는지 이미 한 번 겪었습니다."
        )
    if severity in ALWAYS_WORTH:
        return "urgent", (
            f"{severity} 인데 절차가 없습니다. "
            "한 번만 나도 새벽에 찾아 헤매게 됩니다."
        )
    if count >= WORTH_A_RUNBOOK:
        return "frequent", (
            f"{count}회 반복되는데 절차가 없습니다. "
            "매번 같은 판단을 처음부터 다시 하고 있습니다."
        )
    return None, ""


def uncovered(rows, links=None, limit=5):
    """절차를 다음에 써야 할 알람. 급한 순으로.

    순위표 전체를 다시 정렬하지 않고 따로 뽑는다. 순위표는 '얼마나
    시끄러운가' 순인데, 절차를 쓸 순서는 그것과 다르다 - 한 번 난
    critical 이 50번 난 info 보다 먼저다.

    같은 목록을 두 기준으로 정렬할 수는 없으므로 물음마다 목록을 준다.
    """
    links = links or {}
    picked = []
    for row in rows:
        verdict, why = coverage(row, links.get(row.get("fingerprint")))
        if verdict in ("proven", "urgent", "frequent"):
            picked.append({**row, "coverage": verdict, "coverage_why": why})

    picked.sort(key=lambda r: (
        COVERAGE_ORDER[r["coverage"]],
        SEVERITY_SORT.get(str(r.get("severity") or "").lower(), 4),
        -(r.get("c") or 0),
    ))
    return picked[:limit] if limit else picked


def coverage_summary(rows):
    """화면 위쪽 숫자. 절차가 빈 자리가 몇 종인가."""
    counts = {"covered": 0, "proven": 0, "urgent": 0, "frequent": 0, "skipped": 0}
    for row in rows:
        verdict = row.get("coverage")
        if verdict in counts:
            counts[verdict] += 1
        elif row.get("event_type") in NOT_A_RUNBOOK_TARGET:
            # 절차 대상이 아닌 것을 '아직 쓸 이유 없음' 과 섞지 않는다.
            counts["skipped"] += 1
    counts["gap"] = counts["proven"] + counts["urgent"] + counts["frequent"]
    return counts


def advice_summary(rows):
    """화면 위쪽 숫자."""
    counts = {"risky": 0, "suppress": 0, "keep": 0}
    for row in rows:
        if row["verdict"] in counts:
            counts[row["verdict"]] += 1
    # 장애 이력을 아예 못 읽었는지 구분한다. 0 건과 '모름' 은 다르다.
    counts["linked"] = len([r for r in rows if r["link"]])
    return counts
