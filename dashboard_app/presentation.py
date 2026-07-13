"""Convert the audit report into human-readable dashboard rows."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Iterable

from aws_audit import S3_TCO_BASELINE, s3_tco


ACTION_LABELS = {
    "Rightsize": "사용량에 맞게 사양 조정",
    "Stop": "사용하지 않는 리소스 중지",
    "Upgrade": "새 세대로 업그레이드",
    "PurchaseSavingsPlans": "Savings Plans 구매 검토",
    "PurchaseReservedInstances": "예약 인스턴스 구매 검토",
    "MigrateToGraviton": "Graviton으로 이전",
    "Delete": "사용하지 않는 리소스 삭제",
    "ScaleIn": "리소스 수 축소",
    "Review": "검토 필요",
}

EFFORT_LABELS = {
    "VeryLow": "매우 낮음",
    "Low": "낮음",
    "Medium": "보통",
    "High": "높음",
    "VeryHigh": "매우 높음",
    "unknown": "확인 필요",
}

RESOURCE_TYPE_LABELS = {
    "Ec2Instance": "EC2 가상 서버",
    "Ec2AutoScalingGroup": "EC2 Auto Scaling 그룹",
    "EbsVolume": "EBS 디스크",
    "LambdaFunction": "Lambda 함수",
    "EcsService": "ECS 서비스",
    "RdsDbInstance": "RDS 데이터베이스",
    "RdsDbInstanceStorage": "RDS 저장 공간",
    "NatGateway": "NAT Gateway",
    "DynamoDBTable": "DynamoDB 테이블",
}

SERVICE_LABELS = {
    "Amazon Simple Storage Service": "S3 객체 스토리지",
    "Amazon Relational Database Service": "RDS 데이터베이스",
    "Amazon Elastic Compute Cloud": "EC2 가상 서버",
}

STATE_LABELS = {
    "active": "사용 중",
    "available": "사용 가능",
    "discovered": "발견됨",
    "running": "실행 중",
    "stopped": "중지됨",
}

SEVERITY_LABELS = {"high": "높음", "medium": "보통", "info": "참고"}

S3_CLASS_LABELS = {
    "STANDARD": "Standard(표준)",
    "STANDARD_IA": "Standard-IA(저빈도)",
    "GLACIER_IR": "Glacier Instant Retrieval(즉시 인출 보관)",
    "INTELLIGENT_TIERING": "Intelligent-Tiering(자동 계층화)",
}

PRICE_LABELS = {
    "standard_storage": "Standard 저장 비용($/GB-월)",
    "ia_storage": "Standard-IA 저장 비용($/GB-월)",
    "gir_storage": "Glacier Instant Retrieval 저장 비용($/GB-월)",
    "standard_retrieval": "Standard 데이터 인출 비용($/GB)",
    "ia_retrieval": "Standard-IA 데이터 인출 비용($/GB)",
    "gir_retrieval": "Glacier Instant Retrieval 데이터 인출 비용($/GB)",
    "standard_get_per_1000": "Standard 읽기 요청 비용($/1,000건)",
    "ia_get_per_1000": "Standard-IA 읽기 요청 비용($/1,000건)",
    "gir_get_per_1000": "Glacier Instant Retrieval 읽기 요청 비용($/1,000건)",
    "it_monitor_per_1000": "Intelligent-Tiering 객체 모니터링 비용($/1,000개)",
}

S3_CHECKS = {
    "incomplete_multipart": ("미완료 대용량 업로드", "완료하거나 중단해 불필요한 저장 비용을 없앱니다."),
    "abort_lifecycle": ("미완료 업로드 자동 정리 규칙", "일정 기간이 지난 업로드를 자동 삭제하도록 Lifecycle 규칙을 설정합니다."),
    "noncurrent_versions": ("이전 객체 버전 만료 규칙", "보관이 필요 없는 이전 버전을 자동 만료하도록 Lifecycle 규칙을 설정합니다."),
    "access_evidence": ("객체 접근 기록", "사용 패턴을 판단할 수 있도록 서버 접근 로그나 대체 추적 수단을 검토합니다."),
}


def format_money(value: Any, decimals: int = 0) -> str:
    try:
        return f"${float(value):,.{int(decimals)}f}"
    except (TypeError, ValueError):
        return "$0"


def format_number(value: Any, decimals: int = 0) -> str:
    try:
        return f"{float(value):,.{int(decimals)}f}"
    except (TypeError, ValueError):
        return "0"


def _yes_no(value: bool) -> str:
    return "예" if value else "아니오"


def _bar_rows(rows: Iterable[dict[str, Any]], value_key: str) -> list[dict[str, Any]]:
    result = list(rows)
    maximum = max((float(row.get(value_key, 0)) for row in result), default=0.0)
    return [
        {**row, "bar_percent": 0 if not maximum else round(float(row.get(value_key, 0)) / maximum * 100, 1)}
        for row in result
    ]


def _latest_cost(report: dict[str, Any]) -> tuple[float, float | None, bool]:
    rows = report.get("costs", {}).get("monthly", [])
    if not rows:
        return 0.0, None, False
    current = float(rows[-1]["total_usd"])
    estimated = bool(rows[-1].get("estimated"))
    previous = float(rows[-2]["total_usd"]) if len(rows) > 1 else None
    delta = None if estimated or not previous else ((current - previous) / previous) * 100
    return current, delta, estimated


def _recommendations(rows: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    result = []
    for row in rows:
        action = ACTION_LABELS.get(row.get("action"), row.get("action") or "검토 필요")
        resource_type = RESOURCE_TYPE_LABELS.get(
            row.get("resource_type"), row.get("resource_type") or "알 수 없음"
        )
        source = {
            "ComputeOptimizer": "AWS Compute Optimizer",
            "CostExplorer": "AWS Cost Explorer",
        }.get(row.get("source"), row.get("source") or "AWS Cost Optimization Hub")
        savings = float(row.get("monthly_savings_usd", 0))
        percentage = float(row.get("savings_percentage", 0))
        result.append(
            {
                "action": action,
                "resource_type": resource_type,
                "resource_id": row.get("resource_id", "-"),
                "region": row.get("region", "-"),
                "monthly_cost_usd": float(row.get("monthly_cost_usd", 0)),
                "monthly_savings_usd": savings,
                "savings_percentage": percentage,
                "effort": EFFORT_LABELS.get(row.get("effort"), row.get("effort") or "확인 필요"),
                "restart_needed": _yes_no(bool(row.get("restart_needed"))),
                "rollback_possible": _yes_no(bool(row.get("rollback_possible"))),
                "source": source,
                "reason": f"{source}가 {action} 시 월 {format_money(savings, 2)} ({percentage:.1f}%) 절감 가능성이 있다고 판단",
            }
        )
    return result


def _s3_findings(findings: Iterable[dict[str, Any]], buckets: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    bucket_by_name = {row["name"]: row for row in buckets}
    result = []
    for finding in findings:
        check = finding.get("check", "")
        title, action = S3_CHECKS.get(check, (check, "세부 내용을 확인합니다."))
        bucket = bucket_by_name.get(finding.get("bucket"), {})
        if check == "incomplete_multipart":
            reason = f"미완료 업로드 {bucket.get('incomplete_mpu_count', 0):,}건"
            size = bucket.get("incomplete_mpu_gb")
            if size is not None:
                reason += f", 약 {size:,.2f}GB가 완료 또는 중단 전까지 과금됩니다."
        elif check == "abort_lifecycle":
            reason = "오래된 미완료 업로드를 자동 정리하는 규칙이 없습니다."
        elif check == "noncurrent_versions":
            reason = "버전 관리는 켜져 있지만 이전 버전을 만료하는 규칙이 없습니다."
        elif check == "access_evidence":
            reason = "접근 기록이 꺼져 있어 과거 객체 읽기 패턴을 확인하기 어렵습니다."
        else:
            reason = finding.get("detail", "세부 내용을 확인합니다.")
        result.append(
            {
                "severity": SEVERITY_LABELS.get(finding.get("severity"), finding.get("severity") or "확인"),
                "bucket": finding.get("bucket", "-"),
                "title": title,
                "reason": reason,
                "action": action,
            }
        )
    return result


def build_dashboard(
    report: dict[str, Any], tco_inputs: dict[str, Any], resource_query: str = ""
) -> dict[str, Any]:
    current_cost, cost_delta, current_estimated = _latest_cost(report)
    recommendations = _recommendations(report.get("recommendations", []))
    resources = [
        {
            "service": str(row.get("service", "unknown")).upper(),
            "type": row.get("type", "-"),
            "name": row.get("name", "-"),
            "arn": row.get("arn", "-"),
            "region": row.get("region", "-"),
            "state": STATE_LABELS.get(row.get("state"), row.get("state", "-")),
            "source": row.get("source", "-"),
        }
        for row in report.get("resources", [])
    ]
    resource_total = len(resources)
    if resource_query:
        needle = resource_query.casefold()
        resources = [
            row for row in resources if needle in " ".join(str(value) for value in row.values()).casefold()
        ]
    resource_match_total = len(resources)
    resources = resources[:500]
    monthly = _bar_rows(
        [
            {
                "month": row["month"],
                "total_usd": float(row["total_usd"]),
                "estimated": bool(row.get("estimated")),
            }
            for row in report.get("costs", {}).get("monthly", [])
        ],
        "total_usd",
    )
    resource_mix_rows = sorted(
        (
            {"service": service.upper(), "count": count}
            for service, count in report.get("resource_summary", {}).items()
        ),
        key=lambda row: row["count"],
        reverse=True,
    )
    if len(resource_mix_rows) > 10:
        resource_mix_rows = resource_mix_rows[:10] + [
            {"service": "기타", "count": sum(row["count"] for row in resource_mix_rows[10:])}
        ]
    resource_mix = _bar_rows(resource_mix_rows, "count")
    service_costs = [
        {
            "service": SERVICE_LABELS.get(row.get("service"), row.get("service", "-")),
            "cost_usd": float(row.get("cost_usd", 0)),
        }
        for row in report.get("costs", {}).get("services", [])
    ]
    anomalies = [
        {
            "start": row.get("start", "-"),
            "end": row.get("end") or "진행 중",
            "service": SERVICE_LABELS.get(row.get("service"), row.get("service", "-")),
            "impact_usd": float(row.get("impact_usd", 0)),
            "percentage": float(row.get("percentage", 0)),
        }
        for row in report.get("costs", {}).get("anomalies", [])
    ]
    problems = [
        {
            **row,
            "severity_label": SEVERITY_LABELS.get(row.get("severity"), row.get("severity", "확인")),
            "title_label": row.get("title", "-")
            .replace("Scheduled maintenance", "예정된 유지보수")
            .replace("EC2 status check", "EC2 상태 확인"),
        }
        for row in report.get("problems", [])
    ]
    s3_buckets = report.get("s3", {}).get("buckets", [])
    bucket_rows = [
        {
            "name": row["name"],
            "region": row.get("region", "-"),
            "storage_tb": float(row.get("total_storage_tb", 0)),
            "object_count": int(row.get("object_count", 0)),
            "versioning": "사용" if row.get("versioning") == "Enabled" else "사용 안 함",
            "lifecycle_rules": int(row.get("lifecycle_rules", 0)),
            "incomplete_count": int(row.get("incomplete_mpu_count", 0)),
            "incomplete_gb": row.get("incomplete_mpu_gb"),
            "access_logging": _yes_no(bool(row.get("access_logging"))),
        }
        for row in s3_buckets
    ]
    s3_costs = [
        {
            "kind": "데이터 인출"
            if "Retrieval" in row.get("usage_type", "")
            else "API 요청"
            if "Requests" in row.get("usage_type", "")
            else "저장 용량",
            "usage_type": row.get("usage_type", "-"),
            "cost_usd": float(row.get("cost_usd", 0)),
        }
        for row in report.get("s3", {}).get("costs", [])
    ]
    tco_raw = s3_tco(
        tco_inputs["storage_tb"],
        int(tco_inputs["object_millions"] * 1_000_000),
        tco_inputs["reads"],
        tco_inputs["cold_percent"] / 100,
        tco_inputs["rates"],
    )
    tco_rows = _bar_rows(
        [
            {
                "storage_class": S3_CLASS_LABELS.get(row["storage_class"], row["storage_class"]),
                "monthly_usd": float(row["monthly_usd"]),
                "best": index == 0,
            }
            for index, row in enumerate(tco_raw)
        ],
        "monthly_usd",
    )
    generated_at = datetime.fromisoformat(report["generated_at"]).astimezone()
    return {
        "mode": report.get("mode", "demo"),
        "mode_label": "예시" if report.get("mode") == "demo" else "실제 AWS 계정",
        "account_id": report.get("identity", {}).get("account_id", "확인 불가"),
        "generated_at": generated_at.strftime("%Y-%m-%d %H:%M:%S %Z"),
        "metrics": {
            "resources": resource_total,
            "cost_label": "이번 달 누적 비용" if current_estimated else "최근 월 비용",
            "current_cost": current_cost,
            "cost_delta": cost_delta,
            "monthly_savings": sum(row["monthly_savings_usd"] for row in recommendations),
            "problems": len(problems),
            "api_cost": float(report.get("api_cost_guard", {}).get("estimated_usd", 0)),
        },
        "monthly": monthly,
        "resource_mix": resource_mix,
        "resources": resources,
        "resource_query": resource_query,
        "resource_match_total": resource_match_total,
        "resource_truncated": resource_match_total > len(resources),
        "service_costs": service_costs,
        "anomalies": anomalies,
        "recommendations": recommendations,
        "top_recommendations": recommendations[:5],
        "problems": problems,
        "s3_buckets": bucket_rows,
        "s3_findings": _s3_findings(report.get("s3", {}).get("findings", []), s3_buckets),
        "s3_costs": s3_costs,
        "tco": tco_rows,
        "tco_inputs": tco_inputs,
        "pricing": [
            {"key": key, "label": PRICE_LABELS[key], "value": tco_inputs["rates"].get(key, default)}
            for key, default in S3_TCO_BASELINE.items()
        ],
        "errors": report.get("errors", []),
    }
