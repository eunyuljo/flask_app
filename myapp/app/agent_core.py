# app/agent_core.py
# AI 에이전트의 '두뇌' 부분. Claude 에게 넘길 도구(tool)들을 정의하고, 대화 한 턴을 실행한다.
# 웹(HTTP)과 관련된 코드는 전혀 없어서, 나중에 CLI 나 배치 작업에서도 그대로 재사용할 수 있다.

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
# 2) 호출 경로(provider) 전환
# ----------------------------------------------------------------------
# 같은 Claude 모델을 부르는 두 가지 경로를 환경변수 하나로 갈아끼운다.
# 도구 정의와 에이전트 루프는 양쪽이 완전히 똑같고, '클라이언트를 어떻게 만드느냐'만 다르다.
CLAUDE_API = "claude_api"
BEDROCK = "bedrock"


class AgentNotConfigured(Exception):
    """설정이 부족해서 에이전트를 쓸 수 없을 때 발생시키는 예외."""


def _resolve_model(provider, model):
    """provider 에 맞는 모델 ID 를 만든다.

    Bedrock 은 같은 모델이라도 이름 앞에 "anthropic." 이 붙는다.
        claude_api -> claude-opus-5
        bedrock    -> anthropic.claude-opus-5
    설정에는 짧은 이름만 적어두고 여기서 자동으로 붙여주므로,
    provider 를 바꿔도 AGENT_MODEL 은 손댈 필요가 없다.
    """
    if provider == BEDROCK and not model.startswith("anthropic."):
        return "anthropic." + model
    return model


def check_config():
    """설정이 준비됐는지 확인한다. 문제가 없으면 None, 있으면 안내 문구를 돌려준다."""
    cfg = current_app.config
    provider = cfg["AGENT_PROVIDER"]

    if provider == CLAUDE_API:
        if not cfg["ANTHROPIC_API_KEY"]:
            return "ANTHROPIC_API_KEY 가 설정되지 않았습니다."
        return None

    if provider == BEDROCK:
        if not cfg["AWS_REGION"]:
            return "Bedrock 을 쓰려면 AWS_REGION 이 필요합니다."
        # 액세스 키는 비어 있어도 된다(IAM 역할이나 ~/.aws/credentials 로 해결될 수 있음).
        # 그래서 여기서는 막지 않고, 실제 호출 시점에 자격증명이 없으면 에러로 알린다.
        return None

    return f"AGENT_PROVIDER 값이 올바르지 않습니다: {provider!r} (claude_api 또는 bedrock)"


def _build_client(provider, cfg):
    """provider 에 맞는 클라이언트를 만든다. 여기가 두 경로의 유일한 차이점이다."""
    if provider == BEDROCK:
        # Bedrock 은 전용 클래스를 쓴다.
        # 일반 Anthropic() 에 base_url 만 바꿔 끼우는 방식은 동작하지 않는다.
        # 인증이 API 키가 아니라 AWS SigV4 서명이기 때문이다.
        kwargs = {"aws_region": cfg["AWS_REGION"]}

        # 값이 있는 항목만 넘긴다. 전부 비우면 botocore 기본 자격증명 체인이 알아서 찾는다.
        if cfg["AWS_ACCESS_KEY_ID"]:
            kwargs["aws_access_key"] = cfg["AWS_ACCESS_KEY_ID"]
        if cfg["AWS_SECRET_ACCESS_KEY"]:
            kwargs["aws_secret_key"] = cfg["AWS_SECRET_ACCESS_KEY"]
        if cfg["AWS_SESSION_TOKEN"]:
            kwargs["aws_session_token"] = cfg["AWS_SESSION_TOKEN"]
        if cfg["AWS_PROFILE"]:
            kwargs["aws_profile"] = cfg["AWS_PROFILE"]

        # 거절 시 폴백: Bedrock 은 서버측 fallbacks 파라미터를 지원하지 않으므로
        # SDK 가 클라이언트에서 재시도해주는 미들웨어를 대신 쓴다.
        fallback_model = _resolve_model(BEDROCK, cfg["AGENT_FALLBACK_MODEL"])
        return anthropic.AnthropicBedrockMantle(
            middleware=[
                anthropic.BetaRefusalFallbackMiddleware(
                    fallbacks=[{"model": fallback_model}]
                )
            ],
            **kwargs,
        )

    # 기본 경로: Anthropic 에 직접 호출.
    return anthropic.Anthropic(api_key=cfg["ANTHROPIC_API_KEY"])


