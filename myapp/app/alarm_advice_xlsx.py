# app/alarm_advice_xlsx.py
# 알람 권고 표를 엑셀로 만든다. 권고는 app/alarm_advice.py 가 내고,
# 여기서는 시트에 배치하는 일만 한다(compliance_xlsx.py 와 같은 구조).
#
# ── 왜 엑셀이 이 기능의 절반인가 ────────────────────────────────────
# 이 표를 읽을 사람은 이 앱을 쓰지 않는다. 실제 알람은 모니터링 서버에
# 있고, 거기 담당자가 이 목록을 받아 하나씩 설정한다. 화면으로만 두면
# 스크린샷을 찍어 메신저로 보내게 된다.
#
# 그래서 '처리 여부' 칸을 비워서 함께 낸다. 받는 쪽이 옆 칸에 적어가며
# 일하고, 다음 달 것과 나란히 놓고 본다.
#
# import 를 함수 안에서 한다. xlsxwriter 가 없다고 인프라 화면까지 죽으면
# 안 된다.

from datetime import datetime, timezone
from io import BytesIO

from app import alarm_advice


class ExcelNotAvailable(Exception):
    """xlsxwriter 가 없을 때."""


LEVEL_FILL = {
    "essential":   "#FDE7E7",
    "recommended": "#FEF6DF",
    "optional":    "#EEF1F4",
}
LEVEL_INK = {
    "essential":   "#9B1C1C",
    "recommended": "#854D0E",
    "optional":    "#4B5563",
}


def _formats(wb):
    base = {"font_name": "맑은 고딕", "font_size": 10, "valign": "top"}
    f = {
        "title": wb.add_format({**base, "font_size": 16, "bold": True}),
        "sub": wb.add_format({**base, "font_color": "#62696F", "text_wrap": True}),
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
        # 받는 쪽이 채울 칸. 테두리만 있고 비어 있다.
        "todo": wb.add_format({**base, "border": 1, "border_color": "#B9C0C8",
                               "bg_color": "#FCFDFE"}),
        "kpi_label": wb.add_format({**base, "font_color": "#62696F"}),
        "kpi_value": wb.add_format({**base, "font_size": 20, "bold": True}),
    }
    for level in alarm_advice.LEVELS:
        f[f"lv_{level}"] = wb.add_format({
            **base, "bold": True, "align": "center", "border": 1,
            "border_color": "#E3E6EA",
            "bg_color": LEVEL_FILL[level], "font_color": LEVEL_INK[level],
        })
    return f


def _head(ws, row, headers, f):
    for col, name in enumerate(headers):
        ws.write(row, col, name, f["head"])
    ws.freeze_panes(row + 1, 0)
    ws.autofilter(row, 0, row, len(headers) - 1)


