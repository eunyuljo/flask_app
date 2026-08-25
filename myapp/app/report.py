# app/report.py
# 기간별 운영 리포트를 만든다. 이벤트 집계와 리소스 변경을 한 장으로 모으고,
# 필요하면 에이전트에게 요약문을 맡긴다(키가 없으면 집계만 나온다).

from datetime import datetime, timedelta, timezone

from flask import current_app

from app import db

from app.resources import psycopg_uri

# 표본이 이보다 적으면 순위를 신뢰할 수 없다고 알린다.
# 건수가 적으면 아무 문제 없는 인프라에서도 그럴듯한 1등이 만들어진다.
SMALL_SAMPLE = 30


class ReportError(Exception):
    """DB 가 없거나 테이블이 아직 없을 때."""


def _pct_change(current, previous):
    """전 기간 대비 증감률. 이전이 0이면 비율을 낼 수 없으므로 None."""
    if not previous:
        return None
    return round((current - previous) / previous * 100, 1)


def collect(days=7):
    """리포트에 들어갈 숫자를 모두 모은다.

    핵심은 '이번 기간'만이 아니라 '직전 같은 길이의 기간'도 함께 뽑는다는 점이다.
    절대 건수보다 변화가 훨씬 신뢰할 만한 신호이기 때문이다.
    """
    now = datetime.now(timezone.utc)
    start = now - timedelta(days=days)
    prev_start = now - timedelta(days=days * 2)

    try:
        with db.connect(ReportError) as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT to_regclass('public.events')")
                if cur.fetchone()[0] is None:
                    raise ReportError(
                        "events 테이블이 없습니다. flask --app run init-db 를 실행하세요."
                    )

                # ---- 기간별 총계 (이번 / 직전) ----
                cur.execute(
                    "SELECT count(*) FROM events WHERE occurred_at >= %s AND occurred_at < %s",
                    (start, now),
                )
                total = cur.fetchone()[0]
                cur.execute(
                    "SELECT count(*) FROM events WHERE occurred_at >= %s AND occurred_at < %s",
                    (prev_start, start),
                )
                prev_total = cur.fetchone()[0]

                cur.execute(
                    "SELECT count(*) FROM events "
                    "WHERE occurred_at >= %s AND occurred_at < %s "
                    "AND severity IN ('critical','error')",
                    (start, now),
                )
                alarm_total = cur.fetchone()[0]
                cur.execute(
                    "SELECT count(*) FROM events "
                    "WHERE occurred_at >= %s AND occurred_at < %s "
                    "AND severity IN ('critical','error')",
                    (prev_start, start),
                )
                prev_alarm_total = cur.fetchone()[0]

                # ---- 심각도별 ----
                cur.execute(
                    "SELECT severity, count(*) FROM events "
                    "WHERE occurred_at >= %s AND occurred_at < %s GROUP BY severity",
                    (start, now),
                )
                sev = dict(cur.fetchall())
                by_severity = [
                    {"name": s, "count": sev.get(s, 0)}
                    for s in ("critical", "error", "warning", "info")
                ]

                # ---- 출처별 (이번 vs 직전) ----
                # FULL OUTER JOIN 으로 이번에 사라진 출처도 남긴다.
                cur.execute(
                    """
                    SELECT COALESCE(c.source, p.source) AS source,
                           COALESCE(c.n, 0) AS now_n,
                           COALESCE(p.n, 0) AS prev_n
                    FROM (SELECT source, count(*) n FROM events
                          WHERE occurred_at >= %s AND occurred_at < %s
                            AND severity IN ('critical','error')
                          GROUP BY source) c
                    FULL OUTER JOIN
                         (SELECT source, count(*) n FROM events
                          WHERE occurred_at >= %s AND occurred_at < %s
                            AND severity IN ('critical','error')
                          GROUP BY source) p
                    USING (source)
                    ORDER BY now_n DESC, source
                    """,
                    (start, now, prev_start, start),
                )
                by_source = [
                    {"name": r[0], "count": r[1], "prev": r[2],
                     "change": _pct_change(r[1], r[2])}
                    for r in cur.fetchall()
                ]

                # ---- 반복 알람 (노이즈 후보) ----
                cur.execute(
                    """
                    SELECT fingerprint, count(*) n,
                           min(severity) AS severity, min(message) AS sample
                    FROM events
                    WHERE occurred_at >= %s AND occurred_at < %s
                    GROUP BY fingerprint
                    HAVING count(*) > 1
                    ORDER BY n DESC
                    LIMIT 10
                    """,
                    (start, now),
                )
                repeated = [
                    {"fingerprint": r[0], "count": r[1], "severity": r[2], "sample": r[3]}
                    for r in cur.fetchall()
                ]

                # ---- 조용한 출처 ----
                # 직전 기간엔 있었는데 이번엔 한 건도 없는 출처.
                # 정말 좋아진 것일 수도 있지만, 수집이 끊긴 것일 수도 있다.
                cur.execute(
                    """
                    SELECT DISTINCT source FROM events
                    WHERE occurred_at >= %s AND occurred_at < %s
                      AND source NOT IN (
                        SELECT DISTINCT source FROM events
                        WHERE occurred_at >= %s AND occurred_at < %s)
                    ORDER BY source
                    """,
                    (prev_start, start, start, now),
                )
                silent = [r[0] for r in cur.fetchall()]

                # ---- 리소스 변경 ----
                resource_changes = None
                cur.execute("SELECT to_regclass('public.resources')")
                if cur.fetchone()[0] is not None:
                    cur.execute(
                        """
                        SELECT count(*) FROM resource_snapshots
                        WHERE complete AND collected_at >= %s
                        """,
                        (start,),
                    )
                    snapshot_count = cur.fetchone()[0]
                    resource_changes = {"snapshot_count": snapshot_count}

    except ReportError:
        raise
    except Exception as e:
        if type(e).__module__.split(".")[0] == "psycopg":
            raise ReportError(f"DB 에 접속하지 못했습니다: {e}") from e
        raise

    return {
        "days": days,
        "start": start,
        "end": now,
        "total": total,
        "prev_total": prev_total,
        "total_change": _pct_change(total, prev_total),
        "alarm_total": alarm_total,
        "prev_alarm_total": prev_alarm_total,
        "alarm_change": _pct_change(alarm_total, prev_alarm_total),
        "by_severity": by_severity,
        "by_source": by_source,
        "repeated": repeated,
        "silent_sources": silent,
        "resources": resource_changes,
        # 표본이 적으면 순위를 믿지 말라는 표시
        "small_sample": total < SMALL_SAMPLE,
    }


