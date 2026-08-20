# agent_core.py
# AI 에이전트의 '두뇌' 부분. Claude 에게 넘길 도구(tool)들을 정의하고, 대화 한 턴을 실행한다.
# 웹(HTTP)과 관련된 코드는 전혀 없어서, 나중에 CLI 나 배치 작업에서도 그대로 재사용할 수 있다.

import os
from datetime import datetime

import anthropic
from anthropic import beta_tool
from flask import current_app


# ----------------------------------------------------------------------
# 1) 도구(tool) 정의
# ----------------------------------------------------------------------
# @beta_tool 을 붙이면 '평범한 파이썬 함수'가 Claude 가 호출할 수 있는 도구가 된다.
# SDK 가 함수 시그니처와 docstring 을 읽어서 도구 명세(JSON schema)를 자동으로 만들어준다.
#   - 함수 이름     -> 도구 이름
#   - docstring 첫 줄 -> 도구 설명 (Claude 가 '언제 이 도구를 쓸지' 판단하는 근거)
#   - 인자 타입힌트  -> 입력 스키마
#   - Args: 항목    -> 각 인자의 설명
# 그래서 docstring 을 성의 없이 쓰면 Claude 가 도구를 엉뚱하게 쓰거나 아예 안 쓴다.


@beta_tool
def get_current_time() -> str:
    """지금 시각을 알려준다. 오늘 날짜나 현재 시간을 물어보면 이 도구를 사용한다."""
    # 모델은 학습 시점 이후의 '현재 시각'을 알 수 없다.
    # 이렇게 실제 값을 가져다주는 게 도구의 가장 기본적인 역할이다.
    return datetime.now().strftime("%Y년 %m월 %d일 %H시 %M분 %S초")


@beta_tool
def list_app_routes() -> str:
    """이 Flask 앱에 등록된 URL 목록을 돌려준다.

    앱의 페이지 구성, 어떤 블루프린트가 있는지, 특정 기능의 주소를 물어볼 때 사용한다.
    """
    # current_app 은 '지금 처리 중인 요청이 속한 앱'을 가리키는 프록시다.
    # 덕분에 app 객체를 인자로 넘겨받지 않아도 앱의 정보에 접근할 수 있다.
    # (요청 처리 바깥에서 부르면 에러가 나므로, 웹 요청 안에서만 호출되어야 한다.)
    lines = []
    for rule in sorted(current_app.url_map.iter_rules(), key=lambda r: str(r)):
        if rule.endpoint == "static":
            continue
        methods = ",".join(sorted(rule.methods - {"HEAD", "OPTIONS"}))
        # endpoint 는 "블루프린트이름.함수이름" 형식이다.
        lines.append(f"{rule.rule} [{methods}] -> {rule.endpoint}")
    return "\n".join(lines)


# Claude 에게 넘길 도구 목록. 여기에 함수를 추가하기만 하면 도구가 늘어난다.
TOOLS = [get_current_time, list_app_routes]


SYSTEM_PROMPT = """당신은 Flask 블루프린트 학습용 샘플 앱에 내장된 도우미입니다.
이 앱은 main / auth / agent 세 개의 블루프린트로 이루어져 있습니다.
사용자가 앱의 구조나 현재 시각을 물으면 추측하지 말고 반드시 주어진 도구를 사용해 확인하세요.
답변은 한국어로, 초보자가 이해할 수 있게 간결하게 작성하세요."""


# ----------------------------------------------------------------------
# 2) 대화 실행
# ----------------------------------------------------------------------
class AgentNotConfigured(Exception):
    """API 키가 없어서 에이전트를 쓸 수 없을 때 발생시키는 예외."""


def is_configured():
    """API 키가 준비되어 있는지 확인한다. 화면에 안내를 띄울 때 쓴다."""
    return bool(current_app.config.get("ANTHROPIC_API_KEY"))


def run_agent(history, user_message):
    """대화 한 턴을 실행하고 (답변 텍스트, 사용한 도구 목록) 을 돌려준다.

    history: [{"role": "user"/"assistant", "content": ...}, ...] 형태의 이전 대화
    user_message: 이번에 사용자가 입력한 문장
    """
    api_key = current_app.config.get("ANTHROPIC_API_KEY")
    if not api_key:
        raise AgentNotConfigured("ANTHROPIC_API_KEY 가 설정되지 않았습니다.")

    client = anthropic.Anthropic(api_key=api_key)
    messages = history + [{"role": "user", "content": user_message}]

    # tool_runner: '에이전트 루프'를 SDK 가 대신 돌려주는 헬퍼.
    # 직접 만들면 아래 과정을 while 문으로 반복해야 한다.
    #   요청 -> Claude 가 도구 호출을 요구 -> 내가 함수 실행 -> 결과를 다시 전달 -> 반복
    # tool_runner 는 이 반복을 알아서 처리하고, 매 턴의 응답을 하나씩 내어준다.
    runner = client.beta.messages.tool_runner(
        model=current_app.config["AGENT_MODEL"],
        max_tokens=current_app.config["AGENT_MAX_TOKENS"],
        system=SYSTEM_PROMPT,
        tools=TOOLS,
        messages=messages,
        # 적응형 사고: 어려운 질문에는 더 오래 생각하고, 쉬운 질문은 바로 답한다.
        thinking={"type": "adaptive"},
        # effort 는 '얼마나 공들일지'를 정한다. 웹 요청은 사용자가 기다리는 화면이므로
        # 기본값(high) 대신 medium 으로 낮춰 응답 속도를 확보했다.
        output_config={"effort": current_app.config["AGENT_EFFORT"]},
        # 안전 분류기가 요청을 거절했을 때 서버가 알아서 다른 모델로 넘겨주는 기능.
        betas=["server-side-fallback-2026-07-01"],
        fallbacks="default",
    )

    used_tools = []
    final_message = None

    # runner 를 순회하면 도구 호출이 끝날 때까지 자동으로 반복된다.
    for message in runner:
        final_message = message
        # 이번 턴에 Claude 가 어떤 도구를 불렀는지 기록해 둔다(화면에 보여주기 위함).
        for block in message.content:
            if block.type == "tool_use":
                used_tools.append(block.name)

    # 최종 답변 텍스트만 뽑아낸다.
    # content 는 text / thinking / tool_use 등 여러 종류의 블록이 섞인 리스트이므로
    # 반드시 .type 을 확인하고 꺼내야 한다.
    reply = ""
    if final_message is not None:
        reply = "\n".join(
            block.text for block in final_message.content if block.type == "text"
        ).strip()

    if not reply:
        reply = "(모델이 빈 응답을 반환했습니다.)"

    return reply, used_tools
