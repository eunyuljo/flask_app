# app/msr_pptx.py
# 월간 서비스 리뷰 자료를 슬라이드로 만든다.
#
# 배치 도우미(_title, _card, _table ...)는 app/report_pptx.py 것을 그대로
# 가져다 쓴다. 두 벌로 두면 표 여백이나 글꼴이 슬금슬금 달라져서, 같은
# 회사가 만든 문서로 보이지 않게 된다.
#
# 자료는 app/msr.py 가 모은 것을 그대로 쓴다. 여기서는 놓는 일만 한다.

from io import BytesIO

from pptx import Presentation
from pptx.util import Inches, Pt

from app.report_pptx import (
    ACCENT, DARK_BG, DARK_INK, DARK_MUTED, INK, MUTED, SURFACE, SEVERITY,
    BODY_FONT, HEAD_FONT, W, H, MARGIN,
    _bg, _blank, _card, _delta_text, _table, _text, _title,
)

# 컴플라이언스 심각도는 이름이 다르다(critical/high/medium/low).
# 색은 이벤트 쪽과 같은 것을 재사용한다 - 같은 빨강이 같은 뜻이어야 한다.
COMPLIANCE_COLOR = {
    "critical": SEVERITY["critical"],
    "high":     SEVERITY["error"],
    "medium":   SEVERITY["warning"],
    "low":      SEVERITY["info"],
}

SEV_LABEL = {"critical": "심각", "error": "오류", "warning": "경고", "info": "정보"}

# 슬라이드 한 장에 들어가는 표의 최대 줄 수.
# 줄 높이 0.32in 이라, 표가 시작하는 위치에 따라 넘칠 수 있는 지점이 다르다.
# 넘치면 화면 밖으로 나가서 인쇄물에서 아예 사라진다 - 보고서에서
# 조용히 사라지는 게 가장 나쁘다. 넘치면 잘라내고 "외 N건" 을 적는다.
MAX_ROWS_FULL = 12     # 제목 아래(1.9in)부터 시작하는 표
MAX_ROWS_HALF = 6      # 카드 아래(3.85in)부터 시작하는 표


def _overflow_note(items, shown):
    """잘라낸 줄이 있으면 알린다. 없으면 빈 문자열."""
    extra = len(items) - shown
    return f" 지면 관계로 {shown}건만 실었습니다(외 {extra}건)." if extra > 0 else ""


def _kpi(slide, left, top, width, value, label, *, color=INK, note=None):
    """숫자 하나짜리 카드."""
    height = Inches(1.35)
    _card(slide, left, top, width, height)
    _text(slide, left + Inches(0.2), top + Inches(0.16), width - Inches(0.4),
          Inches(0.6), str(value), size=32, bold=True, color=color, font=HEAD_FONT)
    _text(slide, left + Inches(0.2), top + Inches(0.82), width - Inches(0.4),
          Inches(0.28), label, size=11, color=MUTED)
    if note:
        _text(slide, left + Inches(0.2), top + Inches(1.06), width - Inches(0.4),
              Inches(0.24), note, size=9.5, color=MUTED)
    return height


def _slide_cover(prs, data):
    slide = _blank(prs)
    _bg(slide, prs, DARK_BG)
    _text(slide, MARGIN, Inches(2.5), W - MARGIN * 2, Inches(0.5),
          "월간 서비스 리뷰", size=16, color=DARK_MUTED)
    _text(slide, MARGIN, Inches(3.0), W - MARGIN * 2, Inches(1.1),
          data["customer"], size=46, bold=True, color=DARK_INK, font=HEAD_FONT)
    _text(slide, MARGIN, Inches(4.2), W - MARGIN * 2, Inches(0.5),
          data["label"], size=22, color=DARK_MUTED, font=HEAD_FONT)

    accounts = ", ".join(a["account_id"] for a in data["accounts"]) or "등록된 계정 없음"
    _text(slide, MARGIN, Inches(5.1), W - MARGIN * 2, Inches(0.4),
          f"대상 계정  {accounts}", size=11, color=DARK_MUTED)
    _text(slide, MARGIN, Inches(5.5), W - MARGIN * 2, Inches(0.4),
          f"집계 기간  {data['start']:%Y-%m-%d} ~ {data['end']:%Y-%m-%d} (UTC)",
          size=11, color=DARK_MUTED)


