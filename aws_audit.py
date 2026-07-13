#!/usr/bin/env python3
"""Read-only AWS portfolio audit collector.

The collector prefers AWS Resource Explorer for inventory and falls back to a
small set of direct service APIs.  Every section fails independently so an
account that has not enabled an optional AWS service still produces a useful
report.
"""

from __future__ import annotations

import json
import argparse
from collections import Counter
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any, Callable, Iterable

try:
    import boto3
    from botocore.exceptions import BotoCoreError, ClientError, NoCredentialsError
except ModuleNotFoundError:  # Demo mode and pure tests do not need the AWS SDK.
    boto3 = None
    BotoCoreError = ClientError = NoCredentialsError = RuntimeError


DEFAULT_REGIONS = ("ap-northeast-2", "us-east-1")


def _json_default(value: Any) -> Any:
    if isinstance(value, (datetime, Decimal)):
        return value.isoformat() if isinstance(value, datetime) else float(value)
    raise TypeError(f"Cannot serialize {type(value)!r}")


def resource_summary(resources: Iterable[dict[str, Any]]) -> dict[str, int]:
    """Count resources by service for dashboard KPIs."""
    return dict(sorted(Counter(r.get("service", "unknown") for r in resources).items()))


@dataclass
class ApiBudget:
    """Conservative request-cost guard for optional deep inspections.

    This is a safety ceiling, not an AWS invoice estimator.  Callers reserve a
    category before a request; an operation stops before exceeding max_usd.
    """

    max_usd: float = 1.0
    spent_usd: float = 0.0

    RATES = {
        "cost_explorer": 0.01,
        "s3_list": 0.00001,  # conservative $0.01 / 1,000 requests
        "cloudwatch": 0.00001,
        "free": 0.0,
    }

    def reserve(self, category: str, count: int = 1) -> None:
        amount = self.RATES[category] * count
        if self.spent_usd + amount > self.max_usd:
            raise RuntimeError(
                f"API cost guard stopped the scan: ${self.spent_usd + amount:.4f} "
                f"would exceed ${self.max_usd:.2f}"
            )
        self.spent_usd += amount


