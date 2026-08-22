# app/slack.py
# Slack Incoming Webhook 으로 메시지를 보낸다.
#
# 새 의존성을 만들지 않으려고 표준 라이브러리(urllib)만 쓴다. 보내는 건
# JSON 한 번의 POST 라 requests 를 끌어올 이유가 없다. Lambda 배포
# 패키지에도 영향이 없다.
#
# Incoming Webhook 을 고른 이유: URL 하나면 되고 OAuth 도 앱 설치도
# 필요 없다. 슬래시 커맨드나 봇 토큰은 공개 엔드포인트와 서명 검증이
# 따라오는데, 이 앱은 인증이 하드코딩 계정 하나라 그대로 열면 안 된다.

import json
import urllib.error
import urllib.request

from flask import current_app

# Slack 의 상한. 넘기면 400 을 돌려준다.
#   text          : 40,000자
#   블록 안의 text : 3,000자
MAX_TEXT = 39_000
TIMEOUT_SECONDS = 10


class SlackError(Exception):
    """Slack 으로 보내지 못했을 때."""


class SlackNotConfigured(SlackError):
    """웹훅 URL 이 없을 때. '실패' 와 구분한다 - 설정 안 한 것은 오류가 아니다."""


def webhook_url(purpose=None):
    """용도별 웹훅을 먼저 보고, 없으면 공통 웹훅을 쓴다.

    당직 인계는 #ops-daily, SLA 경고는 #ops-alert 처럼 채널을 나누고
    싶은 경우가 흔하다. 하나만 쓰겠다면 SLACK_WEBHOOK_URL 만 채우면 된다.
    """
    cfg = current_app.config
    if purpose:
        specific = cfg.get(f"SLACK_{purpose.upper()}_WEBHOOK", "")
        if specific:
            return specific
    return cfg.get("SLACK_WEBHOOK_URL", "")


def is_configured(purpose=None):
    return bool(webhook_url(purpose))


def post(text, purpose=None, blocks=None):
    """메시지를 보낸다.

    text 는 알림 미리보기와 폴백에 쓰이므로 blocks 를 줘도 함께 보낸다.
    """
    url = webhook_url(purpose)
    if not url:
        raise SlackNotConfigured(
            "Slack 웹훅 URL 이 설정되지 않았습니다 "
            "(SLACK_WEBHOOK_URL 또는 SLACK_<용도>_WEBHOOK)."
        )

    if len(text) > MAX_TEXT:
        # 잘렸다는 사실을 남긴다. 조용히 자르면 받는 쪽은 내용이 원래
        # 그만큼인 줄 안다.
        text = text[:MAX_TEXT] + "\n\n… (길어서 잘렸습니다. 화면에서 전체를 보세요)"

    payload = {"text": text}
    if blocks:
        payload["blocks"] = blocks

    request = urllib.request.Request(
        url,
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )

    try:
        with urllib.request.urlopen(request, timeout=TIMEOUT_SECONDS) as response:
            body = response.read().decode("utf-8", "replace").strip()
    except urllib.error.HTTPError as e:
        detail = e.read().decode("utf-8", "replace").strip()
        # Slack 은 실패 이유를 본문에 평문으로 준다(invalid_payload 등).
        raise SlackError(f"Slack 이 거절했습니다 (HTTP {e.code}): {detail}")
    except urllib.error.URLError as e:
        raise SlackError(f"Slack 에 연결하지 못했습니다: {e.reason}")
    except Exception as e:
        raise SlackError(f"Slack 전송에 실패했습니다: {e}")

    # 성공하면 본문이 정확히 "ok" 다.
    if body != "ok":
        raise SlackError(f"Slack 이 예상과 다른 응답을 했습니다: {body[:200]}")
    return True


def escape(text):
    """Slack mrkdwn 에서 특별한 뜻을 가지는 글자를 막는다.

    &, <, > 세 개만 이스케이프하면 된다(Slack 문서 기준).
    별표나 밑줄은 이스케이프하지 않는다 - 알람 메시지에 흔히 들어가는데
    전부 막으면 오히려 읽기 나빠진다.
    """
    return (str(text).replace("&", "&amp;")
                     .replace("<", "&lt;")
                     .replace(">", "&gt;"))
