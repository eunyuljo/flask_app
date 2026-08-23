# app/customer_brief.py
# 이 고객사에 대해 우리가 아는 것을 한 장으로.
#
# ── 왜 필요한가 ────────────────────────────────────────────────────
# 담당자가 바뀔 때 넘겨야 할 것이 화면 여섯 개에 흩어져 있다.
#   고객사 현황 · 연락처 · 구성 표준 · 정기 점검 · 계정 접속 · 런북 · 장애
# 그래서 실제 인수인계 때는 사람이 화면을 돌며 다시 정리한다. 자료는
# 이미 다 있는데 모으는 일만 사람이 한다.
#
# ── 왜 저장하지 않는가 ─────────────────────────────────────────────
# 인수인계 문서를 표로 저장하면, 고객사 정보가 바뀐 뒤에도 옛 문서가
# 남아 진실이 둘이 된다. 부를 때마다 지금 상태로 만든다.
#
# ── 무엇을 넣지 않는가 ─────────────────────────────────────────────
# ExternalId 같은 비밀 값은 넣지 않는다. 이 문서는 메신저로 오가기 쉽고,
# 한 번 나가면 회수할 수 없다. 있는지 없는지만 말한다.

from datetime import datetime, timezone


class BriefError(Exception):
    """인수인계 자료를 모으지 못했을 때."""


def collect(customer):
    """이 고객사에 대해 아는 것을 모은다.

    한 조각을 못 읽어도 나머지는 담는다. 인수인계 문서는 완벽하지 않아도
    쓸모가 있고, 오히려 '무엇을 못 읽었는지' 가 인수인계에서 중요하다.
    그래서 실패한 조각은 notes 에 남긴다.
    """
    data = {
        "customer": customer,
        "generated_at": datetime.now(timezone.utc),
        "accounts": [],
        "contacts": [],
        "standards": [],
        "routines": [],
        "runbooks": [],
        "incidents": [],
        "access": [],
        "deliveries": [],
        "readiness": None,
        "notes": [],
    }

    def part(label, fn, key):
        try:
            data[key] = fn()
        except Exception as e:                   # noqa: BLE001
            data["notes"].append(f"{label}을(를) 읽지 못했습니다: {e}")

    from app import access, contacts, delivery, incident, readiness, routines
    from app import standards
    from app.accounts import list_accounts

    part("계정", lambda: [a for a in list_accounts(enabled_only=False)
                          if a["customer"] == customer], "accounts")
    part("연락처", lambda: contacts.listing(customer), "contacts")
    part("구성 표준", lambda: standards.listing(customer), "standards")
    part("정기 점검", lambda: routines.listing(customer), "routines")
    part("발송 기록", lambda: delivery.recent(customer, limit=10), "deliveries")

    # 계정 접속 위생. 계정 목록을 이미 읽었으니 그것으로 판정만 한다.
    if data["accounts"]:
        part("계정 접속 상태",
             lambda: access.overview(data["accounts"]), "access")

    part("장애 이력", lambda: [i for i in incident.recent(200)
                              if i["customer"] == customer][:10], "incidents")

    # 런북은 이 고객사 전용만. 공통 런북까지 넣으면 문서가 통째로 런북
    # 목록이 되고, 정작 이 고객사만의 특이사항이 묻힌다.
    part("런북", lambda: _customer_runbooks(customer), "runbooks")

    try:
        results, _ = readiness.evaluate(customer)
        data["readiness"] = {
            "results": [r for r in results if r["status"] != "ok"],
            "summary": readiness.summarize(results),
        }
    except Exception as e:                       # noqa: BLE001
        data["notes"].append(f"준비도를 읽지 못했습니다: {e}")

    return data


def _customer_runbooks(customer):
    from app import runbook

    return [r for r in runbook.recent(200) if r["customer"] == customer]


