# app/evidence.py
# 작업 기록 + 스냅샷 차이를 '고객사에 낼 수 있는 증적 문서'로 바꾼다.
# 화면용 데이터를 그대로 재활용하지 않고 여기서 문서 형태로 다시 조립한다.
# 화면은 훑어보기 위한 것이고, 증적은 나중에 감사에서 근거로 읽히는 것이라
# 요구되는 정보가 다르기 때문이다(스냅샷 번호, 수집 시각, 작업자, 확정 시각).

from app.work import STATUS_LABEL

# 변경 종류를 문서에 쓸 한국어로.
CHANGE_LABEL = {"added": "생성", "removed": "삭제", "modified": "변경"}


def _fmt(value):
    """속성값 하나를 한 줄로. 리스트는 쉼표로 잇고, 없으면 (없음)."""
    if value is None:
        return "(없음)"
    if isinstance(value, list):
        return ", ".join(str(v) for v in value) if value else "(빈 목록)"
    if isinstance(value, dict):
        return ", ".join(f"{k}={v}" for k, v in sorted(value.items())) or "(빈 값)"
    return str(value)


def verdict(work, rdiff):
    """증적의 결론을 한 줄로 만든다.

    이 문서의 존재 이유가 여기다. "무엇이 바뀌었나" 는 diff 화면에도 있지만,
    "요청한 것 외에 바뀐 게 있나" 에 답하는 건 이 문서뿐이다.

    다만 앱은 '요청한 것' 이 무엇인지 판정할 수 없다. 요청은 자연어이고
    변경은 리소스 속성이라 자동으로 맞대볼 수단이 없다. 그래서 숫자만
    제시하고 판단은 읽는 사람에게 남긴다. 여기서 임의로 '정상'이라고
    찍으면 그게 곧 잘못된 증적이 된다.
    """
    if rdiff is None:
        return "변경 없음 (스냅샷 차이가 집계되지 않음)"

    s = rdiff["summary"]
    total = s["added"] + s["removed"] + s["modified"]
    if total == 0:
        return "리소스 변경 없음 — 작업 전후 상태가 동일합니다."
    return (
        f"리소스 변경 {total}건 (생성 {s['added']} / 삭제 {s['removed']} / 변경 {s['modified']}). "
        "아래 목록이 요청 범위 안에 있는지 확인이 필요합니다."
    )


def to_markdown(work, rdiff):
    """작업 증적을 Markdown 으로 만든다."""
    L = []
    a = L.append

    a(f"# 작업 증적 — {work['title']}")
    a("")
    if work["ticket"]:
        a(f"**티켓** `{work['ticket']}`")
        a("")

    a("| 항목 | 값 |")
    a("|---|---|")
    a(f"| 작업 번호 | #{work['id']} |")
    a(f"| 고객사 | {work['customer']} |")
    a(f"| 계정 / 리전 | `{work['account_id']}` / `{work['region']}` |")
    a(f"| 작업자 | {work['operator']} |")
    a(f"| 상태 | {STATUS_LABEL[work['status']]} |")
    a(f"| 생성 시각 | {work['created_at']} |")
    if work["closed_at"]:
        a(f"| 확정 시각 | {work['closed_at']} |")
    a("")

    if work["request"]:
        a("## 고객 요청")
        a("")
        a(work["request"])
        a("")

    if work["expected"]:
        a("## 예상한 변경")
        a("")
        a(work["expected"])
        a("")

    a("## 결론")
    a("")
    a(verdict(work, rdiff))
    a("")

    a("## 근거 스냅샷")
    a("")
    if rdiff is None:
        a("스냅샷이 두 개 모두 준비되지 않아 차이를 낼 수 없습니다.")
        a("")
    else:
        a("| | 스냅샷 | 수집 시각 | 리소스 수 | 출처 |")
        a("|---|---|---|---|---|")
        base, target = rdiff["base"], rdiff["target"]
        a(f"| 작업 전 | #{base.get('snapshot_id')} | {base.get('collected_at', '-')} "
          f"| {rdiff['base_count']} | {base.get('source', '-')} |")
        a(f"| 작업 후 | #{target.get('snapshot_id')} | {target.get('collected_at', '-')} "
          f"| {rdiff['target_count']} | {target.get('source', '-')} |")
        a("")

        changes = rdiff["changes"]
        a(f"## 변경 목록 ({len(changes)}건)")
        a("")
        if not changes:
            a("변경된 리소스가 없습니다.")
            a("")
        for c in changes:
            a(f"### [{CHANGE_LABEL[c['change']]}] `{c['resource_id']}` ({c['resource_type']})")
            a("")
            if c["change"] == "modified":
                a("| 속성 | 작업 전 | 작업 후 |")
                a("|---|---|---|")
                for f in c["fields"]:
                    a(f"| `{f['field']}` | {_fmt(f['before'])} | {_fmt(f['after'])} |")
            else:
                attrs = c["after"] if c["change"] == "added" else c["before"]
                a("| 속성 | 값 |")
                a("|---|---|")
                for k, v in sorted((attrs or {}).items()):
                    a(f"| `{k}` | {_fmt(v)} |")
            a("")

    if work["note"]:
        a("## 작업 메모")
        a("")
        a(work["note"])
        a("")

    a("---")
    a("")
    a("이 문서는 작업 전후에 수집한 리소스 스냅샷을 비교해 자동 생성했습니다.")
    a("스냅샷은 읽기 전용 조회로 수집되며, 수집 시점 사이에 일어난 변경은")
    a("개별 시각이 아니라 '두 스냅샷 사이'로만 표시됩니다.")

    return "\n".join(L)