class AwsAudit:
    """Collect a portable, read-only AWS architecture and operations snapshot."""

    def __init__(
        self,
        regions: Iterable[str] = DEFAULT_REGIONS,
        profile: str | None = None,
        max_api_cost: float = 1.0,
    ) -> None:
        if boto3 is None:
            raise RuntimeError("Live collection requires: pip install -r requirements.txt")
        self.regions = tuple(dict.fromkeys(regions))
        self.session = boto3.Session(profile_name=profile) if profile else boto3.Session()
        self.budget = ApiBudget(max_api_cost)
        self.errors: list[dict[str, str]] = []

    def client(self, service: str, region: str | None = None):
        return self.session.client(service, region_name=region or self.regions[0])

    def capture(self, section: str, operation: Callable[[], Any], default: Any) -> Any:
        try:
            return operation()
        except (BotoCoreError, ClientError, NoCredentialsError, RuntimeError) as exc:
            self.errors.append({"section": section, "error": str(exc)})
            return default

    def identity(self) -> dict[str, str]:
        return self.capture("identity", self._identity, {})

    def _identity(self) -> dict[str, str]:
        data = self.client("sts").get_caller_identity()
        return {
            "account_id": data["Account"],
            "arn": data["Arn"],
            "principal_id": data["UserId"],
        }

    def resources(self) -> list[dict[str, Any]]:
        resources: list[dict[str, Any]] = []
        for region in self.regions:
            found = self.capture(
                f"resource-explorer:{region}",
                lambda region=region: self._resource_explorer(region),
                [],
            )
            resources.extend(found or self._fallback_resources(region))

        unique = {r["arn"]: r for r in resources if r.get("arn")}
        return sorted(unique.values(), key=lambda r: (r["service"], r["region"], r["name"]))

    def _resource_explorer(self, region: str) -> list[dict[str, Any]]:
        client = self.client("resource-explorer-2", region)
        views = client.list_views().get("Views", [])
        if not views:
            return []

        paginator = client.get_paginator("list_resources")
        rows: list[dict[str, Any]] = []
        for page in paginator.paginate(ViewArn=views[0], Filters={"FilterString": ""}):
            for item in page.get("Resources", []):
                arn = item["Arn"]
                rows.append(
                    {
                        "service": item.get("Service", "unknown"),
                        "type": item.get("ResourceType", "unknown"),
                        "name": arn.rsplit("/", 1)[-1].rsplit(":", 1)[-1],
                        "arn": arn,
                        "region": item.get("Region") or region,
                        "state": "discovered",
                        "source": "Resource Explorer",
                    }
                )
        return rows

    def _fallback_resources(self, region: str) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        rows.extend(self.capture(f"ec2:{region}", lambda: self._ec2_resources(region), []))
        rows.extend(self.capture(f"rds:{region}", lambda: self._rds_resources(region), []))
        rows.extend(self.capture(f"lambda:{region}", lambda: self._lambda_resources(region), []))
        return rows

    def _ec2_resources(self, region: str) -> list[dict[str, Any]]:
        client = self.client("ec2", region)
        rows: list[dict[str, Any]] = []
        for page in client.get_paginator("describe_instances").paginate():
            for reservation in page.get("Reservations", []):
                for instance in reservation.get("Instances", []):
                    if instance["State"]["Name"] == "terminated":
                        continue
                    tags = {t["Key"]: t["Value"] for t in instance.get("Tags", [])}
                    instance_id = instance["InstanceId"]
                    rows.append(
                        {
                            "service": "ec2",
                            "type": "AWS::EC2::Instance",
                            "name": tags.get("Name", instance_id),
                            "arn": f"arn:aws:ec2:{region}::instance/{instance_id}",
                            "region": region,
                            "state": instance["State"]["Name"],
                            "source": "EC2 API fallback",
                        }
                    )
        return rows

    def _rds_resources(self, region: str) -> list[dict[str, Any]]:
        client = self.client("rds", region)
        rows: list[dict[str, Any]] = []
        for page in client.get_paginator("describe_db_instances").paginate():
            for db in page.get("DBInstances", []):
                rows.append(
                    {
                        "service": "rds",
                        "type": "AWS::RDS::DBInstance",
                        "name": db["DBInstanceIdentifier"],
                        "arn": db["DBInstanceArn"],
                        "region": region,
                        "state": db["DBInstanceStatus"],
                        "source": "RDS API fallback",
                    }
                )
        return rows

    def _lambda_resources(self, region: str) -> list[dict[str, Any]]:
        client = self.client("lambda", region)
        rows: list[dict[str, Any]] = []
        for page in client.get_paginator("list_functions").paginate():
            for fn in page.get("Functions", []):
                rows.append(
                    {
                        "service": "lambda",
                        "type": "AWS::Lambda::Function",
                        "name": fn["FunctionName"],
                        "arn": fn["FunctionArn"],
                        "region": region,
                        "state": fn.get("State", "active"),
                        "source": "Lambda API fallback",
                    }
                )
        return rows

    def problems(self) -> list[dict[str, Any]]:
        findings: list[dict[str, Any]] = []
        for region in self.regions:
            findings.extend(
                self.capture(
                    f"cloudwatch-alarms:{region}",
                    lambda region=region: self._alarm_findings(region),
                    [],
                )
            )
            findings.extend(
                self.capture(
                    f"ec2-status:{region}",
                    lambda region=region: self._ec2_status_findings(region),
                    [],
                )
            )
        return sorted(findings, key=lambda f: (f["severity"], f["service"], f["title"]))

    def _alarm_findings(self, region: str) -> list[dict[str, Any]]:
        client = self.client("cloudwatch", region)
        rows = []
        for page in client.get_paginator("describe_alarms").paginate(StateValue="ALARM"):
            for alarm in page.get("MetricAlarms", []):
                rows.append(
                    {
                        "severity": "high",
                        "service": "cloudwatch",
                        "region": region,
                        "title": alarm["AlarmName"],
                        "detail": alarm.get("StateReason", "Alarm is active"),
                        "resource": alarm.get("AlarmArn", ""),
                    }
                )
            for alarm in page.get("CompositeAlarms", []):
                rows.append(
                    {
                        "severity": "high",
                        "service": "cloudwatch",
                        "region": region,
                        "title": alarm["AlarmName"],
                        "detail": alarm.get("StateReason", "Composite alarm is active"),
                        "resource": alarm.get("AlarmArn", ""),
                    }
                )
        return rows

    def _ec2_status_findings(self, region: str) -> list[dict[str, Any]]:
        client = self.client("ec2", region)
        rows = []
        paginator = client.get_paginator("describe_instance_status")
        for page in paginator.paginate(IncludeAllInstances=True):
            for status in page.get("InstanceStatuses", []):
                impaired = any(
                    check.get("Status") not in {"ok", "not-applicable"}
                    for check in (status.get("InstanceStatus", {}), status.get("SystemStatus", {}))
                )
                if impaired or status.get("Events"):
                    rows.append(
                        {
                            "severity": "high" if impaired else "medium",
                            "service": "ec2",
                            "region": region,
                            "title": f"EC2 status check: {status['InstanceId']}",
                            "detail": ", ".join(
                                e.get("Description", "scheduled event") for e in status.get("Events", [])
                            )
                            or "Instance or system status check is impaired",
                            "resource": status["InstanceId"],
                        }
                    )
        return rows

    def collect(self) -> dict[str, Any]:
        resources = self.resources()
        report = {
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "mode": "live",
            "regions": list(self.regions),
            "identity": self.identity(),
            "resources": resources,
            "resource_summary": resource_summary(resources),
            "costs": {"monthly": [], "services": []},
            "recommendations": [],
            "problems": self.problems(),
            "s3": {"buckets": [], "findings": [], "costs": []},
            "api_cost_guard": {
                "estimated_usd": round(self.budget.spent_usd, 4),
                "limit_usd": self.budget.max_usd,
            },
            "errors": self.errors,
        }
        return report


