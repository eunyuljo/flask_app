# app/rca.py
# 장애 기록 + 조립된 타임라인을 사후 보고서(RCA) 문서로 바꾼다.
#
# 문서에서 지키는 규칙 하나: 앱이 모은 것과 사람이 쓴 것을 섞지 않는다.
# 타임라인은 데이터에서 나온 사실이고, 원인과 조치는 사람의 판단이다.
# 읽는 사람이 둘을 구분할 수 있어야 한다. 섞어놓으면 "앱이 원인을 판정했다" 로
# 읽히고, 그건 이 문서가 절대 하면 안 되는 주장이다.

from app.incident import FIELD_LABEL, STATUS_LABEL

KIND_MARK = {"alarm": "알람", "change": "변경", "work": "작업"}


def _fmt_window(item):
    if item["ended_at"]:
        minutes = int((item["ended_at"] - item["started_at"]).total_seconds() // 60)
        return (f"{item['started_at']:%Y-%m-%d %H:%M} ~ {item['ended_at']:%H:%M} UTC "
                f"({minutes}분)")
    return f"{item['started_at']:%Y-%m-%d %H:%M} UTC ~ (진행 중)"


def to_markdown(item, data):
    """사후 보고서를 Markdown 으로 만든다."""
    L = []
    a = L.append

    a(f"# 장애 사후 보고서 — {item['title']}")
    a("")

    a("| 항목 | 값 |")
    a("|---|---|")
    a(f"| 보고서 번호 | #{item['id']} |")
    a(f"| 심각도 | {item['severity']} |")
    a(f"| 발생 구간 | {_fmt_window(item)} |")
    if item["customer"]:
        a(f"| 고객사 | {item['customer']} |")
    if item["account_id"]:
        a(f"| 계정 / 리전 | `{item['account_id']}` / `{item['region']}` |")
    a(f"| 작성자 | {item['author']} |")
    a(f"| 상태 | {STATUS_LABEL[item['status']]} |")
    if item["published_at"]:
        a(f"| 제출 | {item['published_at']:%Y-%m-%d %H:%M} UTC |")
    a("")

    # ---- 사람이 쓴 부분을 앞에 둔다 ----
    # 읽는 사람이 제일 먼저 알고 싶은 것은 원인과 조치다.
    # 타임라인은 그 주장을 뒷받침하는 근거이므로 뒤에 붙인다.
    for field in ("impact", "cause", "action", "prevention"):
        a(f"## {FIELD_LABEL[field]}")
        a("")
        a(item[field].strip() or "_(작성되지 않음)_")
        a("")

    # ---- 여기서부터 앱이 모은 것 ----
    a("---")
    a("")
    a("## 타임라인")
    a("")
    w = data["window"]
    a(f"조사 구간: {w['start']:%Y-%m-%d %H:%M} ~ {w['end']:%m-%d %H:%M} UTC "
      f"(장애 구간 앞 {w['lead']}분, 뒤 {w['tail']}분 포함)")
    if data["sources"]:
        a("")
        a(f"관련 출처: {', '.join(data['sources'])}")
    else:
        a("")
        a("_출처를 좁히지 않아, 같은 시간대의 무관한 알람이 섞여 있을 수 있습니다._")
    a("")
    a("아래는 기록에서 그대로 뽑은 것입니다. 해석은 위의 '원인' 항목을 보십시오.")
    a("")

    if not data["timeline"]:
        a("이 구간에 기록된 알람·변경·작업이 없습니다.")
        a("")
    else:
        a("| 시각 | 종류 | 내용 |")
        a("|---|---|---|")
        for t in data["timeline"]:
            detail = f" <br>_{t['detail']}_" if t["detail"] else ""
            a(f"| {t['at']:%m-%d %H:%M:%S} | {KIND_MARK[t['kind']]} "
              f"| {t['text']}{detail} |")
        a("")

    a("## 알람 요약")
    a("")
    if not data["by_kind"]:
        a("이 구간에 알람이 없습니다.")
    else:
        a(f"이 구간에 이벤트 {data['event_count']}건, {len(data['by_kind'])}종류가 있었습니다.")
        a("")
        a("| 건수 | 심각도 | 출처 | 알람 | 처음 | 마지막 |")
        a("|---|---|---|---|---|---|")
        for k in data["by_kind"]:
            a(f"| {k['c']} | {k['severity']} | {k['source']} | {k['sample']} "
              f"| {k['first_seen']:%H:%M} | {k['last_seen']:%H:%M} |")
    a("")

    a("## 리소스 변경")
    a("")
    rdiff = data["rdiff"]
    if rdiff is None:
        a(data["diff_note"] or "이 구간의 리소스 변경을 확인할 수 없습니다.")
        a("")
    else:
        base, target = rdiff["base"], rdiff["target"]
        a(f"스냅샷 #{base.get('snapshot_id')} ({base.get('collected_at')}) "
          f"→ #{target.get('snapshot_id')} ({target.get('collected_at')})")
        a("")
        s = rdiff["summary"]
        if not rdiff["changes"]:
            a("두 스냅샷 사이에 변경이 없습니다.")
        else:
            a(f"생성 {s['added']} / 삭제 {s['removed']} / 변경 {s['modified']}")
            a("")
            a("| 종류 | 리소스 | 변경 내용 |")
            a("|---|---|---|")
            label = {"added": "생성", "removed": "삭제", "modified": "변경"}
            for c in rdiff["changes"]:
                if c["change"] == "modified":
                    detail = "<br>".join(
                        f"`{f['field']}` {f['before']} → {f['after']}"
                        for f in c["fields"]
                    )
                else:
                    detail = "—"
                a(f"| {label[c['change']]} | `{c['resource_id']}` ({c['resource_type']}) "
                  f"| {detail} |")
        a("")
        a("스냅샷은 두 수집 시점 사이의 변경을 뭉뚱그립니다. "
          "정확한 변경 시각은 이 기록으로 알 수 없습니다.")
        a("")

    if data["works"]:
        a("## 이 구간의 작업")
        a("")
        a("| # | 티켓 | 작업 | 작업자 | 시작 |")
        a("|---|---|---|---|---|")
        for wo in data["works"]:
            a(f"| {wo['id']} | {wo['ticket'] or '-'} | {wo['title']} "
              f"| {wo['operator']} | {wo['created_at']:%m-%d %H:%M} |")
        a("")

    a("---")
    a("")
    a("타임라인·알람 요약·리소스 변경은 기록에서 자동으로 모았습니다.")
    a("영향·원인·조치·재발 방지는 작성자가 직접 쓴 것입니다.")

    return "\n".join(L)
