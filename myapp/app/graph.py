# app/graph.py
# 이 리소스를 건드리면 무엇이 딸려 오나 (영향 범위).
#
# ── 왜 '의존 관계' 라고 부르지 않나 ────────────────────────────────
# 지금 수집기가 훑는 것은 EC2 인스턴스·보안그룹·S3 버킷 셋뿐이다.
# 그걸로 '인프라 의존 관계도' 를 그렸다고 하면, 화면에 안 보이는 것을
# 없는 것으로 읽게 된다. ALB -> 대상그룹 -> 인스턴스도, RDS 도,
# Route53 도 여기에 없다.
#
# 그래서 이름과 화면 둘 다 '영향 범위' 로 좁혔다. 물음도 좁다.
# "이 보안그룹을 고치면 어떤 인스턴스가 영향을 받나" - 작업 승인 직전에
# 실제로 하는 질문이고, 지금 자료로 정확히 답할 수 있는 질문이다.
#
# 볼 수 없는 것은 BLIND 에 적어 화면에 함께 띄운다. 목록이 짧은 것이
# '의존이 없다' 로 읽히면 안 된다.

# 지금 자료로는 볼 수 없는 관계. 화면에 그대로 띄운다.
BLIND = [
    "로드밸런서 → 대상 그룹 → 인스턴스",
    "RDS·ElastiCache 등 데이터 계층",
    "Route53 레코드 → 엔드포인트",
    "IAM 역할이 실제로 무엇에 접근하는지",
    "S3 버킷을 누가 읽고 쓰는지 (버킷은 지금 섬으로만 보인다)",
    "다른 계정·리전에 걸친 연결",
]

# 간선 종류. (from, to) 방향은 '무엇이 무엇에 기대는가' 다.
KINDS = {
    "uses_sg": "보안그룹을 쓴다",
    "allows_sg": "이 보안그룹에서 오는 것을 허용한다",
    "in_vpc": "같은 VPC 에 있다",
}


def build(snap):
    """스냅샷 한 장에서 간선을 뽑는다.

    돌려주는 것: [{"from":..., "to":..., "kind":..., "label":...}, ...]

    없는 리소스를 가리키는 간선은 버린다. 보안그룹이 다른 계정 것이거나
    이번 수집에서 빠졌을 수 있는데, 그런 간선을 남기면 화면에 이름만
    있고 실체가 없는 점이 생긴다.
    """
    edges = []

    for inst in snap.of_type("ec2:instance"):
        for sg in inst["attributes"].get("security_groups") or []:
            if snap.get(sg) is None:
                continue
            edges.append({"from": inst["resource_id"], "to": sg,
                          "kind": "uses_sg", "label": KINDS["uses_sg"]})

    for sg in snap.of_type("ec2:security_group"):
        for source in sg["attributes"].get("ingress_groups") or []:
            if snap.get(source) is None:
                continue
            # 방향에 주의한다. db-sg 가 web-sg 를 허용하면, 고쳐서 문제가
            # 생기는 쪽은 db-sg 다. 그래서 db-sg 가 web-sg 에 기댄다.
            edges.append({"from": sg["resource_id"], "to": source,
                          "kind": "allows_sg", "label": KINDS["allows_sg"]})

    return edges


def _name(snap, resource_id):
    item = snap.get(resource_id)
    if item is None:
        return resource_id
    attrs = item["attributes"]
    return (attrs.get("tags") or {}).get("Name") or attrs.get("name") or resource_id


def impact(snap, resource_id, depth=2):
    """이 리소스를 건드리면 영향을 받는 것들.

    간선의 방향을 거꾸로 따라간다. A 가 B 에 기대고 있으면, B 를 고쳤을 때
    흔들리는 것은 A 다.

    depth 를 두는 이유: 인스턴스 → 보안그룹 → 그 보안그룹을 허용하는
    보안그룹 → 그 보안그룹을 쓰는 인스턴스까지가 실제로 궁금한 범위다.
    끝까지 따라가면 VPC 하나가 통째로 나와서 아무 도움이 안 된다.
    """
    edges = build(snap)
    if snap.get(resource_id) is None:
        return {"target": resource_id, "found": False, "levels": []}

    incoming = {}
    for edge in edges:
        incoming.setdefault(edge["to"], []).append(edge)

    seen = {resource_id}
    levels, frontier = [], [resource_id]
    for _ in range(max(1, depth)):
        step = []
        for node in frontier:
            for edge in incoming.get(node, []):
                if edge["from"] in seen:
                    continue
                seen.add(edge["from"])
                step.append({
                    "resource_id": edge["from"],
                    "name": _name(snap, edge["from"]),
                    "type": (snap.get(edge["from"]) or {}).get("resource_type", ""),
                    "via": node,
                    "via_name": _name(snap, node),
                    "label": edge["label"],
                })
        if not step:
            break
        levels.append(step)
        frontier = [s["resource_id"] for s in step]

    return {
        "target": resource_id,
        "target_name": _name(snap, resource_id),
        "target_type": snap.get(resource_id)["resource_type"],
        "found": True,
        "levels": levels,
        "total": sum(len(level) for level in levels),
    }


def nodes(snap):
    """고를 수 있는 리소스 목록. 간선이 하나라도 붙은 것만.

    아무 데도 안 붙은 리소스를 고르면 언제나 '영향 없음' 이 나오는데,
    그건 정말 영향이 없어서가 아니라 우리가 그 관계를 수집하지 않아서다.
    고를 수 없게 하는 편이 낫다.
    """
    edges = build(snap)
    touched = {e["from"] for e in edges} | {e["to"] for e in edges}
    out = [{"resource_id": r, "name": _name(snap, r),
            "type": snap.get(r)["resource_type"]}
           for r in sorted(touched) if snap.get(r)]
    out.sort(key=lambda n: (n["type"], n["name"]))
    return out


def islands(snap):
    """간선이 하나도 없는 리소스. '연결이 없다' 가 아니라 '못 봤다' 다."""
    edges = build(snap)
    touched = {e["from"] for e in edges} | {e["to"] for e in edges}
    return [{"resource_id": i["resource_id"], "type": i["resource_type"],
             "name": _name(snap, i["resource_id"])}
            for i in snap.items if i["resource_id"] not in touched]