def _slide_overview(prs, data):
    slide = _blank(prs)
    _title(slide, "한 달 요약", f"{data['label']} · {data['days']}일간")

    alarms = data["alarms"]
    delta, delta_color = _delta_text(alarms["change"])
    incidents = data["incidents"]
    works = data["works"]
    comp = data["compliance"]

    gap = Inches(0.25)
    width = (W - MARGIN * 2 - gap * 3) / 4
    top = Inches(1.8)

    _kpi(slide, MARGIN, top, width, alarms["total"], "알람",
         note=f"전월 대비 {delta}" if alarms["prev_total"] else "전월 자료 없음")
    _kpi(slide, MARGIN + (width + gap), top, width, len(incidents), "장애",
         color=SEVERITY["critical"] if incidents else INK)
    _kpi(slide, MARGIN + (width + gap) * 2, top, width, len(works), "작업",
         note=f"진행 중 {len([w for w in works if w['status'] != 'closed'])}건")
    _kpi(slide, MARGIN + (width + gap) * 3, top, width,
         comp["total"] if comp else "—", "컴플라이언스 위반",
         color=SEVERITY["critical"] if comp and comp["by_severity"]["critical"] else INK,
         note=(f"critical {comp['by_severity']['critical']}건" if comp else "스냅샷 없음"))

    # 심각도별 알람. 막대 대신 표로 둔다 - 네 줄짜리 값에 차트를 쓰면
    # 읽는 데 오히려 시간이 더 걸린다.
    rows = [
        (SEV_LABEL[sev], f"{alarms['by_severity'][sev]}건",
         (f"{round(alarms['by_severity'][sev] / alarms['total'] * 100)}%"
          if alarms["total"] else "—"))
        for sev in ("critical", "error", "warning", "info")
    ]
    _text(slide, MARGIN, Inches(3.5), W - MARGIN * 2, Inches(0.3),
          "심각도별 알람", size=13, bold=True, font=HEAD_FONT)
    _table(slide, MARGIN, Inches(3.9), Inches(5.4), rows,
           ["등급", "건수", "비중"],
           col_widths=[Inches(1.8), Inches(1.8), Inches(1.8)])

    if data["notes"]:
        _text(slide, MARGIN, H - Inches(1.1), W - MARGIN * 2, Inches(0.6),
              " / ".join(data["notes"]), size=9.5, color=MUTED)