def _provider_kwargs(provider, cfg):
    """provider 별로 요청에 더 붙일 파라미터를 돌려준다."""
    if provider == BEDROCK:
        # Bedrock 에서는 fallbacks / 관련 beta 헤더를 보내면 안 된다.
        # (SDK 가 막아주지 않고 그대로 전송하므로, 여기서 빼는 게 우리 책임이다.)
        return {}

    # Claude API 에서는 서버가 알아서 다른 모델로 넘겨준다.
    return {
        "betas": ["server-side-fallback-2026-07-01"],
        "fallbacks": "default",
    }


# ----------------------------------------------------------------------
# 3) 대화 실행
# ----------------------------------------------------------------------
def run_agent(history, user_message):
    """대화 한 턴을 실행하고 (답변 텍스트, 사용한 도구 목록) 을 돌려준다.

    history: [{"role": "user"/"assistant", "content": ...}, ...] 형태의 이전 대화
    user_message: 이번에 사용자가 입력한 문장
    """
    problem = check_config()
    if problem:
        raise AgentNotConfigured(problem)

    cfg = current_app.config
    provider = cfg["AGENT_PROVIDER"]
    client = _build_client(provider, cfg)

    messages = history + [{"role": "user", "content": user_message}]

    # tool_runner: '에이전트 루프'를 SDK 가 대신 돌려주는 헬퍼.
    # 직접 만들면 아래 과정을 while 문으로 반복해야 한다.
    #   요청 -> Claude 가 도구 호출을 요구 -> 내가 함수 실행 -> 결과를 다시 전달 -> 반복
    # tool_runner 는 이 반복을 알아서 처리하고, 매 턴의 응답을 하나씩 내어준다.
    # 이 부분은 claude_api / bedrock 양쪽이 완전히 동일하다.
    runner = client.beta.messages.tool_runner(
        model=_resolve_model(provider, cfg["AGENT_MODEL"]),
        max_tokens=cfg["AGENT_MAX_TOKENS"],
        system=SYSTEM_PROMPT,
        tools=TOOLS,
        messages=messages,
        # 적응형 사고: 어려운 질문에는 더 오래 생각하고, 쉬운 질문은 바로 답한다.
        thinking={"type": "adaptive"},
        # effort 는 '얼마나 공들일지'를 정한다. 웹 요청은 사용자가 기다리는 화면이므로
        # 기본값(high) 대신 medium 으로 낮춰 응답 속도를 확보했다.
        output_config={"effort": cfg["AGENT_EFFORT"]},
        **_provider_kwargs(provider, cfg),
    )

    used_tools = []
    final_message = None

    try:
        # runner 를 순회하면 도구 호출이 끝날 때까지 자동으로 반복된다.
        for message in runner:
            final_message = message
            # 이번 턴에 Claude 가 어떤 도구를 불렀는지 기록해 둔다(화면에 보여주기 위함).
            for block in message.content:
                if block.type == "tool_use":
                    used_tools.append(block.name)
    except Exception as e:
        # AWS 자격증명 관련 실패는 anthropic 예외 체계 밖에서 올라온다.
        #   - SDK 가 서명 직전에 던지는 RuntimeError
        #     ("Could not resolve AWS credentials from session")
        #   - botocore 자체 예외(만료된 토큰, 잘못된 프로필 등)
        # 둘 다 '설정을 고쳐야 하는 문제'이므로 AgentNotConfigured 로 바꿔서
        # 화면에 원인을 안내한다.
        is_botocore = type(e).__module__.split(".")[0] == "botocore"
        is_no_creds = isinstance(e, RuntimeError) and "AWS credentials" in str(e)
        if is_botocore or is_no_creds:
            raise AgentNotConfigured(
                f"AWS 자격증명을 확인할 수 없습니다: {e}"
            ) from e
        raise

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


# ----------------------------------------------------------------------
# 4) 알람 진단
# ----------------------------------------------------------------------
# 채팅과 달리 '요청 한 번'으로 끝나는 기능이다. 대화 기록을 남기지 않으므로
# 앞서 본 다른 고객사의 조회 결과가 다음 진단에 섞일 여지가 없다.
#
# 조회 대상 계정은 화면에서 담당자가 고른 값 하나로 고정되고,
# g(요청 컨텍스트)에 실려서 도구에 전달된다. 모델은 계정을 인자로 받지 않는다.
# 계정을 도구 인자로 두면 모델이 다른 고객사 계정 번호를 지어내 조회할 수 있다.

