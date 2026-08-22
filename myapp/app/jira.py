# app/jira.py
# 에스컬레이션 결과를 Jira 이슈로 넘긴다.
#
# 이 앱은 티켓 시스템을 만들지 않는다. 작업 기록에 티켓 번호 칸만 둔 것도,
# 여기서 이슈를 '만들고 끝' 인 것도 같은 이유다 - 상태 관리와 담당자 배정,
# 코멘트 스레드는 Jira 가 이미 훨씬 잘 한다. 여기서 흉내내면 두 곳에
# 같은 내용을 적게 된다.
#
# 전송은 slack.py 와 같은 이유로 표준 라이브러리만 쓴다.

import base64
import json
import urllib.error
import urllib.request

from flask import current_app

TIMEOUT_SECONDS = 15


class JiraError(Exception):
    """Jira 로 넘기지 못했을 때."""


class JiraNotConfigured(JiraError):
    """설정이 없을 때. '실패' 와 구분한다 - 설정 안 한 것은 오류가 아니다."""


def _config():
    cfg = current_app.config
    return {
        "base_url": (cfg.get("JIRA_BASE_URL") or "").rstrip("/"),
        "email": cfg.get("JIRA_EMAIL") or "",
        "token": cfg.get("JIRA_API_TOKEN") or "",
        "project": cfg.get("JIRA_PROJECT_KEY") or "",
        "issue_type": cfg.get("JIRA_ISSUE_TYPE") or "Task",
    }


def is_configured():
    c = _config()
    return all([c["base_url"], c["email"], c["token"], c["project"]])


def _missing():
    c = _config()
    names = {
        "base_url": "JIRA_BASE_URL", "email": "JIRA_EMAIL",
        "token": "JIRA_API_TOKEN", "project": "JIRA_PROJECT_KEY",
    }
    return [names[k] for k in names if not c[k]]


def _adf(text):
    """평문을 Atlassian Document Format 으로 감싼다.

    Jira Cloud REST v3 는 description 을 평문으로 받지 않는다. 문단 하나에
    줄바꿈을 hardBreak 으로 넣는 최소 형태면 충분하다.
    """
    content = []
    for line in text.split("\n"):
        nodes = []
        if line:
            nodes.append({"type": "text", "text": line})
        content.append({"type": "paragraph", "content": nodes})
    return {"type": "doc", "version": 1, "content": content}


def create_issue(summary, description, labels=None):
    """이슈를 만들고 키(PROJ-123)를 돌려준다."""
    c = _config()
    if not is_configured():
        raise JiraNotConfigured(
            "Jira 설정이 없습니다: " + ", ".join(_missing())
        )

    payload = {
        "fields": {
            "project": {"key": c["project"]},
            "summary": summary[:250],
            "description": _adf(description),
            "issuetype": {"name": c["issue_type"]},
        }
    }
    if labels:
        payload["fields"]["labels"] = labels

    # Jira Cloud 는 이메일 + API 토큰의 기본 인증을 쓴다.
    credential = base64.b64encode(
        f"{c['email']}:{c['token']}".encode("utf-8")
    ).decode("ascii")

    request = urllib.request.Request(
        f"{c['base_url']}/rest/api/3/issue",
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers={
            "Content-Type": "application/json",
            "Accept": "application/json",
            "Authorization": f"Basic {credential}",
        },
        method="POST",
    )

    try:
        with urllib.request.urlopen(request, timeout=TIMEOUT_SECONDS) as response:
            body = json.loads(response.read().decode("utf-8", "replace"))
    except urllib.error.HTTPError as e:
        detail = e.read().decode("utf-8", "replace").strip()
        # Jira 는 실패 이유를 JSON 의 errorMessages / errors 에 담아준다.
        try:
            parsed = json.loads(detail)
            messages = parsed.get("errorMessages") or []
            messages += [f"{k}: {v}" for k, v in (parsed.get("errors") or {}).items()]
            detail = "; ".join(messages) or detail
        except ValueError:
            pass
        raise JiraError(f"Jira 가 거절했습니다 (HTTP {e.code}): {detail[:400]}")
    except urllib.error.URLError as e:
        raise JiraError(f"Jira 에 연결하지 못했습니다: {e.reason}")
    except Exception as e:
        raise JiraError(f"Jira 전송에 실패했습니다: {e}")

    key = body.get("key")
    if not key:
        raise JiraError(f"Jira 응답에 이슈 키가 없습니다: {str(body)[:200]}")
    return key


def issue_url(key):
    """이슈로 가는 링크. 설정이 없으면 빈 문자열."""
    base = _config()["base_url"]
    return f"{base}/browse/{key}" if base and key else ""
