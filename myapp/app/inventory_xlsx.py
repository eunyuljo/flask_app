# app/inventory_xlsx.py
# 리소스 목록을 엑셀로. 컴플라이언스 엑셀과 서식을 공유한다.
#
# 두 벌로 두면 같은 회사가 만든 문서로 보이지 않게 된다 -
# 컴플라이언스 엑셀에서 배운 것과 같은 이유다.

from datetime import datetime, timezone

from app.compliance_xlsx import (
    ExcelNotAvailable, _autofit, _formats, _naive, _table_head,
)

# 종류마다 사람들이 실제로 궁금해하는 속성이 다르다.
# 전부 한 칸에 JSON 으로 넣으면 엑셀에서 걸러볼 수가 없다.
COLUMNS = {
    "ec2:instance": [
        ("타입", "instance_type"), ("상태", "state"),
        ("퍼블릭 IP", "public_ip"), ("IMDS", "imds"),
        ("역할", "iam_profile"), ("보안그룹", "security_groups"),
    ],
    "ec2:security_group": [("VPC", "vpc"), ("인바운드", "ingress")],
    "s3:bucket": [
        ("퍼블릭 차단", "public_access_blocked"),
        ("버저닝", "versioning"), ("암호화", "encryption"),
    ],
    "rds:instance": [
        ("엔진", "engine"), ("클래스", "class"),
        ("퍼블릭", "public"), ("다중 AZ", "multi_az"),
    ],
    "iam:role": [("정책", "policies")],
}

# 어느 종류에나 붙는 열.
COMMON = [("Name", ("tags", "Name")), ("Env", ("tags", "Env"))]


def _value(attrs, key):
    """속성 하나를 사람이 읽을 문자열로."""
    if isinstance(key, tuple):
        cur = attrs
        for part in key:
            cur = (cur or {}).get(part) if isinstance(cur, dict) else None
        value = cur
    else:
        value = attrs.get(key)

    if value is None:
        # 키가 아예 없는 것과 값이 없는 것은 다르다.
        # 앞은 '수집 안 함', 뒤는 '없음' 이다.
        known = (key[0] if isinstance(key, tuple) else key) in attrs
        return "" if known else "—"
    if isinstance(value, bool):
        return "예" if value else "아니오"
    if isinstance(value, list):
        return ", ".join(str(v) for v in value)
    if isinstance(value, dict):
        return ", ".join(f"{k}={v}" for k, v in value.items())
    return str(value)


def build(result, filters=None):
    """종류별로 시트를 나눈 리소스 목록.

    한 시트에 다 넣으면 종류마다 의미 있는 열이 달라서 빈 칸이 많아진다.
    엑셀에서 걸러 보려면 열이 종류에 맞아야 한다.
    """
    try:
        import xlsxwriter
    except ImportError as e:
        raise ExcelNotAvailable(
            "xlsxwriter 가 설치되어 있지 않습니다. "
            "pip install -r requirements.txt 를 실행하세요."
        ) from e

    from io import BytesIO

    buf = BytesIO()
    wb = xlsxwriter.Workbook(buf, {"in_memory": True,
                                   "default_date_format": "yyyy-mm-dd hh:mm"})
    f = _formats(wb)

    _sheet_summary(wb, f, result, filters or {})
    by_type = {}
    for item in result["items"]:
        by_type.setdefault(item["resource_type"], []).append(item)

    for resource_type in sorted(by_type):
        _sheet_type(wb, f, resource_type, by_type[resource_type])

    if not by_type:
        ws = wb.add_worksheet("결과 없음")
        _autofit(ws, [60])
        ws.write(0, 0, "조건에 맞는 리소스가 없습니다.", f["cell"])

    wb.close()
    buf.seek(0)
    return buf


def _sheet_summary(wb, f, result, filters):
    ws = wb.add_worksheet("요약")
    _autofit(ws, [18, 18, 16, 20, 14])

    ws.write(0, 0, "리소스 목록", f["title"])
    ws.write(1, 0,
             f"{_naive(datetime.now(timezone.utc)):%Y-%m-%d %H:%M} UTC 생성. "
             "계정+리전마다 가장 최근 스냅샷 하나를 기준으로 합니다.", f["sub"])

    active = [f"{k}={v}" for k, v in filters.items() if v]
    ws.write(2, 0, "필터: " + (", ".join(active) if active else "없음"), f["sub"])
    if result["truncated"]:
        ws.write(3, 0,
                 f"전체 {result['total']}건 중 일부만 실었습니다. "
                 "필터를 좁혀서 다시 받으세요.", f["sub"])

    row = 5
    _table_head(ws, row, ["계정", "리전", "수집 시각(UTC)", "리소스", ""], f)
    for i, s in enumerate(result["snapshots"], start=row + 1):
        n = len([r for r in result["items"]
                 if r["account_id"] == s["account_id"] and r["region"] == s["region"]])
        ws.write(i, 0, s["account_id"], f["mono"])
        ws.write(i, 1, s["region"], f["cell"])
        ws.write_datetime(i, 2, _naive(s["collected_at"]), f["date"])
        ws.write_number(i, 3, n, f["num"])
        ws.write(i, 4, "", f["cell"])


def _sheet_type(wb, f, resource_type, items):
    # 시트 이름에는 : 를 못 쓴다(엑셀 제한). 31자 상한도 있다.
    name = resource_type.replace(":", " ")[:31]
    ws = wb.add_worksheet(name)

    extra = COLUMNS.get(resource_type, [])
    headers = ["계정", "리전", "리소스"] + [h for h, _ in COMMON] + [h for h, _ in extra]
    widths = [16, 16, 30] + [16] * len(COMMON) + [22] * len(extra)
    _autofit(ws, widths)
    _table_head(ws, 0, headers, f)

    for i, item in enumerate(items, start=1):
        attrs = item["attributes"] or {}
        ws.write(i, 0, item["account_id"], f["mono"])
        ws.write(i, 1, item["region"], f["cell"])
        ws.write(i, 2, item["resource_id"], f["mono"])
        col = 3
        for _label, key in COMMON + extra:
            ws.write(i, col, _value(attrs, key), f["cell"])
            col += 1