def build(data, generated_at=None):
    """엑셀 한 권을 만든다.

    data: alarm_advice.for_customer() 가 돌려준 것.
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
    wb = xlsxwriter.Workbook(buf, {"in_memory": True})
    f = _formats(wb)

    _sheet_summary(wb, f, data, generated_at)
    _sheet_by_rule(wb, f, data)
    _sheet_by_resource(wb, f, data)

    wb.close()
    buf.seek(0)
    return buf.read()


def _sheet_summary(wb, f, data, generated_at):
    ws = wb.add_worksheet("요약")
    ws.set_column(0, 0, 22)
    ws.set_column(1, 4, 26)

    ws.write(0, 0, f"{data['customer']} 알람 권고", f["title"])
    ws.write(1, 0, f"{generated_at:%Y-%m-%d %H:%M} UTC 기준", f["sub"])

    # 이 문서가 무엇이 아닌지를 맨 위에 적는다. 받는 쪽이 '현황표' 로
    # 읽으면 이미 걸어둔 알람을 지웠다고 오해할 수 있다.
    ws.merge_range(
        3, 0, 5, 4,
        "이 표는 '무엇을 걸어야 하는가' 입니다. "
        "실제로 걸려 있는지는 확인하지 않았습니다 — 실제 알람은 모니터링 "
        "서버에 있고, 이 도구는 그 설정을 읽지 않습니다.\n"
        "따라서 여기 있는 항목이 이미 걸려 있을 수 있습니다. "
        "'처리 여부' 칸에 확인 결과를 적어 주세요.",
        f["sub"],
    )

    counts = data["counts"]
    labels = [("권고 대상 리소스", counts["resources"]),
              ("권고 항목 합계", counts["total"]),
              ("필수", counts["by_level"]["essential"]),
              ("권장", counts["by_level"]["recommended"]),
              ("선택", counts["by_level"]["optional"])]
    for col, (label, value) in enumerate(labels):
        ws.write(7, col, label, f["kpi_label"])
        ws.write(8, col, value, f["kpi_value"])

    row = 11
    ws.write(row, 0, "판정 근거 스냅샷", f["head"])
    ws.write(row, 1, "수집 시각(UTC)", f["head"])
    row += 1
    for s in data["snapshots"]:
        ws.write(row, 0, f"{s['account_id']} / {s['region']}", f["mono"])
        ws.write(row, 1,
                 s["collected_at"].astimezone(timezone.utc)
                 .replace(tzinfo=None).strftime("%Y-%m-%d %H:%M"), f["cell"])
        row += 1

    if data["missing_types"]:
        row += 1
        ws.merge_range(
            row, 0, row, 4,
            "이번 수집에서 빠진 리소스 종류: "
            + ", ".join(data["missing_types"])
            + " — 그 종류에 걸 알람은 이 표에 없습니다(권고가 없는 것이 "
              "아니라 보지 못한 것입니다).",
            f["sub"],
        )


def _sheet_by_rule(wb, f, data):
    """규칙별. 모니터링 담당은 이 단위로 일한다 -
    '크레딧 알람 걸 인스턴스 12대' 를 한 번에 처리한다."""
    ws = wb.add_worksheet("알람별")
    for col, width in enumerate([8, 26, 30, 22, 40, 30, 12, 20]):
        ws.set_column(col, col, width)

    headers = ["등급", "알람", "지표", "임계값 제안", "왜 필요한가",
               "추가로 필요한 것", "대상 수", "처리 여부"]
    _head(ws, 0, headers, f)

    row = 1
    for g in data["groups"]:
        ws.write(row, 0, alarm_advice.LEVELS[g["level"]], f[f"lv_{g['level']}"])
        ws.write(row, 1, g["label"], f["cell"])
        ws.write(row, 2, g["metric"], f["mono"])
        ws.write(row, 3, g["hint"], f["cell"])
        ws.write(row, 4, g["why"], f["cell"])
        ws.write(row, 5, g["needs"] or "—", f["cell"])
        ws.write(row, 6, len(g["targets"]), f["num"])
        ws.write_blank(row, 7, None, f["todo"])
        row += 1


def _sheet_by_resource(wb, f, data):
    """리소스별. 한 대씩 확인할 때 쓴다."""
    ws = wb.add_worksheet("리소스별")
    for col, width in enumerate([16, 14, 22, 20, 8, 26, 30, 20]):
        ws.set_column(col, col, width)

    headers = ["계정", "리전", "리소스", "이름", "등급", "알람", "지표",
               "처리 여부"]
    _head(ws, 0, headers, f)

    row = 1
    for item in data["rows"]:
        if not item["rules"]:
            # 알람 대상이 아닌 리소스도 한 줄 남긴다. 목록에서 빠지면
            # 빠뜨린 것인지 대상이 아닌 것인지 알 수 없다.
            ws.write(row, 0, item["account_id"], f["mono"])
            ws.write(row, 1, item["region"], f["cell"])
            ws.write(row, 2, item["resource_id"], f["mono"])
            ws.write(row, 3, item["name"], f["cell"])
            ws.write(row, 4, "—", f["cell"])
            ws.write(row, 5, item["note"] or "권고 항목 없음", f["cell"])
            ws.write(row, 6, "", f["cell"])
            ws.write_blank(row, 7, None, f["todo"])
            row += 1
            continue

        for rule in item["rules"]:
            ws.write(row, 0, item["account_id"], f["mono"])
            ws.write(row, 1, item["region"], f["cell"])
            ws.write(row, 2, item["resource_id"], f["mono"])
            ws.write(row, 3, item["name"], f["cell"])
            ws.write(row, 4, alarm_advice.LEVELS[rule["level"]],
                     f[f"lv_{rule['level']}"])
            ws.write(row, 5, rule["label"], f["cell"])
            ws.write(row, 6, rule["metric"], f["mono"])
            ws.write_blank(row, 7, None, f["todo"])
            row += 1
