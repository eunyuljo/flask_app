# app/compliance_xlsx.py
# 컴플라이언스 점검 결과를 엑셀로 만든다. 점검은 app/compliance.py 가 하고,
# 여기서는 시트에 배치하는 일만 한다.
#
# ── 왜 엑셀인가 ──────────────────────────────────────────────────────
# 화면은 "지금 무엇이 문제인가" 를 보는 데 맞춰져 있다. 고객사에 내는
# 보고서는 다르다. 담당자가 항목을 골라 담당자를 배정하고, 처리 여부를
# 옆 칸에 적고, 다음 달 것과 나란히 놓고 본다. 그건 엑셀이 하는 일이다.
#
# ── xlsxwriter 를 쓰는 이유 ──────────────────────────────────────────
# 새 파일을 쓰기만 하면 되고(기존 파일을 여는 일이 없다), 서식·자동 필터·
# 틀 고정을 한 번에 지정할 수 있다. openpyxl 은 읽기까지 되는 대신
# 쓰기만 할 때는 손이 더 간다.
#
# import 를 함수 안에서 한다. 이 패키지가 없다고 앱 전체가 뜨지 않으면
# 곤란하다 - 엑셀 내려받기 하나 때문에 알람 화면까지 죽는다.
# (report_pptx.py 는 모듈 맨 위에서 import 하는데, 그건 python-pptx 가
#  없으면 리포트 블루프린트 자체가 등록되지 않는다는 뜻이다. 같은 실수를
#  반복하지 않으려고 여기서는 늦게 부른다.)

from datetime import datetime, timezone
from io import BytesIO

from app import compliance


class ExcelNotAvailable(Exception):
    """xlsxwriter 가 없을 때."""


# 화면(style.css)과 같은 색을 쓴다. 리포트와 화면의 심각도 색이 다르면
# 같은 것을 두 번 배워야 한다.
SEVERITY_FILL = {
    "critical": "#FDE7E7",
    "high":     "#FDEDE3",
    "medium":   "#FEF6DF",
    "low":      "#EEF1F4",
}
SEVERITY_INK = {
    "critical": "#9B1C1C",
    "high":     "#9A3412",
    "medium":   "#854D0E",
    "low":      "#4B5563",
}


def _formats(wb):
    """시트마다 같은 서식을 쓰도록 한곳에서 만든다."""
    base = {"font_name": "맑은 고딕", "font_size": 10, "valign": "top"}
    f = {
        "title": wb.add_format({**base, "font_size": 16, "bold": True}),
        "sub": wb.add_format({**base, "font_color": "#62696F"}),
        "head": wb.add_format({
            **base, "bold": True, "bg_color": "#F0F2F5", "border": 1,
            "border_color": "#D8DDE3", "text_wrap": True,
        }),
        "cell": wb.add_format({**base, "text_wrap": True, "border": 1,
                               "border_color": "#E3E6EA"}),
        "mono": wb.add_format({**base, "font_name": "Consolas", "border": 1,
                               "border_color": "#E3E6EA"}),
        "num": wb.add_format({**base, "border": 1, "border_color": "#E3E6EA",
                              "align": "right"}),
        "date": wb.add_format({**base, "border": 1, "border_color": "#E3E6EA",
                               "num_format": "yyyy-mm-dd hh:mm"}),
        "kpi_label": wb.add_format({**base, "font_color": "#62696F"}),
        "kpi_value": wb.add_format({**base, "font_size": 20, "bold": True}),
    }
    for sev in compliance.SEVERITIES:
        f[f"sev_{sev}"] = wb.add_format({
            **base, "bold": True, "align": "center", "border": 1,
            "border_color": "#E3E6EA",
            "bg_color": SEVERITY_FILL[sev], "font_color": SEVERITY_INK[sev],
        })
    return f


def _naive(value):
    """엑셀에는 시간대가 없다. UTC 기준 값으로 적고 헤더에 UTC 라고 밝힌다.

    시간대를 단 채로 넘기면 xlsxwriter 가 거부한다. 로컬 시각으로 바꾸면
    서버 시간대에 따라 보고서 값이 달라져서, 같은 점검을 두 사람이 뽑으면
    다른 문서가 나온다.
    """
    if isinstance(value, datetime) and value.tzinfo is not None:
        return value.astimezone(timezone.utc).replace(tzinfo=None)
    return value