def to_markdown(data):
    """인수인계 문서. 메신저에 붙여넣거나 파일로 넘긴다.

    Markdown 으로 내는 이유: 이 문서를 받는 사람이 이 앱을 쓰지 않을 수도
    있고, 위키나 티켓에 그대로 붙일 수 있어야 한다.
    """
    L = []
    a = L.append

    a(f"# {data['customer']} 인수인계")
    a("")
    a(f"작성 {data['generated_at']:%Y-%m-%d %H:%M} UTC")
    a("")
    a("이 문서는 저장되지 않습니다. 볼 때마다 지금 상태로 다시 만들어집니다.")

    if data["notes"]:
        a("")
        a("> **읽지 못한 것이 있습니다.** 인수인계에서는 이것이 오히려 중요합니다.")
        for note in data["notes"]:
            a(f"> - {note}")

    # ---- 지금 상태 ----
    ready = data.get("readiness")
    if ready:
        a("")
        a("## 지금 상태")
        a("")
        if ready["summary"]["ready"]:
            a("필수 항목은 모두 채워져 있습니다.")
        else:
            a(f"**필수 항목 {ready['summary']['blocking']}건이 비어 있습니다.**")
        for item in ready["results"]:
            a(f"- [{item['status']}] {item['title']} — {item['detail']}")

    # ---- 연락처 ----
    a("")
    a("## 연락처")
    a("")
    if not data["contacts"]:
        a("등록된 연락처가 없습니다. **인수인계 전에 채워야 합니다** — "
          "새벽에 장애가 나면 누구에게 말할지 알 수 없습니다.")
    else:
        from app.contacts import KINDS

        a("| 역할 | 이름 | 연락처 | 메모 |")
        a("|---|---|---|---|")
        for c in data["contacts"]:
            reach = " / ".join(x for x in (c["email"], c["phone"]) if x)
            a(f"| {KINDS.get(c['kind'], c['kind'])} | {c['name']} | "
              f"{reach} | {c['note']} |")

    # ---- 계정 ----
    a("")
    a("## 계정")
    a("")
    if not data["accounts"]:
        a("등록된 계정이 없습니다.")
    else:
        a("| 계정 | 별칭 | 리전 | 역할 | ExternalId |")
        a("|---|---|---|---|---|")
        for acc in data["accounts"]:
            a(f"| `{acc['account_id']}` | {acc.get('alias') or '—'} | "
              f"{', '.join(acc.get('regions') or []) or '없음'} | "
              # 역할 ARN 은 비밀이 아니지만, ExternalId 는 비밀이다.
              # 있는지 없는지만 적는다.
              f"{'있음' if acc.get('role_arn') else '없음(데모)'} | "
              f"{'설정됨' if acc.get('external_id') else '**없음**'} |")

    trouble = [r for r in data["access"] if r["worst"] == "danger"]
    if trouble:
        a("")
        a("**계정 접속에 위험 항목이 있습니다.**")
        for row in trouble:
            for finding in row["findings"]:
                if finding["level"] == "danger":
                    a(f"- {row['account']['account_id']}: {finding['title']}")

    # ---- 합의한 것 ----
    a("")
    a("## 합의한 구성 표준")
    a("")
    if not data["standards"]:
        a("정해둔 규칙이 없습니다.")
    else:
        from app.standards import RULES

        for item in data["standards"]:
            label = RULES.get(item["rule"], {}).get("label", item["rule"])
            a(f"- **{label}**: `{item['value']}`"
              + (f" — {item['note']}" if item["note"] else ""))

    # ---- 약속한 것 ----
    a("")
    a("## 정기 점검")
    a("")
    if not data["routines"]:
        a("등록된 주기 업무가 없습니다.")
    else:
        from app.routines import STATE_LABEL

        a("| 항목 | 주기 | 상태 | 마지막 |")
        a("|---|---|---|---|")
        for item in data["routines"]:
            last = (item["last_done_at"].strftime("%Y-%m-%d")
                    if item["last_done_at"] else "—")
            a(f"| {item['name']} | {item['interval_days']}일 | "
              f"{STATE_LABEL.get(item['state'], item['state'])} | {last} |")

    # ---- 이 고객사만의 절차 ----
    a("")
    a("## 이 고객사 전용 런북")
    a("")
    if not data["runbooks"]:
        a("전용 절차가 없습니다. 공통 런북만 적용됩니다.")
    else:
        for item in data["runbooks"]:
            a(f"### {item['title']}")
            a("")
            a(item["body"])
            a("")

    # ---- 지난 일 ----
    a("")
    a("## 최근 장애")
    a("")
    if not data["incidents"]:
        a("기록된 장애가 없습니다.")
    else:
        for item in data["incidents"]:
            a(f"- {item['started_at']:%Y-%m-%d} [{item['severity']}] "
              f"{item['title']} ({item['status']})")

    if data["deliveries"]:
        a("")
        a("## 최근 발송")
        a("")
        for item in data["deliveries"]:
            a(f"- {item['sent_at']:%Y-%m-%d} {item['title'] or item['ref']} "
              f"→ {item['recipients']}")

    a("")
    a("---")
    a("")
    a("비밀 값(ExternalId, 자격증명)은 이 문서에 들어 있지 않습니다. "
      "필요하면 도구에서 직접 확인하세요.")
    return "\n".join(L)
