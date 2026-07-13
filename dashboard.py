#!/usr/bin/env python3
"""Streamlit dashboard for the read-only AWS portfolio audit."""

from __future__ import annotations

from datetime import datetime

import pandas as pd
import streamlit as st

from aws_audit import AwsAudit, DEFAULT_REGIONS, S3_TCO_BASELINE, demo_report, s3_tco


st.set_page_config(page_title="AWS Architecture Review", page_icon="☁️", layout="wide")


def money(value: float) -> str:
    return f"${value:,.0f}"


def latest_month_cost(report: dict) -> tuple[float, float | None]:
    rows = report["costs"].get("monthly", [])
    if not rows:
        return 0.0, None
    current = float(rows[-1]["total_usd"])
    previous = float(rows[-2]["total_usd"]) if len(rows) > 1 else None
    delta = None if not previous else ((current - previous) / previous) * 100
    return current, delta


def load_live(regions: list[str], profile: str, max_api_cost: float, s3_depth: str) -> dict:
    return AwsAudit(regions, profile or None, max_api_cost).collect(s3_depth)


st.title("AWS Architecture & FinOps Review")
st.caption("Read-only evidence for resource visibility, cost optimization, and operational risk")

with st.sidebar:
    st.header("Data source")
    mode = st.radio("Mode", ["Portfolio demo", "Live AWS"], horizontal=True)
    region_text = st.text_input("Regions", ",".join(DEFAULT_REGIONS))
    profile = st.text_input("AWS profile (optional)")
    max_api_cost = st.number_input("API cost guard (USD)", 0.01, 10.0, 1.0, 0.01)
    deep_s3 = st.checkbox("Deep S3 MPU byte scan", help="Lists every uploaded part; cost guard remains active")
    refresh = st.button("Refresh snapshot", type="primary", use_container_width=True)
    st.caption("Live collection uses read-only APIs. Optional services fail independently.")

if "report" not in st.session_state:
    st.session_state.report = demo_report()

if refresh:
    if mode == "Portfolio demo":
        st.session_state.report = demo_report()
    else:
        regions = [item.strip() for item in region_text.split(",") if item.strip()]
        with st.spinner("Collecting read-only AWS evidence..."):
            try:
                st.session_state.report = load_live(
                    regions, profile, max_api_cost, "deep" if deep_s3 else "basic"
                )
            except Exception as exc:  # Keep the last good dashboard snapshot visible.
                st.error(str(exc))

report = st.session_state.report
current_cost, cost_delta = latest_month_cost(report)
monthly_savings = sum(float(r.get("monthly_savings_usd", 0)) for r in report["recommendations"])

k1, k2, k3, k4, k5 = st.columns(5)
k1.metric("Resources", len(report["resources"]))
k2.metric("Latest cost", money(current_cost), None if cost_delta is None else f"{cost_delta:+.1f}%")
k3.metric("Savings backlog", money(monthly_savings) + "/mo")
k4.metric("Active findings", len(report["problems"]))
k5.metric("API estimate", f"${report['api_cost_guard']['estimated_usd']:.4f}")

overview, resources_tab, costs_tab, recommendations_tab, problems_tab, s3_tab = st.tabs(
    ["Overview", "Resources", "Costs", "Recommendations", "Problems", "S3 Cost Lab"]
)

with overview:
    left, right = st.columns((2, 1))
    with left:
        st.subheader("Monthly AWS cost")
        cost_rows = report["costs"].get("monthly", [])
        if cost_rows:
            st.bar_chart(pd.DataFrame(cost_rows), x="month", y="total_usd", color="#36C5F0")
        else:
            st.info("Cost Explorer data is unavailable or not permitted.")
    with right:
        st.subheader("Resource mix")
        summary = pd.DataFrame(
            [{"service": key, "count": value} for key, value in report["resource_summary"].items()]
        )
        if not summary.empty:
            st.bar_chart(summary, x="service", y="count", horizontal=True, color="#7C5CFC")

    st.subheader("Top savings opportunities")
    st.dataframe(report["recommendations"][:5], use_container_width=True, hide_index=True)

with resources_tab:
    st.subheader("Cross-region resource inventory")
    search = st.text_input("Filter resources", placeholder="name, service, type, region")
    resource_rows = report["resources"]
    if search:
        needle = search.casefold()
        resource_rows = [row for row in resource_rows if needle in " ".join(map(str, row.values())).casefold()]
    st.dataframe(resource_rows, use_container_width=True, hide_index=True)
    st.caption("Resource Explorer is preferred; EC2, RDS, and Lambda APIs are the fallback.")