def _autofit(ws, widths):
    for col, width in enumerate(widths):
        ws.set_column(col, col, width)


def _table_head(ws, row, headers, f):
    for col, name in enumerate(headers):
        ws.write(row, col, name, f["head"])
    ws.freeze_panes(row + 1, 0)
    ws.autofilter(row, 0, row, len(headers) - 1)


def build(reports, generated_at=None):
    """엑셀 한 권을 만든다.

    reports: 계정+리전 하나당 dict 하나. 여러 개를 받는 이유는,
             고객사 보고는 계정 하나로 끝나지만 월간 내부 보고는
             전체를 한 장에 놓고 보기 때문이다.
        {
          "account_id", "region", "customer", "snapshot_id", "collected_at",
          "violations", "summary", "resources", "points", "repeats", "since",
        }
    """
    try:
        import xlsxwriter
    except ImportError as e:
        raise ExcelNotAvailable(
            "xlsxwriter 가 설치되어 있지 않습니다. "
            "pip install -r requirements.txt 를 실행하세요."
        ) from e

    generated_at = generated_at or datetime.now(timezone.utc)

    buf = BytesIO()
    wb = xlsxwriter.Workbook(buf, {"in_memory": True, "default_date_format": "yyyy-mm-dd hh:mm"})
    f = _formats(wb)

    _sheet_summary(wb, f, reports, generated_at)
    _sheet_violations(wb, f, reports)
    _sheet_timeline(wb, f, reports)
    _sheet_recurring(wb, f, reports)
    _sheet_exceptions(wb, f, reports)
    _sheet_checks(wb, f)

    wb.close()
    buf.seek(0)
    return buf


# ----------------------------------------------------------------------
# 시트
# ----------------------------------------------------------------------

def _sheet_summary(wb, f, reports, generated_at):
    """맨 앞 장. 열었을 때 숫자부터 보이게 한다."""
    ws = wb.add_worksheet("요약")
    _autofit(ws, [16, 18, 16, 12, 12, 12, 12, 12, 14])

    ws.write(0, 0, "컴플라이언스 점검 결과", f["title"])
    ws.write(1, 0, f"{_naive(generated_at):%Y-%m-%d %H:%M} UTC 생성", f["sub"])
    ws.write(2, 0,
             "AWS Config 가 아니라 수집해 둔 리소스 스냅샷을 기준으로 점검한 결과입니다. "
             "수집하지 않은 항목은 점검 대상에 들어 있지 않습니다.", f["sub"])

    # 전체 합계를 위에 둔다. 계정이 여러 개일 때 맨 위 줄만 보면 되게.
    totals = {s: 0 for s in compliance.SEVERITIES}
    excused = 0
    for r in reports:
        for sev in compliance.SEVERITIES:
            totals[sev] += r["summary"]["by_severity"][sev]
        excused += r["summary"]["excused"]

    row = 4
    cards = [("위반 합계", sum(totals.values()))]
    cards += [(sev, totals[sev]) for sev in compliance.SEVERITIES]
    cards += [("예외 처리됨", excused), ("대상 계정", len(reports))]
    for col, (label, value) in enumerate(cards):
        ws.write(row, col, label, f["kpi_label"])
        ws.write(row + 1, col, value, f["kpi_value"])

    row += 4
    headers = ["고객사", "계정", "리전", "스냅샷", "수집 시각(UTC)",
               "리소스", "위반", "critical", "예외"]
    _table_head(ws, row, headers, f)

    for i, r in enumerate(reports, start=row + 1):
        ws.write(i, 0, r.get("customer") or "—", f["cell"])
        ws.write(i, 1, r["account_id"], f["mono"])
        ws.write(i, 2, r["region"], f["cell"])
        ws.write(i, 3, f"#{r['snapshot_id']}", f["cell"])
        ws.write_datetime(i, 4, _naive(r["collected_at"]), f["date"])
        ws.write_number(i, 5, r["resources"], f["num"])
        ws.write_number(i, 6, r["summary"]["total"], f["num"])
        ws.write_number(i, 7, r["summary"]["by_severity"]["critical"], f["num"])
        ws.write_number(i, 8, r["summary"]["excused"], f["num"])


