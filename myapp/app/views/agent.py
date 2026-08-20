# app/views/agent.py
# AI 에이전트 채팅 페이지를 담당하는 블루프린트. app/__init__.py 에서 url_prefix="/agent" 로 등록되어
# 이 파일의 "/", "/ask", "/reset" 은 실제로 "/agent/", "/agent/ask", "/agent/reset" 이 된다.

import uuid

from flask import (
    Blueprint,
    render_template,
    request,
    redirect,
    url_for,
    session,
    flash,
    current_app,
)

import anthropic

from app.agent_core import run_agent, check_config, AgentNotConfigured

# 블루프린트 이름은 "agent" -> 엔드포인트는 agent.index, agent.ask, agent.reset 이 된다.
agent_bp = Blueprint("agent", __name__)


# ----------------------------------------------------------------------
# 대화 기록 보관소
# ----------------------------------------------------------------------
# 대화 내용을 session 에 넣지 않는 이유:
# Flask 의 session 은 '쿠키'라서 약 4KB 제한이 있다. 대화가 몇 번만 오가도 이 한도를 넘겨
# 조용히 잘리거나 에러가 난다. 그래서 여기서는 서버 메모리에 저장하고,
# session 에는 '어느 대화인지'를 가리키는 짧은 ID 하나만 넣는다.
#
# 주의: 이 방식은 학습용이다. 서버를 재시작하면 대화가 사라지고,
#       프로세스를 여러 개 띄우면(gunicorn -w 4 등) 요청마다 다른 메모리를 보게 된다.
#       실제 서비스라면 Redis 나 DB 에 저장해야 한다.
_CONVERSATIONS = {}


def _get_history():
    """현재 사용자의 대화 기록을 가져온다. 없으면 새로 만든다."""
    chat_id = session.get("chat_id")
    if not chat_id or chat_id not in _CONVERSATIONS:
        chat_id = uuid.uuid4().hex
        session["chat_id"] = chat_id
        _CONVERSATIONS[chat_id] = []
    return _CONVERSATIONS[chat_id]


# ----------------------------------------------------------------------
# 로그인 검사
# ----------------------------------------------------------------------
# before_request 를 '블루프린트에' 붙이면, 이 블루프린트의 모든 라우트가 실행되기 전에
# 이 함수가 먼저 돈다. 각 뷰 함수마다 로그인 검사를 복사해 넣을 필요가 없다.
# (app.before_request 였다면 앱 전체에 적용되어 메인/로그인 페이지까지 막혔을 것이다.)
@agent_bp.before_request
def require_login():
    """에이전트 기능은 로그인한 사용자만 쓸 수 있게 막는다."""
    if not session.get("username"):
        flash("에이전트를 사용하려면 먼저 로그인해 주세요.", "error")
        # None 이 아닌 값을 반환하면 Flask 는 뷰 함수를 실행하지 않고 이걸 응답으로 쓴다.
        return redirect(url_for("auth.login"))
    # None 을 반환하면(=아무것도 반환하지 않으면) 원래 뷰 함수가 정상 실행된다.


# 최종 URL: /agent/
@agent_bp.route("/")
def index():
    """채팅 화면을 보여준다."""
    return render_template(
        "agent.html",
        history=_get_history(),
        # 설정에 문제가 있으면 안내 문구가, 없으면 None 이 넘어간다.
        config_error=check_config(),
        model=current_app.config["AGENT_MODEL"],
        provider=current_app.config["AGENT_PROVIDER"],
    )


# 최종 URL: /agent/ask  (POST 전용)
@agent_bp.route("/ask", methods=["POST"])
def ask():
    """사용자 질문을 받아 에이전트를 실행하고, 결과를 기록한 뒤 채팅 화면으로 되돌린다."""
    user_message = request.form.get("message", "").strip()
    if not user_message:
        flash("질문을 입력해 주세요.", "error")
        return redirect(url_for("agent.index"))

    history = _get_history()

    try:
        # 모델에게 넘기는 대화 기록은 role/content 두 개만 있는 순수한 형태여야 한다.
        # 화면 표시용으로 덧붙인 정보(used_tools 등)는 걸러낸다.
        api_history = [{"role": m["role"], "content": m["content"]} for m in history]
        reply, used_tools = run_agent(api_history, user_message)

    except AgentNotConfigured as e:
        # 설정 부족(키 없음, 리전 없음, AWS 자격증명 없음 등)은 사용자가 고쳐야 하는 문제이므로
        # 원인을 그대로 보여준다.
        flash(f"에이전트를 실행할 수 없습니다: {e}", "error")
        return redirect(url_for("agent.index"))

    # 예외는 '좁은 것부터' 순서대로 잡는다. 맨 위에서 넓은 예외로 한 번에 잡아버리면
    # 재시도하면 되는 오류(429, 5xx)와 고쳐야 하는 오류(400, 401)를 구분할 수 없다.
    except anthropic.AuthenticationError:
        flash("API 키가 올바르지 않습니다. .env 의 ANTHROPIC_API_KEY 를 확인하세요.", "error")
        return redirect(url_for("agent.index"))
    except anthropic.RateLimitError:
        flash("요청이 너무 많습니다. 잠시 후 다시 시도해 주세요.", "error")
        return redirect(url_for("agent.index"))
    except anthropic.APIStatusError as e:
        flash(f"API 오류가 발생했습니다 (HTTP {e.status_code}).", "error")
        return redirect(url_for("agent.index"))
    except anthropic.APIConnectionError:
        flash("Anthropic 서버에 연결하지 못했습니다. 네트워크를 확인하세요.", "error")
        return redirect(url_for("agent.index"))

    # 대화 기록에 이번 턴을 추가한다.
    history.append({"role": "user", "content": user_message})
    history.append({"role": "assistant", "content": reply, "used_tools": used_tools})

    # POST 처리 후에는 redirect 한다(Post/Redirect/Get).
    # 이렇게 해야 사용자가 새로고침해도 같은 질문이 다시 전송되지 않는다.
    return redirect(url_for("agent.index"))


# 최종 URL: /agent/reset  (POST 전용)
@agent_bp.route("/reset", methods=["POST"])
def reset():
    """대화 기록을 비운다."""
    chat_id = session.pop("chat_id", None)
    if chat_id:
        _CONVERSATIONS.pop(chat_id, None)
    flash("대화를 초기화했습니다.", "success")
    return redirect(url_for("agent.index"))
