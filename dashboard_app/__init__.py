"""Flask application for the local AWS review dashboard."""

from __future__ import annotations

from typing import Any, Callable

from flask import Flask, redirect, render_template, request, url_for

from aws_audit import AwsAudit, DEFAULT_REGIONS, S3_TCO_BASELINE, demo_report
from dashboard_app.presentation import build_dashboard, format_money, format_number


DEFAULT_FORM = {
    "mode": "demo",
    "regions": ",".join(DEFAULT_REGIONS),
    "profile": "",
    "max_api_cost": 1.0,
    "deep_s3": False,
}

DEFAULT_TCO = {
    "storage_tb": 26.0,
    "object_millions": 1.46,
    "reads": 1.0,
    "cold_percent": 0.0,
}


def _bounded_float(raw: str | None, default: float, minimum: float, maximum: float) -> float:
    try:
        return min(max(float(raw), minimum), maximum)
    except (TypeError, ValueError):
        return default


def _tco_inputs(args: dict[str, Any]) -> dict[str, Any]:
    values = {
        "storage_tb": _bounded_float(args.get("storage_tb"), 26.0, 0.1, 10_000.0),
        "object_millions": _bounded_float(args.get("object_millions"), 1.46, 0.001, 1_000.0),
        "reads": _bounded_float(args.get("reads"), 1.0, 0.0, 100.0),
        "cold_percent": _bounded_float(args.get("cold_percent"), 0.0, 0.0, 100.0),
    }
    values["rates"] = {
        key: _bounded_float(args.get(key), float(default), 0.0, 1_000.0)
        for key, default in S3_TCO_BASELINE.items()
    }
    return values


def create_app(collector_factory: Callable[..., AwsAudit] = AwsAudit) -> Flask:
    app = Flask(__name__)
    # ponytail: process memory is enough for this single-user local tool; add persistence if hosted.
    app.config.update(
        DASHBOARD_REPORT=demo_report(),
        DASHBOARD_FORM=dict(DEFAULT_FORM),
        DASHBOARD_NOTICE=None,
    )
    app.jinja_env.filters["money"] = format_money
    app.jinja_env.filters["number"] = format_number

    @app.get("/")
    def index():
        return render_template(
            "dashboard.html",
            data=build_dashboard(
                app.config["DASHBOARD_REPORT"],
                _tco_inputs(request.args),
                request.args.get("resource_query", "").strip(),
            ),
            form=app.config["DASHBOARD_FORM"],
            notice=app.config.pop("DASHBOARD_NOTICE", None),
        )

    @app.post("/refresh")
    def refresh():
        mode = request.form.get("mode", "demo")
        mode = mode if mode in {"demo", "live"} else "demo"
        regions = [item.strip() for item in request.form.get("regions", "").split(",") if item.strip()]
        if not regions:
            regions = list(DEFAULT_REGIONS)
        profile = request.form.get("profile", "").strip()
        max_api_cost = _bounded_float(request.form.get("max_api_cost"), 1.0, 0.01, 10.0)
        deep_s3 = request.form.get("deep_s3") == "on"
        app.config["DASHBOARD_FORM"] = {
            "mode": mode,
            "regions": ",".join(regions),
            "profile": profile,
            "max_api_cost": max_api_cost,
            "deep_s3": deep_s3,
        }

        if mode == "demo":
            app.config["DASHBOARD_REPORT"] = demo_report()
            app.config["DASHBOARD_NOTICE"] = ("success", "예시 데이터를 새로 불러왔습니다.")
        else:
            try:
                report = collector_factory(regions, profile or None, max_api_cost).collect(
                    "deep" if deep_s3 else "basic"
                )
            except Exception as exc:  # Keep the last good snapshot visible.
                app.config["DASHBOARD_NOTICE"] = ("error", f"AWS 조회에 실패했습니다: {exc}")
            else:
                app.config["DASHBOARD_REPORT"] = report
                app.config["DASHBOARD_NOTICE"] = ("success", "AWS 읽기 전용 조회를 완료했습니다.")

        return redirect(url_for("index") + "#overview")

    return app
