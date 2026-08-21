# app/aws_session.py
# 고객사 계정으로 들어가기 위한 임시 자격증명을 만든다(sts:AssumeRole).
# 결과는 파일에 쓰지 않고, 명령을 실행할 프로세스의 환경변수로만 넘긴다.

import threading
import time

# (account_id, region) -> {"env": {...}, "expires_at": float}
# AssumeRole 은 보통 1시간짜리 자격증명을 준다. 명령마다 새로 부르면
# 느리고 STS 스로틀링에 걸리므로 캐시한다.
_CACHE = {}
_LOCK = threading.Lock()

# 만료 직전에 쓰다가 실패하는 일이 없도록 여유를 둔다.
_SKEW_SECONDS = 300

DEMO_ACCOUNT_PREFIX = "demo"


class SessionError(Exception):
    """자격증명을 얻지 못했을 때."""


def _demo_env(account, region):
    """role_arn 이 없는 데모 계정용. 실제 AWS 를 부르지 않는다.

    이 값으로는 아무것도 인증되지 않는다. 화면과 명령 실행 흐름을
    자격증명 없이 확인해보기 위한 자리표시자다.
    """
    return {
        "AWS_ACCESS_KEY_ID": "DEMO",
        "AWS_SECRET_ACCESS_KEY": "DEMO",
        "AWS_SESSION_TOKEN": "DEMO",
        "AWS_DEFAULT_REGION": region,
        "AWS_REGION": region,
    }


def is_demo(account):
    return not (account or {}).get("role_arn")


def get_env(account, region, duration_seconds=3600):
    """계정+리전에 맞는 자격증명 환경변수를 돌려준다.

    account: app/accounts.py 가 돌려준 dict
    """
    if not account:
        raise SessionError("계정을 찾을 수 없습니다.")
    if region not in (account.get("regions") or []):
        raise SessionError(
            f"이 계정에 허용된 리전이 아닙니다: {region}\n"
            f"허용 리전: {', '.join(account.get('regions') or []) or '없음'}"
        )

    if is_demo(account):
        return _demo_env(account, region)

    key = (account["account_id"], region)
    now = time.time()

    with _LOCK:
        hit = _CACHE.get(key)
        if hit and hit["expires_at"] - _SKEW_SECONDS > now:
            return dict(hit["env"])

    # 캐시가 없거나 만료가 임박했다 -> 새로 발급
    try:
        import boto3
        from botocore.exceptions import BotoCoreError, ClientError
    except ImportError as e:
        raise SessionError("boto3 가 설치되어 있지 않습니다.") from e

    params = {
        "RoleArn": account["role_arn"],
        # 어느 세션인지 CloudTrail 에 남는 이름이다. 추적을 위해 의미 있게 짓는다.
        "RoleSessionName": f"flaskapp-{account['account_id']}"[:64],
        "DurationSeconds": duration_seconds,
    }
    if account.get("external_id"):
        params["ExternalId"] = account["external_id"]

    try:
        sts = boto3.client("sts", region_name=region)
        result = sts.assume_role(**params)
    except (BotoCoreError, ClientError) as e:
        raise SessionError(f"AssumeRole 에 실패했습니다.\n  {e}") from e

    creds = result["Credentials"]
    env = {
        "AWS_ACCESS_KEY_ID": creds["AccessKeyId"],
        "AWS_SECRET_ACCESS_KEY": creds["SecretAccessKey"],
        "AWS_SESSION_TOKEN": creds["SessionToken"],
        "AWS_DEFAULT_REGION": region,
        "AWS_REGION": region,
    }

    with _LOCK:
        _CACHE[key] = {"env": dict(env), "expires_at": creds["Expiration"].timestamp()}

    return env


def cache_state():
    """캐시 상태(디버깅/화면 표시용). 자격증명 값은 절대 내보내지 않는다."""
    now = time.time()
    with _LOCK:
        return [
            {
                "account_id": acc,
                "region": reg,
                "expires_in": max(0, int(v["expires_at"] - now)),
            }
            for (acc, reg), v in _CACHE.items()
        ]


def clear_cache():
    with _LOCK:
        _CACHE.clear()