# 진단 도구가 한 번에 모델에게 넘길 수 있는 출력 크기.
# 콘솔 화면용 상한(awscli.MAX_OUTPUT, 20만자)과 일부러 다르게 잡았다.
# 사람은 긴 JSON 을 스크롤해서 보면 되지만, 모델에게 넘기면 그대로 컨텍스트를
# 채우고 비용이 된다. 잘렸다는 사실을 알려주면 --query 로 좁혀서 다시 부른다.
DIAG_MAX_OUTPUT = 12_000

# 도구 호출을 몇 번까지 허용할지. 거부된 명령을 모델이 계속 변형해가며
# 시도하는 상황을 여기서 끊는다.
DIAG_MAX_ITERATIONS = 12


@beta_tool
def aws_read(command: str) -> str:
    """이 알람이 발생한 AWS 계정에서 읽기 전용 aws CLI 명령을 실행하고 결과를 돌려준다.

    계정과 리전은 담당자가 화면에서 이미 정했으므로 명령에 쓰지 않는다.
    --region, --profile 같은 옵션을 붙이면 거부된다.
    describe/list/get 으로 시작하는 조회 명령만 실행할 수 있다.
    출력이 크면 --query 나 --max-items 로 좁혀서 다시 부를 것.

    Args:
        command: 실행할 명령. 예) aws ec2 describe-instances --instance-ids i-0abc123
    """
    from flask import g

    from app.awscli import run, CommandRejected, ExecutionError
    from app.aws_session import get_env, SessionError

    account = g.diag_account
    region = g.diag_region

    try:
        env = get_env(account, region)
        result = run(command, env, timeout=20)
    except CommandRejected as e:
        # 예외로 던지지 않고 문자열로 돌려준다.
        # 던지면 tool_runner 루프가 그대로 죽고, 모델은 왜 안 됐는지 알 수 없다.
        # 문자열로 주면 허용되는 형태로 스스로 고쳐서 다시 부른다.
        g.diag_commands.append({"command": command, "outcome": "rejected", "detail": str(e)})
        return f"[거부됨] {e}"
    except ExecutionError as e:
        g.diag_commands.append({"command": command, "outcome": "exec_failed", "detail": str(e)})
        return f"[실행 실패] {e}"
    except SessionError as e:
        g.diag_commands.append({"command": command, "outcome": "no_credentials", "detail": str(e)})
        return f"[자격증명 없음] {e}"

    out = result["stdout"] or result["stderr"] or "(출력 없음)"
    truncated = len(out) > DIAG_MAX_OUTPUT
    if truncated:
        out = out[:DIAG_MAX_OUTPUT]

    g.diag_commands.append({
        "command": command,
        "outcome": "ok" if result["returncode"] == 0 else "failed",
        "detail": f"{result['elapsed']}초",
    })

    header = f"$ {' '.join(result['argv'])}\n(종료코드 {result['returncode']})\n"
    if truncated:
        header += (
            f"[출력이 {DIAG_MAX_OUTPUT}자에서 잘렸습니다. "
            "--query 나 --max-items 로 범위를 좁혀 다시 조회하세요.]\n"
        )
    return header + out


DIAGNOSE_SYSTEM_PROMPT = """당신은 AWS 운영 담당자를 돕는 진단 도우미입니다.
알람 하나를 받아서, 담당자가 다음에 무엇을 볼지 판단할 수 있게 짧은 진단을 씁니다.

작업 방식:
- 알람 메시지와 meta 에 리소스 ID(i-..., vol-..., 알람 이름 등)가 있으면
  aws_read 도구로 그 리소스의 현재 상태를 직접 확인하세요. 추측하지 마세요.
- 조회는 필요한 만큼만 하세요. 관련 없는 리소스를 훑지 마세요.
- 도구가 [거부됨] 을 돌려주면 허용되는 읽기 전용 명령으로 바꿔서 다시 시도하세요.
- 확인할 수 없는 것은 확인할 수 없다고 쓰세요. 지어내지 마세요.

답변 형식(한국어, 각 항목 1~3줄):
## 추정 원인
## 확인한 것
## 다음에 확인할 것

발생 이력이 주어지면 반드시 반영하세요. 처음 발생인지 반복되는 알람인지에 따라
봐야 할 곳이 달라집니다. 이력이 '집계 불가'로 표시되면 그 사실을 밝히세요.

등록된 대응 절차가 주어지면 그 절차를 우선하세요. 일반적인 AWS 지식보다
이 조직이 실제로 하는 방식이 먼저입니다. 절차대로 확인한 결과를 쓰고,
절차에 없는 것을 제안할 때는 절차 밖의 제안임을 밝히세요.

같은 알람의 지난 장애가 주어지면 그 원인을 먼저 확인하세요. 다만
'지난번과 같은 원인' 이라고 단정하지 말고, 실제로 그런지 도구로 확인한 뒤
쓰세요. 확인할 수 없으면 '지난번에는 X 였는데 이번에는 확인하지 못했다' 고
쓰세요."""


