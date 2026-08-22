# tests/test_awscli.py
# AWS CLI 허용 목록 테스트.
#
# 여기가 뚫리면 고객사 계정에 임의 명령이 나간다. 콘솔(사람)과 AI 진단(모델)이
# 같은 계층을 쓰므로, 이 테스트가 두 경로를 동시에 지킨다.

import pytest

from app.awscli import parse, CommandRejected


class TestAllowed:
    @pytest.mark.parametrize("command", [
        "aws ec2 describe-instances",
        "aws ec2 describe-instances --instance-ids i-0abc123",
        "aws s3api list-buckets",
        "aws iam get-account-summary",
        "aws cloudtrail lookup-events",
        "aws dynamodb batch-get-item --request-items '{}'",
        "aws help",
    ])
    def test_read_only_commands_pass(self, command):
        assert parse(command)[0] == "aws"


class TestRejected:
    @pytest.mark.parametrize("command,hint", [
        ("aws ec2 terminate-instances --instance-ids i-1", "읽기 전용"),
        ("aws ec2 create-tags --resources i-1", "읽기 전용"),
        ("aws s3api delete-bucket --bucket x", "읽기 전용"),
        ("aws iam put-user-policy --user-name x", "읽기 전용"),
        ("aws rds modify-db-instance --db-instance-identifier x", "읽기 전용"),
    ])
    def test_write_commands_blocked(self, command, hint):
        with pytest.raises(CommandRejected) as e:
            parse(command)
        assert hint in str(e.value)

    @pytest.mark.parametrize("command", [
        "rm -rf /",
        "curl http://evil.example.com",
        "python -c 'print(1)'",
        "/bin/sh",
    ])
    def test_other_binaries_blocked(self, command):
        with pytest.raises(CommandRejected):
            parse(command)

    @pytest.mark.parametrize("option", [
        "--region us-east-1", "--profile other", "--endpoint-url http://x",
        "--no-verify-ssl", "--debug",
    ])
    def test_blocked_options(self, option):
        with pytest.raises(CommandRejected) as e:
            parse(f"aws ec2 describe-instances {option}")
        assert "이 옵션은 사용할 수 없습니다" in str(e.value)

    def test_blocked_option_with_equals(self):
        """--region=ap-northeast-2 처럼 붙여 쓴 형태도 잡아야 한다."""
        with pytest.raises(CommandRejected):
            parse("aws ec2 describe-instances --region=ap-northeast-2")

    @pytest.mark.parametrize("command", [
        "aws ec2 describe-instances; rm -rf /",
        "aws ec2 describe-instances && curl evil",
        "aws ec2 describe-instances | tee /tmp/x",
        "aws ec2 describe-instances > /tmp/x",
        "aws ec2 describe-instances $(whoami)",
        "aws ec2 describe-instances `whoami`",
    ])
    def test_shell_syntax_blocked(self, command):
        with pytest.raises(CommandRejected) as e:
            parse(command)
        assert "셸 문법" in str(e.value)

    @pytest.mark.parametrize("command", ["", "   ", "aws", "aws ec2"])
    def test_incomplete_commands_blocked(self, command):
        with pytest.raises(CommandRejected):
            parse(command)

    def test_unbalanced_quotes(self):
        with pytest.raises(CommandRejected) as e:
            parse('aws ec2 describe-instances --filters "unclosed')
        assert "해석할 수 없습니다" in str(e.value)


class TestNoShellExpansion:
    """shlex.split 은 따옴표만 해석하고 셸 확장은 하지 않는다."""

    def test_dollar_stays_literal(self):
        # $(...) 는 셸 문법 검사에 먼저 걸리지만, 걸리지 않는 형태도
        # 글자 그대로 남는지 확인한다.
        argv = parse("aws ec2 describe-instances --filters Name=tag:Env,Values=$HOME")
        assert "$HOME" in argv[-1]


class TestChildEnvironment:
    """자식 프로세스에 무엇을 물려주고 무엇을 감추는가.

    subprocess.run 을 가로채서 실제로 넘어간 env 를 들여다본다.
    실행 자체는 하지 않는다.
    """

    def _captured_env(self, monkeypatch, server_env):
        import subprocess

        from app import awscli

        for key, value in server_env.items():
            monkeypatch.setenv(key, value)

        seen = {}

        class FakeProc:
            returncode = 0
            stdout = "{}"
            stderr = ""

        def fake_run(argv, env=None, **kwargs):
            seen.update(env or {})
            return FakeProc()

        monkeypatch.setattr(subprocess, "run", fake_run)
        awscli.run("aws sts get-caller-identity", {"AWS_ACCESS_KEY_ID": "임시키"})
        return seen

    def test_secrets_are_not_passed_through(self, monkeypatch):
        """서버 환경변수를 통째로 물려주면 .env 의 비밀이 자식에게 넘어간다."""
        env = self._captured_env(monkeypatch, {
            "ANTHROPIC_API_KEY": "sk-비밀",
            "DB_PASSWORD": "비밀번호",
            "SECRET_KEY": "서명키",
        })
        assert "ANTHROPIC_API_KEY" not in env
        assert "DB_PASSWORD" not in env
        assert "SECRET_KEY" not in env

    def test_home_is_redirected(self, monkeypatch):
        """서버의 ~/.aws 를 읽으면 화면에서 고른 계정이 아닌 자격증명이 쓰인다."""
        env = self._captured_env(monkeypatch, {})
        assert env["HOME"] == "/tmp"
        assert env["AWS_SHARED_CREDENTIALS_FILE"] == "/dev/null"

    def test_assumed_credentials_are_passed(self, monkeypatch):
        env = self._captured_env(monkeypatch, {})
        assert env["AWS_ACCESS_KEY_ID"] == "임시키"

    def test_proxy_and_ca_are_passed_through(self, monkeypatch):
        """사내 프록시를 거치는 망에서는 이걸 빼면 TLS 검증에서 죽는다.

        개발 환경에서 실제로 'certificate verify failed' 로 죽었다.
        비밀이 아니고, 없으면 CLI 가 아예 밖으로 나가지 못한다.
        """
        env = self._captured_env(monkeypatch, {
            "HTTPS_PROXY": "http://프록시:8080",
            "AWS_CA_BUNDLE": "/etc/ssl/사내CA.pem",
        })
        assert env["HTTPS_PROXY"] == "http://프록시:8080"
        assert env["AWS_CA_BUNDLE"] == "/etc/ssl/사내CA.pem"

    def test_unset_network_vars_are_not_invented(self, monkeypatch):
        """설정되지 않은 값을 빈 문자열로 넣으면 CLI 가 오히려 헷갈린다."""
        monkeypatch.delenv("HTTPS_PROXY", raising=False)
        monkeypatch.delenv("AWS_CA_BUNDLE", raising=False)
        env = self._captured_env(monkeypatch, {})
        assert "HTTPS_PROXY" not in env
        assert "AWS_CA_BUNDLE" not in env