# ----------------------------------------------------------------------
# 리소스 변경 (별도 조회)
# ----------------------------------------------------------------------
def resource_diff_summary():
    """최근 두 스냅샷의 차이를 리포트에 넣을 형태로 가져온다."""
    from app.resources import diff, ResourceError

    try:
        return diff(psycopg_uri())
    except ResourceError:
        return None
    except Exception:
        return None


# ----------------------------------------------------------------------
# Markdown 출력
# ----------------------------------------------------------------------
def to_markdown(data, rdiff=None, summary_text=None):
    """리포트를 Markdown 문자열로 만든다. 파일로 내려받아 공유하기 위한 것."""
    def arrow(change):
        if change is None:
            return "—"
        sign = "+" if change > 0 else ""
        return f"{sign}{change}%"

    lines = [
        f"# 운영 리포트 ({data['days']}일)",
        "",
        f"- 기간: {data['start'].strftime('%Y-%m-%d %H:%M')} ~ "
        f"{data['end'].strftime('%Y-%m-%d %H:%M')} (UTC)",
        f"- 생성: {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M')} (UTC)",
        "",
    ]

    if summary_text:
        lines += ["## 요약", "", summary_text, ""]

    lines += [
        "## 지표",
        "",
        "| 항목 | 이번 기간 | 직전 기간 | 변화 |",
        "|---|---|---|---|",
        f"| 전체 이벤트 | {data['total']} | {data['prev_total']} | {arrow(data['total_change'])} |",
        f"| 알람 대상 | {data['alarm_total']} | {data['prev_alarm_total']} | {arrow(data['alarm_change'])} |",
        "",
    ]

    if data["small_sample"]:
        lines += [
            f"> 표본이 {data['total']}건으로 적습니다. 아래 순위는 우연일 수 있으니 "
            "참고용으로만 보세요.",
            "",
        ]

    lines += ["## 심각도 분포", "", "| 심각도 | 건수 |", "|---|---|"]
    lines += [f"| {r['name']} | {r['count']} |" for r in data["by_severity"]]
    lines += [""]

    if data["by_source"]:
        lines += [
            "## 출처별 (critical + error)",
            "",
            "| 출처 | 이번 | 직전 | 변화 |",
            "|---|---|---|---|",
        ]
        lines += [
            f"| {r['name']} | {r['count']} | {r['prev']} | {arrow(r['change'])} |"
            for r in data["by_source"]
        ]
        lines += [""]

    if data["repeated"]:
        lines += ["## 반복 알람 (노이즈 후보)", "", "| 횟수 | 심각도 | 예시 |", "|---|---|---|"]
        lines += [
            f"| {r['count']} | {r['severity']} | {r['sample'][:60]} |"
            for r in data["repeated"]
        ]
        lines += [""]

    if data["silent_sources"]:
        lines += [
            "## 조용해진 출처",
            "",
            "직전 기간에는 이벤트가 있었으나 이번 기간에는 한 건도 없습니다.",
            "정말 안정된 것일 수도, 수집이 끊긴 것일 수도 있으니 확인이 필요합니다.",
            "",
        ]
        lines += [f"- `{s}`" for s in data["silent_sources"]]
        lines += [""]

    if rdiff and rdiff.get("changes"):
        s = rdiff["summary"]
        lines += [
            "## 인프라 변경",
            "",
            f"최근 스냅샷 #{rdiff['base']['snapshot_id']} → #{rdiff['target']['snapshot_id']}: "
            f"생성 {s['added']} · 변경 {s['modified']} · 삭제 {s['removed']}",
            "",
            "| 구분 | 리소스 | 종류 | 변경 내용 |",
            "|---|---|---|---|",
        ]
        label = {"added": "생성", "modified": "변경", "removed": "삭제"}
        for c in rdiff["changes"]:
            detail = ", ".join(
                f"{f['field']}: {f['before']} → {f['after']}" for f in c["fields"]
            ) or "—"
            lines.append(
                f"| {label[c['change']]} | `{c['resource_id']}` | {c['resource_type']} | {detail[:80]} |"
            )
        lines += [""]

    return "\n".join(lines)