def _format_event(event, history, runbook=None, past=None):
    """모델에게 넘길 알람 설명을 만든다."""
    lines = [
        "다음 알람을 진단해 주세요.",
        "",
        f"- 심각도: {event.get('severity')}",
        f"- 출처(source): {event.get('source')}",
        f"- 종류(event_type): {event.get('event_type')}",
        f"- 발생 시각: {event.get('occurred_at')}",
        f"- 메시지: {event.get('message')}",
    ]

    meta = event.get("meta") or {}
    if meta:
        import json as _json
        lines.append(f"- meta: {_json.dumps(meta, ensure_ascii=False, default=str)[:2000]}")

    lines.append("")
    if history is None:
        lines.append("발생 이력: 집계 불가 (이벤트 DB 를 조회할 수 없음)")
    else:
        lines.append(
            f"발생 이력: 같은 종류의 알람이 전체 {history['total']}건, "
            f"최근 {history['hours']}시간 안에 {history['recent']}건."
        )
        if history.get("first_seen"):
            lines.append(
                f"  처음 발생 {history['first_seen']}, 마지막 발생 {history['last_seen']}"
            )
        for s in history.get("samples", [])[:5]:
            lines.append(f"  - {s['occurred_at']} [{s['severity']}] {s['message']}")

    # 등록된 대응 절차가 있으면 그대로 넘긴다.
    # 이게 없으면 모델은 일반적인 AWS 지식으로만 답할 수밖에 없다.
    # 절차가 있으면 "이 고객사에서 이 알람에 실제로 하는 것" 을 답한다.
    if runbook:
        scope = f"{runbook['customer']} 전용" if runbook["customer"] else "공통"
        lines.append("")
        lines.append(f"등록된 대응 절차 ({scope}) — {runbook['title']}")
        lines.append(runbook["body"])

    # 같은 알람이 관련됐던 지난 장애. 런북이 '이럴 땐 이렇게 하세요' 라면
    # 이건 '지난번엔 이게 원인이었다' 다. 확정된 사후 보고서만 온다.
    if past:
        lines.append("")
        lines.append(f"같은 알람이 관련됐던 지난 장애 {len(past)}건")
        for p in past:
            when = f"{p['started_at']:%Y-%m-%d}"
            lines.append(f"  [{when}] {p['title']} (심각도 {p['severity']})")
            lines.append(f"    원인: {p['cause']}")
            if p.get("action"):
                lines.append(f"    조치: {p['action']}")
            if p.get("prevention"):
                lines.append(f"    재발 방지: {p['prevention']}")

    return "\n".join(lines)