def _sheet_violations(wb, f, reports):
    """보고서의 본문. 담당자가 여기에 처리 여부를 적어 넣는다."""
    ws = wb.add_worksheet("위반 상세")
    _autofit(ws, [14, 14, 10, 26, 30, 40, 17, 10, 30, 16, 16])

    headers = [
        "고객사", "계정", "심각도", "점검 항목", "리소스", "내용",
        "언제부터(UTC)", "예외", "예외 사유", "담당자", "조치 결과",
    ]
    _table_head(ws, 0, headers, f)

    # '담당자' 와 '조치 결과' 는 빈 칸이다. 받은 사람이 채우는 자리라서
    # 우리가 채울 값이 없다. 열이 없으면 옆에 새 열을 만들어 쓰게 되고,
    # 그러면 다음 달 파일과 모양이 달라진다.
    #
    # 계정 순서가 아니라 심각도 순으로 놓는다. 계정별로 묶으면 두 번째
    # 계정의 critical 이 첫 번째 계정의 low 보다 아래로 내려간다.
    # 보고서를 여는 사람이 맨 위에서 보고 싶은 건 가장 급한 것이다.
    rows = [
        (r, v) for r in reports for v in r["violations"]
    ]
    rows.sort(key=lambda rv: (
        compliance.SEVERITY_ORDER[rv[1]["severity"]],
        rv[0].get("customer") or "",
        rv[0]["account_id"],
        rv[1]["check_id"],
        rv[1]["resource_id"],
    ))

    row = 1
    for r, v in rows:
        when = r["since"].get((v["check_id"], v["resource_id"]))
        ws.write(row, 0, r.get("customer") or "—", f["cell"])
        ws.write(row, 1, r["account_id"], f["mono"])
        ws.write(row, 2, v["severity"], f[f"sev_{v['severity']}"])
        ws.write(row, 3, v["title"], f["cell"])
        ws.write(row, 4, v["resource_id"], f["mono"])
        ws.write(row, 5, v["detail"], f["cell"])
        if when:
            ws.write_datetime(row, 6, _naive(when), f["date"])
        else:
            ws.write(row, 6, "시점 미상", f["cell"])
        ws.write(row, 7, "예외" if v["excused"] else "", f["cell"])
        ws.write(row, 8, v.get("excuse_reason", ""), f["cell"])
        ws.write(row, 9, "", f["cell"])
        ws.write(row, 10, "", f["cell"])
        row += 1

    if row == 1:
        ws.write(1, 0, "위반이 없습니다.", f["cell"])


def _sheet_timeline(wb, f, reports):
    """시간축. 스냅샷 기준 점검이 Config 와 갈리는 부분이라 따로 둔다."""
    ws = wb.add_worksheet("시간축")
    _autofit(ws, [14, 14, 12, 17, 10, 10, 12, 12, 44])

    headers = ["고객사", "계정", "리전", "수집 시각(UTC)", "리소스",
               "위반", "새로 생김", "해소됨", "변화 내역"]
    _table_head(ws, 0, headers, f)

    row = 1
    for r in reports:
        # 화면과 같은 최신순.
        for p in reversed(r["points"]):
            changes = []
            if p.get("first"):
                changes.append("가장 오래된 스냅샷 (비교 대상 없음)")
            else:
                changes += [f"+ {k[1]} ({k[0]})" for k in p["opened"]]
                changes += [f"- {k[1]} ({k[0]})" for k in p["closed"]]

            ws.write(row, 0, r.get("customer") or "—", f["cell"])
            ws.write(row, 1, r["account_id"], f["mono"])
            ws.write(row, 2, r["region"], f["cell"])
            ws.write_datetime(row, 3, _naive(p["collected_at"]), f["date"])
            ws.write_number(row, 4, p["resources"], f["num"])
            ws.write_number(row, 5, p["summary"]["total"], f["num"])
            ws.write_number(row, 6, 0 if p.get("first") else len(p["opened"]), f["num"])
            ws.write_number(row, 7, 0 if p.get("first") else len(p["closed"]), f["num"])
            ws.write(row, 8, "\n".join(changes) or "변화 없음", f["cell"])
            row += 1



