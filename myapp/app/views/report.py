# app/views/report.py
# 기간별 운영 리포트 화면과 Markdown 내려받기.
# app/__init__.py 에서 url_prefix="/report" 로 등록된다.

from flask import (
    Blueprint, render_template, request, redirect, url_for,
    session, flash, Response,
)

from urllib.parse import quote

from app import delivery, msr
from app.customer import names as customer_names, CustomerError
from app.delivery import DeliveryError
from app.msr import MsrError
from app.report import collect, to_markdown, resource_diff_summary, generate_summary, ReportError
from app.report_pptx import build as build_pptx
from app.agent_core import check_config as agent_check

report_bp = Blueprint("report", __name__)

ALLOWED_DAYS = (1, 7, 30)


@report_bp.before_request
def require_login():
    if not session.get("username"):
        flash("리포트를 보려면 먼저 로그인해 주세요.", "error")
        return redirect(url_for("auth.login"))


def _days():
    try:
        days = int(request.args.get("days", 7))
    except ValueError:
        days = 7
    return days if days in ALLOWED_DAYS else 7


def _build(days, with_summary):
    """리포트 자료를 모은다. 요약은 요청했을 때만 만든다(API 호출이라 느리고 비용이 든다)."""
    data = collect(days)
    rdiff = resource_diff_summary()
    summary = None
    if with_summary:
        try:
            summary = generate_summary(data, rdiff)
        except Exception as e:
            # 요약에 실패해도 리포트 본문은 나와야 한다.
            summary = None
            flash(f"AI 요약을 만들지 못했습니다: {type(e).__name__}", "error")
    return data, rdiff, summary


@report_bp.route("/")
def index():
    days = _days()
    # ?summary=1 일 때만 에이전트를 부른다.
    want_summary = request.args.get("summary") == "1"

    try:
        data, rdiff, summary = _build(days, want_summary)
        error = None
    except ReportError as e:
        data, rdiff, summary, error = None, None, None, str(e)

    return render_template(
        "report.html",
        data=data,
        rdiff=rdiff,
        summary=summary,
        error=error,
        days=days,
        allowed_days=ALLOWED_DAYS,
        want_summary=want_summary,
        # 에이전트 설정이 없으면 요약 버튼을 비활성화한다.
        agent_ready=agent_check() is None,
    )