def diagnose(event, account, region, history=None, runbook=None, past=None):
    """알람 하나를 진단한다. (진단문, 실행한 명령 목록) 을 돌려준다.

    event   : 정규화된 이벤트 레코드
    account : app.accounts 가 돌려준 계정 dict (조회 범위는 이 계정 하나뿐)
    region  : 조회할 리전
    history : app.stats.fingerprint_history 결과. 없으면 None.
    runbook : app.runbook.find() 결과. 있으면 모델이 이 절차를 따른다.
    past    : app.incident.past_incidents() 결과. 같은 알람의 지난 장애.
    """
    from flask import g

    problem = check_config()
    if problem:
        raise AgentNotConfigured(problem)

    cfg = current_app.config
    provider = cfg["AGENT_PROVIDER"]
    client = _build_client(provider, cfg)

    # 도구가 볼 값을 요청 컨텍스트에 실어둔다. 모델이 고를 수 없는 자리다.
    g.diag_account = account
    g.diag_region = region
    g.diag_commands = []

    runner = client.beta.messages.tool_runner(
        model=_resolve_model(provider, cfg["AGENT_MODEL"]),
        max_tokens=cfg["AGENT_MAX_TOKENS"],
        system=DIAGNOSE_SYSTEM_PROMPT,
        tools=[aws_read],
        messages=[{"role": "user", "content": _format_event(event, history, runbook, past)}],
        max_iterations=DIAG_MAX_ITERATIONS,
        thinking={"type": "adaptive"},
        output_config={"effort": cfg["AGENT_EFFORT"]},
        **_provider_kwargs(provider, cfg),
    )

    final_message = None
    try:
        for message in runner:
            final_message = message
    except Exception as e:
        is_botocore = type(e).__module__.split(".")[0] == "botocore"
        is_no_creds = isinstance(e, RuntimeError) and "AWS credentials" in str(e)
        if is_botocore or is_no_creds:
            raise AgentNotConfigured(f"AWS 자격증명을 확인할 수 없습니다: {e}") from e
        raise

    reply = ""
    if final_message is not None:
        reply = "\n".join(
            block.text for block in final_message.content if block.type == "text"
        ).strip()

    if not reply:
        reply = "(모델이 빈 응답을 반환했습니다.)"

    return reply, g.diag_commands


# ----------------------------------------------------------------------
# 5) 사후 보고서 초안
# ----------------------------------------------------------------------
# 알람 진단(4)과 다른 점이 하나 있다. 여기서는 AWS 를 조회하지 않는다.
# 장애는 이미 지난 일이고 근거는 전부 DB 에 모여 있다 - 타임라인, 작업
# 기록, 리소스 변경, 같은 알람으로 났던 지난 장애. 그래서 도구를 붙이지
# 않은 단순 호출이면 된다.
#
# 그 차이가 감사 로그에도 나타난다. 알람 진단은 '고객사 계정을 건드림'
# 이지만 이건 '우리 DB 로 문서를 만듦' 이다.

RCA_MAX_INPUT = 24_000     # 프롬프트에 싣는 근거 글자 수 상한
RCA_MAX_TIMELINE = 80      # 타임라인 줄 수 상한

RCA_SYSTEM_PROMPT = """당신은 AWS 운영팀의 장애 사후 보고서 작성을 돕습니다.
주어진 근거만으로 초안을 씁니다. 이 초안은 담당자가 고쳐 쓸 재료이지
그대로 나가는 문서가 아닙니다.

지켜야 할 것:

1. 주어진 근거에 없는 사실을 지어내지 마십시오. 로그에 없는 수치, 확인되지
   않은 인과, 실제로 하지 않은 조치를 쓰면 안 됩니다.
2. 근거만으로 단정할 수 없는 것은 단정하지 말고 uncertain 에 적으십시오.
   "무엇을 더 확인해야 이 결론을 확정할 수 있는가" 를 적습니다.
3. 타임라인의 시각과 리소스 변경을 근거로 삼되, 시간 순서가 곧 인과는
   아닙니다. 앞에 일어났다는 이유만으로 원인이라고 쓰지 마십시오.
4. 지난 장애가 함께 주어졌다면 같은 원인인지 살펴보고, 같다면 재발이라는
   점을 원인과 재발 방지에 반영하십시오.
5. 한국어로 씁니다. 각 칸은 문단 두세 개를 넘기지 마십시오.
6. 담당자 이름을 지어내지 말고, 근거에 있는 작업자·티켓 번호만 인용하십시오.

각 칸의 뜻:
- impact     : 무엇이 얼마나 영향을 받았나. 범위와 시간.
- cause      : 왜 일어났나. 직접 원인과, 알 수 있다면 그 뒤의 이유까지.
- action     : 무엇을 해서 멈췄나. 근거에 있는 작업만.
- prevention : 다시 안 나게 하려면. 구체적이고 실행 가능한 것으로.
- uncertain  : 이 근거만으로는 알 수 없어 담당자가 확인해야 하는 것.
"""