def _sheet_recurring(wb, f, reports):
    """재발. 시간축 시트에 이어 붙이지 않고 따로 뗀다.

    자동 필터가 걸린 시트 아래에 다른 표를 두면, 위쪽 표에 필터를 거는
    순간 아래 표가 통째로 숨는다. 받은 사람이 필터를 걸어보는 건
    당연한 동작이라 그때 사라지면 안 된다.
    """
    ws = wb.add_worksheet("재발")
    _autofit(ws, [14, 14, 10, 30, 26, 12])

    ws.write(0, 0, "고쳤다가 다시 열린 항목", f["title"])
    ws.write(1, 0,
             "임시 조치였거나 무언가가 설정을 되돌리고 있다는 뜻입니다. "
             "현재 상태만 보는 도구로는 처음 열린 것과 구분되지 않습니다.", f["sub"])

    _table_head(ws, 3, ["고객사", "계정", "심각도", "점검 항목", "리소스", "열린 횟수"], f)

    row = 4
    for r in reports:
        for x in r["repeats"]:
            ws.write(row, 0, r.get("customer") or "—", f["cell"])
            ws.write(row, 1, r["account_id"], f["mono"])
            ws.write(row, 2, x["severity"], f[f"sev_{x['severity']}"])
            ws.write(row, 3, x["title"], f["cell"])
            ws.write(row, 4, x["resource_id"], f["mono"])
            ws.write_number(row, 5, x["times"], f["num"])
            row += 1
    if row == 4:
        ws.write(row, 0, "재발한 항목이 없습니다.", f["cell"])


def _sheet_exceptions(wb, f, reports):
    """승인된 예외. 위반을 목록에서 뺀 근거라서 보고서에 함께 나가야 한다."""
    ws = wb.add_worksheet("예외")
    _autofit(ws, [14, 14, 26, 30, 40, 14, 14])

    headers = ["고객사", "계정", "점검 항목", "리소스", "사유", "승인자", "만료(UTC)"]
    _table_head(ws, 0, headers, f)

    row = 1
    for r in reports:
        for e in r.get("exceptions", []):
            ws.write(row, 0, r.get("customer") or "—", f["cell"])
            ws.write(row, 1, e["account_id"], f["mono"])
            ws.write(row, 2, e["check_id"], f["cell"])
            ws.write(row, 3, e["resource_id"] or "계정 전체", f["mono"])
            ws.write(row, 4, e["reason"], f["cell"])
            ws.write(row, 5, e["approved_by"] or "—", f["cell"])
            ws.write_datetime(row, 6, _naive(e["expires_at"]), f["date"])
            row += 1

    if row == 1:
        ws.write(1, 0, "등록된 예외가 없습니다.", f["cell"])


def _sheet_checks(wb, f):
    """무엇을 봤는지. 이게 없으면 '위반 0건' 이 무슨 뜻인지 알 수 없다."""
    ws = wb.add_worksheet("점검 항목")
    _autofit(ws, [10, 30, 22, 60])

    ws.write(0, 0, "이 보고서가 확인한 항목", f["title"])
    ws.write(1, 0,
             "여기 없는 것은 점검하지 않았습니다. IAM 사용자 MFA, root 액세스 키, "
             "CloudTrail 활성화 등은 현재 수집 대상이 아닙니다.", f["sub"])

    _table_head(ws, 3, ["심각도", "항목", "근거", "왜 위반인가"], f)
    for i, c in enumerate(compliance.CHECKS, start=4):
        ws.write(i, 0, c["severity"], f[f"sev_{c['severity']}"])
        ws.write(i, 1, c["title"], f["cell"])
        ws.write(i, 2, c["standard"], f["cell"])
        ws.write(i, 3, c["why"], f["cell"])