def _slide_sla(prs, data):
    slide = _blank(prs)
    sla = data.get("sla")
    _title(slide, "최초 대응 시간 (SLA)",
           "알람이 발생한 뒤 그 계정을 처음 들여다본 때까지")

    if not sla or not sla["by_severity"]:
        _text(slide, MARGIN, Inches(2.0), W - MARGIN * 2, Inches(0.5),
              "집계할 자료가 없습니다.", size=13, color=MUTED)
        return

    rows = []
    for r in sla["by_severity"]:
        if not r["tracked"]:
            rows.append((SEV_LABEL[r["severity"]], "목표 없음",
                         f"{r['total']}건", "—", "—", ("집계 제외", MUTED)))
            continue
        verdict = ("달성", SEVERITY["info"]) if r["met"] else ("미달", SEVERITY["critical"])
        rows.append((
            SEV_LABEL[r["severity"]],
            f"{r['target_minutes']}분",
            f"{r['total']}건",
            f"{r['median_minutes']}분",
            f"{r['worst_minutes']}분",
            verdict,
        ))

    _table(slide, MARGIN, Inches(1.9), W - MARGIN * 2, rows,
           ["등급", "목표", "알람", "중앙값", "최악", "판정"],
           col_widths=[Inches(1.6), Inches(1.6), Inches(1.6),
                       Inches(1.6), Inches(1.6), Inches(1.6)])

    unanswered = sum(r["unanswered"] for r in sla["by_severity"])
    notes = [
        "목표가 0분인 등급은 '목표 없음' 이라 집계에서 뺍니다. "
        "목표 없음을 항상 달성으로 보여주면 지표가 거짓말을 합니다.",
    ]
    if unanswered:
        notes.append(
            f"대응 기록이 없는 알람 {unanswered}건은 미대응으로 셉니다. "
            "실제로 대응했는데 감사 기록이 없으면 실제보다 나쁘게 나옵니다."
        )
    if sla.get("unattributed"):
        notes.append(
            f"계정을 알 수 없는 이벤트가 {sla['unattributed']}건 있어 "
            "위 숫자가 전체를 대표하지 않을 수 있습니다."
        )
    _text(slide, MARGIN, Inches(4.6), W - MARGIN * 2, Inches(1.6),
          "\n".join("· " + n for n in notes), size=10, color=MUTED)


def _slide_incidents(prs, data):
    slide = _blank(prs)
    incidents = data["incidents"]
    _title(slide, "장애", f"{data['label']}에 발생한 건")

    if not incidents:
        _text(slide, MARGIN, Inches(2.0), W - MARGIN * 2, Inches(0.5),
              "이번 달에 기록된 장애가 없습니다.", size=13, color=MUTED)
        return

    rows = []
    for i in incidents[:MAX_ROWS_FULL]:
        if i["ended_at"]:
            minutes = round((i["ended_at"] - i["started_at"]).total_seconds() / 60)
            duration = f"{minutes}분"
        else:
            duration = ("진행 중", SEVERITY["critical"])
        sent = "제출 완료" if i["customer_status"] == "sent" else ("미제출", SEVERITY["error"])
        rows.append((
            f"#{i['id']}",
            i["title"][:44],
            (SEV_LABEL.get(i["severity"], i["severity"]),
             SEVERITY.get(i["severity"], INK)),
            f"{i['started_at']:%m-%d %H:%M}",
            duration,
            sent,
        ))

    _table(slide, MARGIN, Inches(1.9), W - MARGIN * 2, rows,
           ["번호", "제목", "등급", "시작(UTC)", "지속", "고객 보고"],
           col_widths=[Inches(0.9), Inches(5.2), Inches(1.2),
                       Inches(1.8), Inches(1.4), Inches(1.4)])

    unsent = len([i for i in incidents if i["customer_status"] != "sent"])
    note = f"고객 제출본이 아직 안 나간 건이 {unsent}건 있습니다." if unsent else ""
    note += _overflow_note(incidents, len(rows))
    if note:
        _text(slide, MARGIN, H - Inches(0.9), W - MARGIN * 2, Inches(0.4),
              note.strip(), size=10, color=MUTED)


def _slide_works(prs, data):
    slide = _blank(prs)
    works = data["works"]
    _title(slide, "작업 기록", "고객사 계정에 무엇을 했는지")

    if not works:
        _text(slide, MARGIN, Inches(2.0), W - MARGIN * 2, Inches(0.5),
              "이번 달에 기록된 작업이 없습니다.", size=13, color=MUTED)
        return

    # 상태 이름은 app/work.py 것을 그대로 쓴다. 여기서 따로 적으면
    # 작업 화면과 보고서가 같은 상태를 다른 말로 부르게 된다.
    from app.work import STATUS_LABEL as label
    rows = []
    for w in works[:MAX_ROWS_FULL]:
        state = label.get(w["status"], w["status"])
        if w["status"] != "closed":
            state = (state, SEVERITY["warning"])
        rows.append((
            w["ticket"] or f"#{w['id']}",
            w["title"][:44],
            w["account_id"],
            w["operator"],
            f"{w['created_at']:%m-%d}",
            state,
        ))

    _table(slide, MARGIN, Inches(1.9), W - MARGIN * 2, rows,
           ["티켓", "작업", "계정", "담당", "요청일", "상태"],
           col_widths=[Inches(1.3), Inches(4.8), Inches(1.8),
                       Inches(1.4), Inches(1.1), Inches(1.5)])

    note = ("증적이 확정되지 않은 작업은 나중에 근거로 쓸 수 없습니다."
            if any(w["status"] != "closed" for w in works) else "")
    note += _overflow_note(works, len(rows))
    if note:
        _text(slide, MARGIN, H - Inches(0.9), W - MARGIN * 2, Inches(0.4),
              note.strip(), size=10, color=MUTED)