# ----------------------------------------------------------------------
# 에이전트 요약 (선택)
# ----------------------------------------------------------------------
SUMMARY_PROMPT = """다음은 인프라 운영 리포트의 집계 결과입니다.
운영자가 읽을 3~5문장의 요약을 한국어로 작성하세요.

지켜야 할 것:
- 숫자를 그대로 나열하지 말고, 무엇이 눈에 띄는지 짚으세요.
- 절대 건수보다 직전 기간 대비 변화를 우선해서 보세요.
- 표본이 적다고 표시되어 있으면 단정하지 말고 그렇게 밝히세요.
- 알람이 몰린 곳이 원인이라고 단정하지 마세요. 원인일 수도, 피해자일 수도 있습니다.
- 추측을 사실처럼 쓰지 마세요. 확인이 필요한 것은 확인이 필요하다고 쓰세요.

집계 결과:
"""


def generate_summary(data, rdiff=None):
    """에이전트에게 요약문을 맡긴다. 설정이 없으면 None 을 돌려준다."""
    import json

    from app.agent_core import check_config, _build_client, _resolve_model, _provider_kwargs

    if check_config() is not None:
        return None

    cfg = current_app.config
    provider = cfg["AGENT_PROVIDER"]
    client = _build_client(provider, cfg)

    payload = {
        "기간_일": data["days"],
        "전체_이벤트": data["total"],
        "직전_기간_전체": data["prev_total"],
        "전체_변화율": data["total_change"],
        "알람_대상": data["alarm_total"],
        "직전_알람_대상": data["prev_alarm_total"],
        "알람_변화율": data["alarm_change"],
        "심각도별": {r["name"]: r["count"] for r in data["by_severity"]},
        "출처별": [
            {"출처": r["name"], "이번": r["count"], "직전": r["prev"], "변화율": r["change"]}
            for r in data["by_source"]
        ],
        "반복_알람": [
            {"횟수": r["count"], "예시": r["sample"]} for r in data["repeated"][:5]
        ],
        "조용해진_출처": data["silent_sources"],
        "표본_부족": data["small_sample"],
    }
    if rdiff:
        payload["인프라_변경"] = rdiff["summary"]

    message = SUMMARY_PROMPT + json.dumps(payload, ensure_ascii=False, indent=2)

    response = client.beta.messages.create(
        model=_resolve_model(provider, cfg["AGENT_MODEL"]),
        max_tokens=1500,
        messages=[{"role": "user", "content": message}],
        thinking={"type": "adaptive"},
        output_config={"effort": cfg["AGENT_EFFORT"]},
        **_provider_kwargs(provider, cfg),
    )

    # 안전 분류기가 거절했을 수 있으므로 stop_reason 을 먼저 본다.
    if getattr(response, "stop_reason", None) == "refusal":
        return None

    return "\n".join(
        b.text for b in response.content if b.type == "text"
    ).strip() or None
