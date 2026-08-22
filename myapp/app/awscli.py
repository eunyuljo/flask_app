# app/awscli.py
# AWS CLI 명령을 안전하게 실행하는 계층.
#
# 핵심: 셸을 쓰지 않는다(shell=False). 그래서 ; | && $() ` 같은 기호가
# 특별한 뜻을 잃고 그냥 문자열이 된다. 셸이 없으니 셸 주입도 없다.
# 여기에 '무엇을 실행할 수 있는지' 허용 목록을 더해 두 겹으로 막는다.

import os
import shlex
import subprocess
import time

# 실행을 허용할 프로그램. 지금은 aws 하나뿐이다.
ALLOWED_BINARY = "aws"

# aws 하위 명령 중 읽기 전용에 해당하는 접두사.
# 이 목록에 없으면 실행하지 않는다. (예: delete-, create-, put-, modify- 는 전부 거부)
READ_ONLY_PREFIXES = ("describe", "list", "get", "search", "lookup", "batch-get")

# 하위 명령 없이 단독으로 허용할 것들
ALLOWED_BARE = {"help", "--version"}

# 사용자가 리전·프로필·자격증명을 명령으로 바꾸지 못하게 한다.
# 리전은 화면에서 고른 값으로만 정해져야 하고, 프로필을 지정하면
# 서버에 있는 다른 자격증명을 쓸 수 있게 되기 때문이다.
BLOCKED_OPTIONS = {
    "--profile", "--region", "--endpoint-url", "--ca-bundle",
    "--cli-binary-format", "--debug", "--no-verify-ssl",
}

DEFAULT_TIMEOUT = 30
MAX_OUTPUT = 200_000   # 화면이 감당할 수 있는 크기로 자른다


class CommandRejected(Exception):
    """허용 목록에 걸려 '실행하지 않았을' 때. 사용자에게 이유를 그대로 보여준다.

    감사 관점에서 이건 '금지된 명령을 시도했다' 는 기록이다.
    실행 자체가 실패한 것(ExecutionError)과 반드시 구분해야 한다.
    둘을 같은 값으로 남기면 감사 로그에서 위험 신호를 골라낼 수 없다.
    """


class ExecutionError(Exception):
    """명령은 허용됐으나 실행에 실패했을 때(바이너리 없음, 시간 초과 등).

    사용자의 잘못이 아니라 서버 환경 문제다.
    """


def parse(command):
    """명령 문자열을 토큰으로 나누고 허용 여부를 판정한다.

    shlex.split 은 따옴표만 해석하고 셸 확장은 하지 않는다.
    즉 $(...) 나 `...` 는 여기서 실행되지 않고 글자 그대로 남는다.
    """
    if not command or not command.strip():
        raise CommandRejected("명령이 비어 있습니다.")

    try:
        argv = shlex.split(command.strip())
    except ValueError as e:
        raise CommandRejected(f"명령을 해석할 수 없습니다: {e}")

    if not argv:
        raise CommandRejected("명령이 비어 있습니다.")

    if argv[0] != ALLOWED_BINARY:
        raise CommandRejected(
            f"'{ALLOWED_BINARY}' 로 시작하는 명령만 실행할 수 있습니다.\n"
            f"입력한 명령: {argv[0]}"
        )

    # 셸 문법이 섞여 있으면 거부한다.
    # shell=False 라서 이런 기호는 어차피 그냥 글자로 넘어가 aws 가 알 수 없는
    # 인자라며 실패한다. 즉 이 검사는 보안을 위한 것이 아니라,
    # "왜 안 되는지" 를 분명히 알려주기 위한 것이다.
    # (보안은 shell=False 와 허용 목록이 담당한다.)
    SHELL_CHARS = (";", "&&", "||", "|", "$(", "`", ">", "<", "\n")
    for token in argv[1:]:
        for ch in SHELL_CHARS:
            if ch in token:
                raise CommandRejected(
                    f"셸 문법은 쓸 수 없습니다: {token!r} 안의 {ch!r}\n"
                    "이 콘솔은 셸을 거치지 않고 aws 를 직접 실행하므로 "
                    "파이프·리다이렉션·명령 연결이 동작하지 않습니다."
                )

    # 금지 옵션 확인. --region=ap-northeast-2 처럼 붙여 쓴 형태도 잡는다.
    for token in argv[1:]:
        name = token.split("=", 1)[0]
        if name in BLOCKED_OPTIONS:
            raise CommandRejected(
                f"이 옵션은 사용할 수 없습니다: {name}\n"
                "리전과 자격증명은 화면에서 고른 계정 설정으로만 정해집니다."
            )

    # aws <서비스> <하위명령> 구조에서 하위명령을 찾는다.
    positionals = [t for t in argv[1:] if not t.startswith("-")]

    if not positionals:
        if any(t in ALLOWED_BARE for t in argv[1:]):
            return argv
        raise CommandRejected(
            "서비스와 하위 명령이 필요합니다. 예) aws ec2 describe-instances"
        )

    if len(positionals) < 2:
        if positionals[0] in ALLOWED_BARE:
            return argv
        raise CommandRejected(
            f"하위 명령이 필요합니다. 예) aws {positionals[0]} describe-..."
        )

    subcommand = positionals[1]
    if not subcommand.startswith(READ_ONLY_PREFIXES):
        raise CommandRejected(
            f"읽기 전용 명령만 실행할 수 있습니다: '{subcommand}' 는 허용되지 않습니다.\n"
            f"허용되는 하위 명령 접두사: {', '.join(READ_ONLY_PREFIXES)}"
        )

    return argv