def _slide_compliance(prs, data):
    slide = _blank(prs)
    comp = data["compliance"]
    _title(slide, "컴플라이언스",
           "AWS Config 가 아니라 수집해 둔 리소스 스냅샷을 기준으로 점검한 결과")

    if not comp:
        _text(slide, MARGIN, Inches(2.0), W - MARGIN * 2, Inches(0.5),
              "리소스 스냅샷이 없어 점검하지 못했습니다.", size=13, color=MUTED)
        return

    gap = Inches(0.25)
    width = (W - MARGIN * 2 - gap * 3) / 4
    top = Inches(1.8)
    for i, sev in enumerate(("critical", "high", "medium", "low")):
        _kpi(slide, MARGIN + (width + gap) * i, top, width,
             comp["by_severity"][sev], sev, color=COMPLIANCE_COLOR[sev])

    _text(slide, MARGIN, Inches(3.45), W - MARGIN * 2, Inches(0.3),
          "먼저 조치할 항목", size=13, bold=True, font=HEAD_FONT)

    if comp["worst"]:
        rows = [
            ((v["severity"], COMPLIANCE_COLOR[v["severity"]]),
             v["title"][:36], v["resource_id"][:26], v["standard"])
            for v in comp["worst"][:MAX_ROWS_HALF]
        ]
        _table(slide, MARGIN, Inches(3.85), W - MARGIN * 2, rows,
               ["심각도", "항목", "리소스", "근거"],
               col_widths=[Inches(1.2), Inches(4.6), Inches(3.4), Inches(2.7)])
    else:
        _text(slide, MARGIN, Inches(3.9), W - MARGIN * 2, Inches(0.4),
              "즉시 조치가 필요한 항목이 없습니다.", size=12, color=MUTED)

    note = (f"{comp['checked_at']:%Y-%m-%d %H:%M} UTC 수집분 기준. "
            "수집하지 않은 항목은 점검 대상에 들어 있지 않습니다.")
    if comp.get("skipped"):
        note += (" 이번에 돌리지 못한 점검: " + ", ".join(comp["skipped"])
                 + " (위반이 없는 것이 아니라 보지 않은 것입니다).")
    if comp["excused"]:
        note += f" 승인된 예외 {comp['excused']}건은 집계에서 뺐습니다."
    note += _overflow_note(comp["worst"], min(len(comp["worst"]), MAX_ROWS_HALF))
    _text(slide, MARGIN, H - Inches(1.0), W - MARGIN * 2, Inches(0.5),
          note, size=9.5, color=MUTED)