def demo_report() -> dict[str, Any]:
    """Safe portfolio data; no customer identifiers or real ARNs."""
    resources = [
        {
            "service": "ec2",
            "type": "AWS::EC2::Instance",
            "name": "portfolio-api",
            "arn": "arn:aws:ec2:ap-northeast-2:000000000000:instance/i-demo1",
            "region": "ap-northeast-2",
            "state": "running",
            "source": "demo",
        },
        {
            "service": "rds",
            "type": "AWS::RDS::DBInstance",
            "name": "portfolio-db",
            "arn": "arn:aws:rds:ap-northeast-2:000000000000:db:portfolio-db",
            "region": "ap-northeast-2",
            "state": "available",
            "source": "demo",
        },
        {
            "service": "lambda",
            "type": "AWS::Lambda::Function",
            "name": "cost-digest",
            "arn": "arn:aws:lambda:ap-northeast-2:000000000000:function:cost-digest",
            "region": "ap-northeast-2",
            "state": "active",
            "source": "demo",
        },
    ]
    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "mode": "demo",
        "regions": ["ap-northeast-2"],
        "identity": {"account_id": "000000000000", "arn": "demo", "principal_id": "demo"},
        "resources": resources,
        "resource_summary": resource_summary(resources),
        "costs": {"monthly": [], "services": []},
        "recommendations": [],
        "problems": [
            {
                "severity": "medium",
                "service": "ec2",
                "region": "ap-northeast-2",
                "title": "Scheduled maintenance: i-demo1",
                "detail": "Demo finding for portfolio display",
                "resource": "i-demo1",
            }
        ],
        "s3": {"buckets": [], "findings": [], "costs": []},
        "api_cost_guard": {"estimated_usd": 0.0, "limit_usd": 1.0},
        "errors": [],
    }


def main() -> None:
    """Collect a JSON snapshot for the dashboard."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--region", dest="regions", action="append", help="Region to scan; repeatable")
    parser.add_argument("--profile", help="AWS profile name")
    parser.add_argument("--max-api-cost", default=1.0, type=float)
    parser.add_argument("--demo", action="store_true", help="Use safe sample data without AWS calls")
    parser.add_argument("--output", default="reports/latest.json")
    args = parser.parse_args()

    report = (
        demo_report()
        if args.demo
        else AwsAudit(args.regions or DEFAULT_REGIONS, args.profile, args.max_api_cost).collect()
    )
    path = Path(args.output)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(report, ensure_ascii=False, indent=2, default=_json_default) + "\n")
    print(f"Wrote {path} ({report['mode']} mode)")


if __name__ == "__main__":
    main()
