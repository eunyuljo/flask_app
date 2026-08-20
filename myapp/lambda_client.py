# lambda_client.py
# Flask 에서 Lambda 를 호출하는 얇은 계층. LAMBDA_MODE 로 '진짜 AWS 호출' 과
# '같은 프로세스에서 핸들러 직접 실행(로컬)' 을 갈아끼운다. AWS 없이도 개발할 수 있게 하기 위함이다.

import json

import boto3
from botocore.exceptions import BotoCoreError, ClientError
from flask import current_app

from lambda_functions.normalize_handler import lambda_handler

LOCAL = "local"
AWS = "aws"


class LambdaInvokeError(Exception):
    """Lambda 호출 자체가 실패했을 때(권한, 함수 없음, 네트워크 등)."""


def _invoke_local(payload):
    """로컬 모드: 핸들러를 그냥 파이썬 함수로 부른다.

    Lambda 핸들러는 결국 event 를 받는 함수일 뿐이므로,
    AWS 를 거치지 않고 직접 호출해도 결과가 똑같다.
    네트워크도 자격증명도 필요 없어서 학습과 테스트에 편하다.
    다만 '비동기 호출'이나 '타임아웃', '동시 실행 제한' 같은 Lambda 특성은 재현되지 않는다.
    """
    return lambda_handler(payload, None)


def _invoke_aws(payload, invocation_type):
    """AWS 모드: 실제 Lambda 함수를 호출한다.

    invocation_type
      "RequestResponse" (동기)  - 결과를 받을 때까지 기다린다. 화면에 결과를 보여줄 때.
      "Event"           (비동기) - 던지고 바로 끝낸다. 응답 본문이 없고 상태코드 202 만 온다.
                                   오래 걸리는 작업에 쓰지만, 결과를 즉시 볼 수 없다.
    """
    cfg = current_app.config
    client_kwargs = {"region_name": cfg["AWS_REGION"]}
    # 키가 비어 있으면 넘기지 않는다 -> botocore 기본 자격증명 체인(IAM 역할 등)이 처리한다.
    if cfg["AWS_ACCESS_KEY_ID"]:
        client_kwargs["aws_access_key_id"] = cfg["AWS_ACCESS_KEY_ID"]
    if cfg["AWS_SECRET_ACCESS_KEY"]:
        client_kwargs["aws_secret_access_key"] = cfg["AWS_SECRET_ACCESS_KEY"]
    if cfg["AWS_SESSION_TOKEN"]:
        client_kwargs["aws_session_token"] = cfg["AWS_SESSION_TOKEN"]

    client = boto3.client("lambda", **client_kwargs)

    try:
        response = client.invoke(
            FunctionName=cfg["LAMBDA_FUNCTION_NAME"],
            InvocationType=invocation_type,
            # Payload 는 bytes 여야 한다. ensure_ascii=False 로 한글이 깨지지 않게 보낸다.
            Payload=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        )
    except (BotoCoreError, ClientError) as e:
        # 자격증명 없음, 함수 이름 오타, 권한 부족 등이 전부 여기로 온다.
        raise LambdaInvokeError(str(e)) from e

    # 비동기 호출은 응답 본문이 없다. 202 만 확인하고 끝낸다.
    if invocation_type == "Event":
        status = response.get("StatusCode")
        return {
            "ok": status == 202,
            "async": True,
            "status_code": status,
            "note": "비동기 호출이라 결과는 받지 않는다. CloudWatch 로그에서 확인해야 한다.",
        }

    body = response["Payload"].read().decode("utf-8")

    # FunctionError 는 '호출은 됐지만 함수 안에서 예외가 터진' 경우다.
    # HTTP 상태코드는 200 이므로 이걸 따로 확인하지 않으면 실패를 성공으로 착각한다.
    if response.get("FunctionError"):
        raise LambdaInvokeError(f"Lambda 함수 내부 오류: {body[:300]}")

    try:
        return json.loads(body)
    except json.JSONDecodeError as e:
        raise LambdaInvokeError(f"Lambda 응답을 JSON 으로 읽지 못했습니다: {body[:200]}") from e


def invoke_normalizer(payload, invocation_type=None):
    """설정된 모드에 맞춰 정규화 Lambda 를 호출한다."""
    cfg = current_app.config
    mode = cfg["LAMBDA_MODE"]

    if mode == LOCAL:
        return _invoke_local(payload)

    if mode == AWS:
        return _invoke_aws(payload, invocation_type or cfg["LAMBDA_INVOCATION_TYPE"])

    raise LambdaInvokeError(f"LAMBDA_MODE 값이 올바르지 않습니다: {mode!r} (local 또는 aws)")