def _slide_alarms(prs, data):
    slide = _blank(prs)
    top_alarms = data["top_alarms"]
    _title(slide, "자주 발생한 알람", "같은 종류로 묶어 센 것 (지문 기준)")

    if not top_alarms:
        _text(slide, MARGIN, Inches(2.0), W - MARGIN * 2, Inches(0.5),
              "집계할 알람이 없습니다.", size=13, color=MUTED)
        return

    rows = [
        (f"{a['times']}회",
         (SEV_LABEL.get(a["severity"], a["severity"]),
          SEVERITY.get(a["severity"], INK)),
         a["sample"][:56],
         ("있음", MUTED) if a["has_runbook"] else ("없음", SEVERITY["error"]))
        for a in top_alarms[:MAX_ROWS_FULL]
    ]
    _table(slide, MARGIN, Inches(1.9), W - MARGIN * 2, rows,
           ["횟수", "등급", "알람", "대응 절차"],
           col_widths=[Inches(1.1), Inches(1.2), Inches(7.3), Inches(1.6)])

    gaps = [a for a in top_alarms if not a["has_runbook"]]
    if gaps:
        _text(slide, MARGIN, H - Inches(1.1), W - MARGIN * 2, Inches(0.6),
              f"절차가 없는 알람이 {len(gaps)}종 있습니다. 절차가 없으면 같은 알람마다 "
              "사람이 처음부터 생각하고, 당직자가 바뀌면 대응도 바뀝니다.",
              size=10, color=MUTED)


def _slide_actions(prs, data):
    slide = _blank(prs)
    _title(slide, "다음 달 계획",
           "이번 달 자료에서 자동으로 뽑은 초안입니다 — 미팅 전에 손보세요")

    top = Inches(1.9)
    for item in data["actions"][:6]:
        height = Inches(0.78)
        _card(slide, MARGIN, top, W - MARGIN * 2, height)
        _text(slide, MARGIN + Inches(0.25), top + Inches(0.08), Inches(1.2),
              Inches(0.3), item["kind"], size=10, bold=True, color=ACCENT)
        _text(slide, MARGIN + Inches(1.5), top + Inches(0.06), W - MARGIN * 2 - Inches(1.8),
              Inches(0.32), item["text"], size=13, bold=True)
        _text(slide, MARGIN + Inches(1.5), top + Inches(0.40), W - MARGIN * 2 - Inches(1.8),
              Inches(0.3), item["why"], size=10, color=MUTED)
        top += height + Inches(0.14)


def _slide_caveats(prs, data):
    """이 숫자가 무엇을 말하지 않는지.

    보고서에 자신 없는 부분을 적어두는 건 약점이 아니다. 적지 않으면
    읽는 사람이 숫자를 실제보다 넓게 해석한다.
    """
    slide = _blank(prs)
    _bg(slide, prs, DARK_BG)
    _text(slide, MARGIN, Inches(1.3), W - MARGIN * 2, Inches(0.6),
          "이 숫자가 말하지 않는 것", size=30, bold=True,
          color=DARK_INK, font=HEAD_FONT)

    lines = [
        "컴플라이언스는 수집한 리소스만 점검합니다. IAM 사용자 MFA, root 액세스 키, "
        "CloudTrail 활성화 등은 현재 수집 대상이 아닙니다.",
        "최초 대응 시간은 '그 계정을 처음 들여다본 감사 기록' 으로 잽니다. "
        "이 도구를 거치지 않은 대응은 미대응으로 집계됩니다.",
        "알람 건수는 억제(중복 묶음) 이후 기준입니다. 원본 발생 횟수와 다릅니다.",
        "시각은 전부 UTC 입니다.",
    ]
    if data["notes"]:
        lines += data["notes"]

    _text(slide, MARGIN, Inches(2.3), W - MARGIN * 2, Inches(3.6),
          "\n\n".join("· " + line for line in lines),
          size=12.5, color=DARK_MUTED)


def build(data):
    """MSR 자료를 받아 pptx 를 만들고 BytesIO 로 돌려준다."""
    prs = Presentation()
    prs.slide_width, prs.slide_height = W, H

    _slide_cover(prs, data)
    _slide_overview(prs, data)
    _slide_sla(prs, data)
    _slide_incidents(prs, data)
    _slide_works(prs, data)
    _slide_compliance(prs, data)
    _slide_alarms(prs, data)
    _slide_actions(prs, data)
    _slide_caveats(prs, data)

    buf = BytesIO()
    prs.save(buf)
    buf.seek(0)
    return buf