def run(command, env_extra, timeout=DEFAULT_TIMEOUT):
    """허용 목록을 통과한 명령만 실행한다.

    env_extra: AssumeRole 로 받은 임시 자격증명 환경변수
    """
    argv = parse(command)

    # 서버의 환경변수를 통째로 물려주지 않는다.
    # .env 로 들어온 ANTHROPIC_API_KEY, DB 비밀번호 등이 자식 프로세스에
    # 노출될 이유가 없다. 실행에 꼭 필요한 것만 골라 넣는다.
    #
    # HOME 을 /tmp 로 바꾸는 것도 같은 이유다. 그대로 두면 CLI 가 서버의
    # ~/.aws 를 읽어서, 화면에서 고른 계정이 아니라 서버 자신의 자격증명으로
    # 명령이 나갈 수 있다.
    env = {
        "PATH": os.environ.get("PATH", "/usr/local/bin:/usr/bin:/bin"),
        "HOME": "/tmp",
        "AWS_PAGER": "",           # CLI 가 less 를 띄우면 응답이 멈춘다
        "AWS_CONFIG_FILE": "/dev/null",
        "AWS_SHARED_CREDENTIALS_FILE": "/dev/null",
        "LC_ALL": "C.UTF-8",
    }

    # 네트워크 설정은 물려준다. 비밀이 아니고, 없으면 아예 나가지 못한다.
    #
    # MSP 환경에서는 외부로 나가는 트래픽이 사내 프록시를 거치는 경우가 흔하고,
    # 그 프록시가 자체 CA 로 TLS 를 다시 맺습니다. 이 값을 빼면 CLI 가
    # "certificate verify failed" 로 죽습니다 - 개발 환경에서 실제로 그랬다.
    #
    # 검증을 끄는 선택지(--no-verify-ssl)는 두지 않는다. 그건 고객사 계정으로
    # 가는 연결을 아무나 가로챌 수 있게 만드는 것이다. CA 를 알려주는 게 맞다.
    for key in ("HTTPS_PROXY", "HTTP_PROXY", "NO_PROXY",
                "https_proxy", "http_proxy", "no_proxy",
                "AWS_CA_BUNDLE", "REQUESTS_CA_BUNDLE"):
        if os.environ.get(key):
            env[key] = os.environ[key]

    env.update(env_extra)

    started = time.time()
    try:
        proc = subprocess.run(
            argv,
            env=env,
            shell=False,          # 셸을 거치지 않는다
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except FileNotFoundError:
        raise ExecutionError(
            f"'{ALLOWED_BINARY}' 명령을 찾을 수 없습니다. AWS CLI 가 설치되어 있는지 확인하세요."
        )
    except subprocess.TimeoutExpired:
        raise ExecutionError(f"{timeout}초 안에 끝나지 않아 중단했습니다.")

    elapsed = round(time.time() - started, 2)
    out = (proc.stdout or "")[:MAX_OUTPUT]
    err = (proc.stderr or "")[:MAX_OUTPUT]

    return {
        "argv": argv,
        "returncode": proc.returncode,
        "stdout": out,
        "stderr": err,
        "elapsed": elapsed,
        "truncated": len(proc.stdout or "") > MAX_OUTPUT,
    }
