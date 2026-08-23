# app/views/noise.py
# 알람 노이즈 블루프린트. url_prefix="/noise" 로 등록된다.
# 시끄러운 알람을 순위로 보고, 지문별 억제 규칙을 건다.

from flask import (
    Blueprint, render_template, request, redirect, url_for, session, flash
)

from app import noise
from app.noise import NoiseError

noise_bp = Blueprint("noise", __name__)

# 집계 구간 선택지. 임의의 숫자를 받으면 전체 테이블을 훑게 된다.
HOUR_CHOICES = (24, 168, 720)
DEFAULT_HOURS = 168


@noise_bp.before_request
def require_login():
    """노이즈 화면은 로그인한 사용자만."""
    if not session.get("username"):
        flash("로그인이 필요합니다.", "error")
        return redirect(url_for("auth.login"))


def _hours():
    try:
        value = int(request.args.get("hours", DEFAULT_HOURS))
    except (TypeError, ValueError):
        return DEFAULT_HOURS
    return value if value in HOUR_CHOICES else DEFAULT_HOURS


# 최종 URL: /noise/
@noise_bp.route("/")
def index():
    """시끄러운 알람 순위와 억제 규칙."""
    hours = _hours()
    items, total, error, advice = [], None, None, None
    try:
        items = noise.ranking(hours)
        total = noise.summary(hours)
    except NoiseError as e:
        error = str(e)

    # 순위표는 '얼마나 시끄러운가' 까지만 말한다. 억제할지 정하려면
    # '실제 장애의 신호였던 적이 있는가' 가 필요하다.
    #
    # 장애 이력을 못 읽어도 순위표는 보여준다. 그것만으로도 쓸모가 있다.
    if items:
        try:
            links = noise.incident_links([i["fingerprint"] for i in items])
        except NoiseError:
            links = {}
        items = noise.advise(items, links)
        advice = noise.advice_summary(items)

    return render_template(
        "noise.html",
        items=items, total=total, error=error, advice=advice,
        hours=hours, choices=HOUR_CHOICES,
        noisy_enough=noise.NOISY_ENOUGH,
    )


# 최종 URL: /noise/rule
@noise_bp.route("/rule", methods=["POST"])
def rule():
    """억제 규칙을 저장한다."""
    try:
        noise.save_rule(
            fingerprint=request.form.get("fingerprint", ""),
            window_minutes=request.form.get("window_minutes", "0"),
            muted=bool(request.form.get("muted")),
            note=request.form.get("note", ""),
            author=session.get("username", ""),
            sample=request.form.get("sample", ""),
        )
        flash("억제 규칙을 저장했습니다.", "success")
    except NoiseError as e:
        flash(str(e), "error")
    return redirect(url_for("noise.index", hours=_hours()))


# 최종 URL: /noise/rule/delete
@noise_bp.route("/rule/delete", methods=["POST"])
def delete_rule():
    """억제 규칙을 지운다."""
    try:
        noise.delete_rule(request.form.get("fingerprint", ""))
        flash("억제 규칙을 지웠습니다.", "success")
    except NoiseError as e:
        flash(str(e), "error")
    return redirect(url_for("noise.index", hours=_hours()))
