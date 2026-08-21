# app/views/console.py
# 고객사 -> 계정 -> 리전을 고르고 AWS CLI 읽기 전용 명령을 실행하는 콘솔.
# app/__init__.py 에서 url_prefix="/console" 로 등록된다.

from flask import (
    Blueprint, render_template, request, redirect, url_for, session, flash, current_app
)

from app.accounts import list_accounts, get_account, by_customer, AccountError
from app.aws_session import get_env, is_demo, SessionError, cache_state
from app.awscli import run, parse, CommandRejected, ExecutionError, READ_ONLY_PREFIXES
from app import event_store

console_bp = Blueprint("console", __name__)

EXAMPLES = [
    "aws ec2 describe-instances",
    "aws ec2 describe-security-groups",
    "aws s3api list-buckets",
    "aws rds describe-db-instances",
    "aws iam list-roles",
]


@console_bp.before_request
def require_login():
    """콘솔은 관리자만 쓸 수 있게 한다.

    고객사 계정에 접근하는 화면이라 로그인만으로는 부족하다.
    """
    if not session.get("username"):
        flash("콘솔을 쓰려면 먼저 로그인해 주세요.", "error")
        return redirect(url_for("auth.login"))
    if session.get("username") not in current_app.config["ADMIN_USERS"]:
        flash("콘솔은 관리자만 사용할 수 있습니다.", "error")
        return redirect(url_for("main.index"))


def _audit(account, region, command, outcome, detail=""):
    """누가 어느 계정에 무슨 명령을 냈는지 남긴다.

    다중 고객사 환경에서는 이 기록이 선택이 아니다.
    이벤트 저장소를 그대로 쓴다 - 명령 실행도 하나의 사건이기 때문이다.
    """
    event_store.add(
        {
            "event_id": f"console-{id(command)}-{outcome}",
            "event_type": "console",
            "source": (account or {}).get("account_id", "unknown"),
            "severity": "info" if outcome == "ok" else "warning",
            "message": f"[{session.get('username')}] {command}",
            "occurred_at": "",
            "received_at": "",
            "fingerprint": outcome,
            "meta": {
                "user": session.get("username"),
                "customer": (account or {}).get("customer"),
                "account_id": (account or {}).get("account_id"),
                "region": region,
                "outcome": outcome,
                "detail": detail[:200],
            },
        },
        False,
    )


@console_bp.route("/", methods=["GET", "POST"])
def index():
    error = None
    accounts = []
    try:
        accounts = list_accounts()
    except AccountError as e:
        error = str(e)

    grouped = by_customer(accounts)

    # 선택 상태. POST 면 폼 값, GET 이면 쿼리스트링에서 가져온다.
    src = request.form if request.method == "POST" else request.args
    customer = src.get("customer") or (sorted(grouped)[0] if grouped else "")
    account_id = src.get("account_id") or ""
    region = src.get("region") or ""
    command = src.get("command", "")

    # 고객사가 바뀌면 계정 선택을 그 고객사 것으로 맞춘다.
    customer_accounts = grouped.get(customer, [])
    if account_id not in {a["account_id"] for a in customer_accounts}:
        account_id = customer_accounts[0]["account_id"] if customer_accounts else ""

    account = get_account(account_id) if account_id else None
    regions = (account or {}).get("regions") or []
    if region not in regions:
        region = regions[0] if regions else ""

    result = None
    if request.method == "POST" and command.strip() and not error:
        try:
            if not account:
                raise SessionError("계정을 고르세요.")
            # 1) 허용 목록 판정 (실행 전에 먼저 막는다)
            parse(command)
            # 2) 그 계정의 임시 자격증명
            env = get_env(account, region)
            # 3) 셸 없이 실행
            result = run(command, env)
            _audit(account, region, command,
                   "ok" if result["returncode"] == 0 else "failed",
                   result["stderr"][:200])
        except CommandRejected as e:
            # 금지된 명령을 시도한 것. 감사 관점에서 눈여겨봐야 할 기록이다.
            error = str(e)
            _audit(account, region, command, "rejected", str(e))
        except ExecutionError as e:
            # 명령은 허용됐으나 실행 환경 문제로 실패. 사용자 잘못이 아니다.
            error = str(e)
            _audit(account, region, command, "exec_failed", str(e))
        except SessionError as e:
            error = str(e)
            _audit(account, region, command, "no_credentials", str(e))

    return render_template(
        "console.html",
        grouped=grouped,
        customer=customer,
        customer_accounts=customer_accounts,
        account=account,
        account_id=account_id,
        regions=regions,
        region=region,
        command=command,
        result=result,
        error=error,
        examples=EXAMPLES,
        prefixes=READ_ONLY_PREFIXES,
        demo=is_demo(account) if account else False,
        sessions=cache_state(),
    )