with costs_tab:
    left, right = st.columns(2)
    with left:
        st.subheader("Service cost (six-month total)")
        service_rows = report["costs"].get("services", [])
        st.dataframe(service_rows, use_container_width=True, hide_index=True)
    with right:
        st.subheader("Cost anomalies (90 days)")
        anomalies = report["costs"].get("anomalies", [])
        st.dataframe(anomalies, use_container_width=True, hide_index=True)
    st.warning("The current month is estimated and can be incomplete. Validate commitments with amortized cost views.")

with recommendations_tab:
    st.subheader("Cost Optimization Hub backlog")
    st.dataframe(report["recommendations"], use_container_width=True, hide_index=True)
    st.caption(
        "AWS-generated estimates account for account-specific pricing terms when Cost Optimization Hub is enabled."
    )

with problems_tab:
    st.subheader("Operational signals")
    if report["problems"]:
        for finding in report["problems"]:
            with st.expander(f"[{finding['severity'].upper()}] {finding['title']}"):
                st.write(finding["detail"])
                st.code(finding.get("resource", ""), language=None)
    else:
        st.success("No active CloudWatch alarms or EC2 status findings were returned.")

with s3_tab:
    st.subheader("Bucket cost posture")
    s3_buckets = report["s3"].get("buckets", [])
    bucket_table = [
        {
            "bucket": row["name"],
            "region": row["region"],
            "storage_tb": row["total_storage_tb"],
            "objects": row["object_count"],
            "versioning": row["versioning"],
            "lifecycle_rules": row["lifecycle_rules"],
            "incomplete_mpu": row["incomplete_mpu_count"],
            "incomplete_gb": row["incomplete_mpu_gb"],
            "access_logging": row["access_logging"],
        }
        for row in s3_buckets
    ]
    st.dataframe(bucket_table, use_container_width=True, hide_index=True)

    left, right = st.columns(2)
    with left:
        st.subheader("S3 findings")
        st.dataframe(report["s3"].get("findings", []), use_container_width=True, hide_index=True)
    with right:
        st.subheader("S3 usage-type cost")
        st.dataframe(report["s3"].get("costs", []), use_container_width=True, hide_index=True)

    st.divider()
    st.subheader("Storage-class TCO simulator")
    st.caption(
        "Editable 2026-06 ap-northeast-2 baseline from the original S3 review. "
        "Verify current AWS pricing before an implementation decision."
    )
    c1, c2, c3, c4 = st.columns(4)
    storage_tb = c1.number_input("Storage (TB)", 0.1, 10000.0, 26.0, 1.0)
    object_millions = c2.number_input("Objects (million)", 0.001, 1000.0, 1.46, 0.1)
    reads = c3.number_input("Full reads / month", 0.0, 100.0, 1.0, 0.25)
    cold_fraction = c4.slider("I-T cold fraction", 0, 100, 0, 5) / 100
    tco_rows = s3_tco(storage_tb, int(object_millions * 1_000_000), reads, cold_fraction)
    st.bar_chart(pd.DataFrame(tco_rows), x="storage_class", y="monthly_usd", color="#2EB67D")
    st.dataframe(tco_rows, use_container_width=True, hide_index=True)
    with st.expander("Edit pricing assumptions"):
        pricing = {
            key: st.number_input(key, min_value=0.0, value=float(value), format="%.5f")
            for key, value in S3_TCO_BASELINE.items()
        }
        adjusted = s3_tco(
            storage_tb, int(object_millions * 1_000_000), reads, cold_fraction, pricing
        )
        st.dataframe(adjusted, use_container_width=True, hide_index=True)

with st.expander("Collection coverage and limitations"):
    st.markdown(
        """
        - Resource inventory: Resource Explorer, with direct EC2/RDS/Lambda fallback.
        - Cost: Cost Explorer monthly unblended cost and Cost Anomaly Detection.
        - Optimization: Cost Optimization Hub recommendations, when enrolled.
        - Operations: active CloudWatch alarms and EC2 instance/system status checks.
        - All optional-service permission or enrollment failures stay visible under diagnostics.
        """
    )
    if report["errors"]:
        st.dataframe(report["errors"], use_container_width=True, hide_index=True)

st.caption(
    f"Mode: {report['mode']} · Generated: {datetime.fromisoformat(report['generated_at']).astimezone():%Y-%m-%d %H:%M:%S %Z}"
)