# 초안이 채울 칸. incident.NARRATIVE_FIELDS 와 같아야 한다.
RCA_SCHEMA = {
    "type": "object",
    "properties": {
        "impact": {"type": "string"},
        "cause": {"type": "string"},
        "action": {"type": "string"},
        "prevention": {"type": "string"},
        "uncertain": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["impact", "cause", "action", "prevention", "uncertain"],
    "additionalProperties": False,
}


def _format_incident(item, data, past=None, runbooks=None):
    """모델에게 넘길 근거를 글로 만든다.

    화면이 이미 모아둔 것을 그대로 쓴다. 여기서 따로 질의하면 화면과
    보고서 초안이 서로 다른 근거를 보게 된다.
    """
    L = []
    a = L.append

    a(f"# 장애 #{item['id']} — {item['title']}")
    a(f"- 심각도: {item['severity']}")
    a(f"- 발생: {item['started_at']:%Y-%m-%d %H:%M} UTC")
    a(f"- 종료: {item['ended_at']:%Y-%m-%d %H:%M} UTC" if item.get("ended_at")
      else "- 종료: 아직 끝나지 않음")
    if item.get("customer"):
        a(f"- 고객사: {item['customer']}")
    if item.get("account_id"):
        a(f"- 계정/리전: {item['account_id']} / {item.get('region', '')}")
    if (item.get("summary") or "").strip():
        a(f"- 담당자 메모: {item['summary'].strip()}")

    # 사람이 이미 쓴 칸이 있으면 넘긴다. 덮어쓰라는 뜻이 아니라,
    # 이미 파악된 것을 다시 추측하지 말라는 뜻이다.
    written = {f: (item.get(f) or "").strip()
               for f in ("impact", "cause", "action", "prevention")}
    if any(written.values()):
        a("")
        a("## 담당자가 이미 쓴 칸")
        a("(비어 있지 않은 칸은 이미 파악된 내용입니다. 근거와 어긋나지 않는 한 존중하고,")
        a(" 보탤 것이 있으면 보태십시오.)")
        for field, text in written.items():
            if text:
                a(f"- {field}: {text}")

    window = data.get("window") or {}
    a("")
    a("## 타임라인")
    a(f"(장애 구간 앞뒤로 {window.get('lead', 0)}분 / {window.get('tail', 0)}분을 함께 봅니다)")
    if not data.get("account_scoped") and item.get("account_id"):
        a("주의: 계정으로 좁히지 못해 다른 계정의 알람이 섞여 있을 수 있습니다.")

    timeline = data.get("timeline") or []
    if not timeline:
        a("- (구간 안에서 기록된 일이 없습니다)")
    for row in timeline[:RCA_MAX_TIMELINE]:
        a(f"- {row['at']:%m-%d %H:%M:%S} [{row['kind']}/{row['severity']}] "
          f"{row['text']} ({row['detail']})")
    if len(timeline) > RCA_MAX_TIMELINE:
        a(f"- ... 외 {len(timeline) - RCA_MAX_TIMELINE}건 (지면 관계로 생략)")

    # by_kind 는 지문별로 묶은 행들이다(dict 가 아니라 list).
    #   {fingerprint, c, first_seen, last_seen, sample, severity, source}
    by_kind = data.get("by_kind") or []
    if by_kind:
        a("")
        a("## 구간 안 알람 묶음 (같은 종류끼리)")
        for kind in by_kind[:12]:
            a(f"- {kind['c']}회 [{kind['severity']}] {kind['sample']} "
              f"({kind['source']}, {kind['first_seen']:%m-%d %H:%M}"
              f" ~ {kind['last_seen']:%m-%d %H:%M})")

    works = data.get("works") or []
    if works:
        a("")
        a("## 이 구간에 이루어진 작업")
        for w in works:
            a(f"- #{w['id']} {w['title']} · 담당 {w['operator']}"
              + (f" · 티켓 {w['ticket']}" if w.get("ticket") else "")
              + (f"\n  결과 메모: {w['note'].strip()}" if (w.get("note") or "").strip() else ""))

    rdiff = data.get("rdiff")
    if rdiff:
        a("")
        a("## 구간 앞뒤 리소스 변경")
        a(f"(정확한 변경 시각은 알 수 없고, 두 스냅샷 사이라는 것만 알 수 있습니다: "
          f"{data['diff_note']})" if data.get("diff_note") else "")
        # 변경 종류는 change, 바뀐 항목의 이름은 field 다.
        # (status / key 로 잘못 읽으면 '[?] None:' 만 잔뜩 나간다.)
        for change in (rdiff.get("changes") or [])[:30]:
            a(f"- [{change.get('change', '?')}] {change.get('resource_type', '')} "
              f"{change.get('resource_id', '')}")
            for field in (change.get("fields") or [])[:6]:
                a(f"    {field.get('field')}: "
                  f"{field.get('before')} -> {field.get('after')}")

    if past:
        a("")
        a("## 같은 알람으로 났던 지난 장애")
        a("(이번 장애의 알람과 같은 지문으로 묶인, 이미 제출된 보고서입니다)")
        for p in past:
            a(f"- #{p['id']} {p['title']} ({p['started_at']:%Y-%m-%d}, "
              f"해당 알람 {p['event_count']}건)")
            a(f"    그때 원인: {(p.get('cause') or '').strip()[:400]}")
            if (p.get("action") or "").strip():
                a(f"    그때 조치: {p['action'].strip()[:300]}")

    if runbooks:
        a("")
        a("## 관련 런북")
        for r in runbooks:
            a(f"- {r['title']}: {(r.get('body') or '').strip()[:400]}")

    text = "\n".join(x for x in L if x is not None)
    if len(text) > RCA_MAX_INPUT:
        # 자를 때는 잘랐다고 밝힌다. 모델이 '이게 전부' 로 읽으면
        # 없는 것을 근거로 결론을 낸다.
        text = text[:RCA_MAX_INPUT] + "\n\n(근거가 길어 여기서 잘렸습니다.)"
    return text


def draft_rca(item, data, past=None, runbooks=None):
    """사후 보고서 초안을 만든다.

    돌려주는 것: {"impact":..., "cause":..., "action":..., "prevention":...,
                 "uncertain": [...]}

    저장하지 않는다. 화면이 사람에게 보여주고, 사람이 확인한 뒤에 저장한다.
    모델이 쓴 글이 사람 확인 없이 고객사 보고서에 들어가면 안 된다.
    """
    import json

    problem = check_config()
    if problem:
        raise AgentNotConfigured(problem)

    cfg = current_app.config
    provider = cfg["AGENT_PROVIDER"]
    client = _build_client(provider, cfg)

    try:
        response = client.beta.messages.create(
            model=_resolve_model(provider, cfg["AGENT_MODEL"]),
            max_tokens=16_000,
            system=RCA_SYSTEM_PROMPT,
            messages=[{"role": "user",
                       "content": _format_incident(item, data, past, runbooks)}],
            # 도구가 없으니 반복도 없다. 대신 생각은 켠다 - 타임라인에서
            # 인과를 골라내는 일이라 그냥 요약보다 훨씬 어렵다.
            thinking={"type": "adaptive"},
            output_config={
                "effort": cfg["AGENT_EFFORT"],
                # 네 칸을 따로 받는다. 마크다운 한 덩어리로 받아서 제목으로
                # 쪼개면, 모델이 제목을 조금만 다르게 써도 칸이 비어버린다.
                "format": {"type": "json_schema", "schema": RCA_SCHEMA},
            },
            **_provider_kwargs(provider, cfg),
        )
    except Exception as e:
        is_botocore = type(e).__module__.split(".")[0] == "botocore"
        if is_botocore or (isinstance(e, RuntimeError) and "AWS credentials" in str(e)):
            raise AgentNotConfigured(f"AWS 자격증명을 확인할 수 없습니다: {e}") from e
        raise

    # 안전 장치를 거절당하면 stop_reason 이 refusal 로 온다. content 를
    # 먼저 읽으면 빈 응답을 JSON 으로 파싱하려다 엉뚱한 에러가 난다.
    if getattr(response, "stop_reason", None) == "refusal":
        detail = getattr(response, "stop_details", None)
        raise AgentNotConfigured(
            "모델이 이 요청을 처리하지 않았습니다"
            + (f" ({detail.category})" if detail else "") + "."
        )

    text = next((b.text for b in response.content if b.type == "text"), "")
    try:
        draft = json.loads(text)
    except ValueError as e:
        raise AgentNotConfigured(f"모델 응답을 읽지 못했습니다: {e}") from e

    # 스키마가 보장해 주지만, 값이 문자열인지까지는 여기서 한 번 더 본다.
    out = {f: str(draft.get(f, "") or "").strip()
           for f in ("impact", "cause", "action", "prevention")}
    out["uncertain"] = [str(x).strip() for x in (draft.get("uncertain") or []) if str(x).strip()]
    return out