@report_bp.route("/download")
def download():
    """Markdown 파일로 내려받는다."""
    days = _days()
    want_summary = request.args.get("summary") == "1"

    try:
        data, rdiff, summary = _build(days, want_summary)
    except ReportError as e:
        flash(str(e), "error")
        return redirect(url_for("report.index", days=days))

    text = to_markdown(data, rdiff, summary)
    filename = f"report-{data['end'].strftime('%Y%m%d')}-{days}d.md"

    # Content-Disposition: attachment 를 주면 브라우저가 화면에 그리지 않고 저장한다.
    # mimetype 에 charset 을 직접 쓰면 Flask 가 또 붙여서
    # "text/markdown; charset=utf-8; charset=utf-8" 이 된다.
    # 종류만 주고 인코딩은 Flask 에 맡긴다.
    return Response(
        text,
        mimetype="text/markdown",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@report_bp.route("/download.pptx")
def download_pptx():
    """리포트를 PowerPoint 파일로 내려받는다."""
    days = _days()
    want_summary = request.args.get("summary") == "1"

    try:
        data, rdiff, summary = _build(days, want_summary)
    except ReportError as e:
        flash(str(e), "error")
        return redirect(url_for("report.index", days=days))

    buf = build_pptx(data, rdiff, summary)
    filename = f"report-{data['end'].strftime('%Y%m%d')}-{days}d.pptx"

    return Response(
        buf.getvalue(),
        mimetype="application/vnd.openxmlformats-officedocument.presentationml.presentation",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


# ----------------------------------------------------------------------
# SLA
# ----------------------------------------------------------------------
# 리포트 블루프린트에 붙인 이유: 최초 대응 시간은 월간 고객사 리포트에
# 들어가는 항목이다. 별도 블루프린트로 빼면 같은 독자·같은 주기를 가진
# 화면이 둘로 갈라진다.

SLA_DAYS = (7, 30, 90)


def _sla_days():
    try:
        value = int(request.args.get("days", 30))
    except (TypeError, ValueError):
        return 30
    return value if value in SLA_DAYS else 30


# 최종 URL: /report/sla
@report_bp.route("/sla")
def sla():
    """고객사별 최초 대응 시간과 목표."""
    from app import sla as sla_module
    from app.sla import SlaError, SEVERITIES, SUGGESTED
    from app.customer import names, CustomerError

    days = _sla_days()
    error, all_names, data, goals = None, [], None, {}

    try:
        all_names = names()
    except CustomerError as e:
        error = str(e)

    selected = request.args.get("customer") or (all_names[0] if all_names else "")
    if selected and selected not in all_names:
        flash(f"등록되지 않은 고객사입니다: {selected}", "error")
        selected = all_names[0] if all_names else ""

    if selected and not error:
        try:
            data = sla_module.measure(selected, days)
            goals = sla_module.targets(selected)
        except SlaError as e:
            error = str(e)

    return render_template(
        "report_sla.html",
        names=all_names, selected=selected, days=days, choices=SLA_DAYS,
        data=data, goals=goals, error=error,
        severities=SEVERITIES, suggested=SUGGESTED,
    )


# 최종 URL: /report/sla/target
@report_bp.route("/sla/target", methods=["POST"])
def sla_target():
    """SLA 목표를 저장한다."""
    from app import sla as sla_module
    from app.sla import SlaError

    customer = request.form.get("customer", "")
    try:
        sla_module.save_target(
            customer=customer,
            severity=request.form.get("severity", ""),
            minutes=request.form.get("minutes", "0"),
            note=request.form.get("note", ""),
        )
        flash("목표를 저장했습니다.", "success")
    except SlaError as e:
        flash(str(e), "error")

    return redirect(url_for("report.sla", customer=customer, days=_sla_days()))


# ----------------------------------------------------------------------
# 월간 서비스 리뷰 (MSR)
# ----------------------------------------------------------------------
# 새 블루프린트를 만들지 않고 report 에 붙였다. 접근 정책이 같고,
# 성격도 같다 - 둘 다 "모은 숫자를 문서로 내보내는" 화면이다.
#
# 다른 점은 축이다. /report/ 는 전체를 기간으로 자르고,
# 여기는 고객사 하나를 달로 자른다.


def _msr_args():
    """고객사와 연·월을 쿼리스트링에서 꺼낸다.

    연·월이 숫자가 아니면 가장 최근 자료가 있는 달로 돌린다.
    빈 화면을 내는 것보다 낫다.
    """
    customer_name = request.args.get("customer", "")
    try:
        year = int(request.args.get("year", ""))
        month = int(request.args.get("month", ""))
    except ValueError:
        year = month = 0
    if not (1 <= month <= 12):
        year = month = 0
    return customer_name, year, month


@report_bp.route("/msr")
def msr_page():
    """고객사 하나의 한 달치를 한 화면에."""
    from app.customer import names as customer_names, CustomerError

    error, all_names, months, data = None, [], [], None
    try:
        all_names = customer_names()
    except CustomerError as e:
        error = str(e)

    try:
        months = msr.available_months()
    except MsrError as e:
        error = error or str(e)

    customer_name, year, month = _msr_args()
    if all_names and customer_name not in all_names:
        customer_name = all_names[0]
    if not year and months:
        year, month = months[0]["year"], months[0]["month"]

    if customer_name and year and not error:
        try:
            data = msr.collect(customer_name, year, month)
        except MsrError as e:
            error = str(e)

    from app.work import STATUS_LABEL as work_labels

    # 이 달치를 실제로 보냈나. 자료를 만드는 것과 보내는 것은 다른 일이고,
    # 지금까지는 뒤쪽이 아무 데도 안 남았다.
    ref = f"{year}-{month:02d}" if year and month else ""
    sent = [d for d in delivery.for_ref("msr", ref)
            if d["customer"] == customer_name] if ref else []

    return render_template(
        "msr.html",
        names=all_names, months=months, error=error,
        customer=customer_name, year=year, month=month, data=data,
        sent=sent, msr_ref=ref,
        channels=delivery.CHANNELS,
        suggested=delivery.suggest_recipients(customer_name)
                  if customer_name else "",
        # 상태 이름은 app/work.py 것을 그대로 쓴다. 여기서 따로 적으면
        # 작업 화면과 보고서가 같은 상태를 다른 말로 부르게 된다.
        work_labels=work_labels,
    )


@report_bp.route("/msr/download.pptx")
def msr_download():
    """월간 리뷰 자료를 PowerPoint 로 내려받는다."""
    from app.msr_pptx import build as build_msr

    customer_name, year, month = _msr_args()
    if not customer_name or not year:
        flash("고객사와 연·월을 골라 주세요.", "error")
        return redirect(url_for("report.msr_page"))

    try:
        data = msr.collect(customer_name, year, month)
    except MsrError as e:
        flash(str(e), "error")
        return redirect(url_for("report.msr_page", customer=customer_name))

    buf = build_msr(data)

    # 파일 이름에 고객사 이름(한글)이 들어간다. HTTP 헤더는 latin-1 이라
    # 그대로 넣으면 깨진다. 엑셀 내려받기와 같은 방식으로 처리한다.
    name = f"MSR-{customer_name}-{year}{month:02d}.pptx"
    ascii_name = f"msr-{year}{month:02d}.pptx"
    disposition = (
        f'attachment; filename="{ascii_name}"; '
        f"filename*=UTF-8''{quote(name)}"
    )
    return Response(
        buf.getvalue(),
        mimetype="application/vnd.openxmlformats-officedocument.presentationml.presentation",
        headers={"Content-Disposition": disposition},
    )


# 최종 URL: /report/deliveries
@report_bp.route("/deliveries")
def deliveries():
    """무엇을 누구에게 언제 보냈나.

    다운로드 기록이 아니다. 파일을 내려받은 것을 발송으로 세면 확인하려고
    열어본 것까지 전부 발송이 되고, 그러면 이 화면이 아무 뜻도 없어진다.
    사람이 보냈다고 누른 것만 여기 있다.
    """
    error, items, stats, names = None, [], None, []
    selected = request.args.get("customer", "")

    try:
        items = delivery.recent(selected)
        stats = delivery.summary()
    except DeliveryError as e:
        error = str(e)

    try:
        names = customer_names()
    except CustomerError:
        names = []

    return render_template(
        "report_deliveries.html",
        items=items, stats=stats, error=error,
        customers=names, selected=selected,
        kinds=delivery.KINDS, channels=delivery.CHANNELS,
        suggested=delivery.suggest_recipients(selected) if selected else "",
    )


# 최종 URL: /report/deliveries/record
@report_bp.route("/deliveries/record", methods=["POST"])
def deliveries_record():
    """보냈다는 기록을 남긴다."""
    name = request.form.get("customer", "")
    try:
        delivery.record(
            customer=name,
            kind=request.form.get("kind", ""),
            channel=request.form.get("channel", ""),
            sent_by=session.get("username", ""),
            ref=request.form.get("ref", ""),
            title=request.form.get("title", ""),
            recipients=request.form.get("recipients", ""),
            note=request.form.get("note", ""),
        )
        flash("발송을 기록했습니다.", "success")
    except DeliveryError as e:
        flash(str(e), "error")
    return redirect(request.form.get("back")
                    if (request.form.get("back") or "").startswith("/")
                    else url_for("report.deliveries", customer=name))
