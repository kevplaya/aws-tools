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
from datetime import date, datetime, timedelta, timezone
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

S3_STORAGE_TYPES = {
    "StandardStorage": "STANDARD",
    "StandardIAStorage": "STANDARD_IA",
    "OneZoneIAStorage": "ONEZONE_IA",
    "GlacierStorage": "GLACIER",
    "GlacierInstantRetrievalStorage": "GLACIER_IR",
    "DeepArchiveStorage": "DEEP_ARCHIVE",
    "IntelligentTieringFAStorage": "INTELLIGENT_TIERING",
    "IntelligentTieringIAStorage": "INTELLIGENT_TIERING",
    "IntelligentTieringAAStorage": "INTELLIGENT_TIERING",
    "IntelligentTieringAIAStorage": "INTELLIGENT_TIERING",
    "IntelligentTieringDAAStorage": "INTELLIGENT_TIERING",
}

S3_TCO_BASELINE = {
    "standard_storage": 0.025,
    "ia_storage": 0.0138,
    "gir_storage": 0.005,
    "standard_retrieval": 0.0,
    "ia_retrieval": 0.01,
    "gir_retrieval": 0.03,
    "standard_get_per_1000": 0.0004,
    "ia_get_per_1000": 0.001,
    "gir_get_per_1000": 0.01,
    "it_monitor_per_1000": 0.0025,
}


def _json_default(value: Any) -> Any:
    if isinstance(value, (datetime, Decimal)):
        return value.isoformat() if isinstance(value, datetime) else float(value)
    raise TypeError(f"Cannot serialize {type(value)!r}")


def resource_summary(resources: Iterable[dict[str, Any]]) -> dict[str, int]:
    """Count resources by service for dashboard KPIs."""
    return dict(sorted(Counter(r.get("service", "unknown") for r in resources).items()))


def _month_start(value: date, offset: int = 0) -> date:
    month_index = value.year * 12 + value.month - 1 + offset
    return date(month_index // 12, month_index % 12 + 1, 1)


def normalize_cost_results(results: Iterable[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    """Convert grouped Cost Explorer results into chart-friendly rows."""
    monthly: list[dict[str, Any]] = []
    services: Counter[str] = Counter()
    for result in results:
        month_total = 0.0
        for group in result.get("Groups", []):
            amount = float(group["Metrics"]["UnblendedCost"]["Amount"])
            month_total += amount
            services[group["Keys"][0]] += amount
        monthly.append(
            {
                "month": result["TimePeriod"]["Start"][:7],
                "total_usd": round(month_total, 2),
                "estimated": bool(result.get("Estimated")),
            }
        )
    return {
        "monthly": monthly,
        "services": [
            {"service": service, "cost_usd": round(cost, 2)}
            for service, cost in services.most_common()
        ],
    }


def s3_tco(
    storage_tb: float,
    object_count: int,
    full_reads_per_month: float,
    intelligent_tiering_cold_fraction: float = 0.0,
    rates: dict[str, float] | None = None,
) -> list[dict[str, Any]]:
    """Compare S3 classes using an explicit, editable monthly TCO model.

    A "full read" means every byte and object is read once. Intelligent-Tiering
    uses the IA rate for the caller-supplied cold fraction and has no retrieval
    charge; actual automatic-tier distribution must be verified with metrics.
    """
    price = {**S3_TCO_BASELINE, **(rates or {})}
    gb = storage_tb * 1024
    requests_1000 = object_count / 1000
    cold = min(max(intelligent_tiering_cold_fraction, 0.0), 1.0)

    def total(storage_rate: float, retrieval_rate: float, request_rate: float) -> float:
        return (
            gb * storage_rate
            + full_reads_per_month * gb * retrieval_rate
            + full_reads_per_month * requests_1000 * request_rate
        )

    it_storage = gb * (
        (1 - cold) * price["standard_storage"] + cold * price["ia_storage"]
    )
    it_total = (
        it_storage
        + requests_1000 * price["it_monitor_per_1000"]
        + full_reads_per_month * requests_1000 * price["standard_get_per_1000"]
    )
    rows = [
        {"storage_class": "STANDARD", "monthly_usd": total(price["standard_storage"], 0, price["standard_get_per_1000"])},
        {"storage_class": "STANDARD_IA", "monthly_usd": total(price["ia_storage"], price["ia_retrieval"], price["ia_get_per_1000"])},
        {"storage_class": "GLACIER_IR", "monthly_usd": total(price["gir_storage"], price["gir_retrieval"], price["gir_get_per_1000"])},
        {"storage_class": "INTELLIGENT_TIERING", "monthly_usd": it_total},
    ]
    for row in rows:
        row["monthly_usd"] = round(row["monthly_usd"], 2)
    return sorted(rows, key=lambda row: row["monthly_usd"])


def s3_findings(buckets: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    findings: list[dict[str, Any]] = []
    for bucket in buckets:
        name = bucket["name"]
        if bucket.get("incomplete_mpu_count", 0):
            size = bucket.get("incomplete_mpu_gb")
            detail = f"{bucket['incomplete_mpu_count']:,} incomplete uploads"
            if size is not None:
                detail += f" / {size:,.2f} GB billed until completed or aborted"
            findings.append(
                {"severity": "high", "bucket": name, "check": "incomplete_multipart", "detail": detail}
            )
        if not bucket.get("has_abort_mpu_rule"):
            findings.append(
                {
                    "severity": "medium",
                    "bucket": name,
                    "check": "abort_lifecycle",
                    "detail": "No lifecycle rule aborts incomplete multipart uploads",
                }
            )
        if bucket.get("versioning") == "Enabled" and not bucket.get("has_noncurrent_expiration"):
            findings.append(
                {
                    "severity": "medium",
                    "bucket": name,
                    "check": "noncurrent_versions",
                    "detail": "Versioning is enabled without noncurrent-version expiration",
                }
            )
        if not bucket.get("access_logging"):
            findings.append(
                {
                    "severity": "info",
                    "bucket": name,
                    "check": "access_evidence",
                    "detail": "Server access logging is off; historical object reads cannot be reconstructed",
                }
            )
    order = {"high": 0, "medium": 1, "info": 2}
    return sorted(findings, key=lambda row: (order[row["severity"]], row["bucket"]))


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

    def costs(self) -> dict[str, list[dict[str, Any]]]:
        return self.capture(
            "cost-explorer",
            self._costs,
            {"monthly": [], "services": [], "anomalies": []},
        )

    def _costs(self) -> dict[str, list[dict[str, Any]]]:
        client = self.client("ce", "us-east-1")
        today = datetime.now(timezone.utc).date()
        start = _month_start(today, -5)
        end = today + timedelta(days=1)
        self.budget.reserve("cost_explorer")
        response = client.get_cost_and_usage(
            TimePeriod={"Start": start.isoformat(), "End": end.isoformat()},
            Granularity="MONTHLY",
            Metrics=["UnblendedCost"],
            GroupBy=[{"Type": "DIMENSION", "Key": "SERVICE"}],
        )
        results = list(response.get("ResultsByTime", []))
        while response.get("NextPageToken"):
            self.budget.reserve("cost_explorer")
            response = client.get_cost_and_usage(
                TimePeriod={"Start": start.isoformat(), "End": end.isoformat()},
                Granularity="MONTHLY",
                Metrics=["UnblendedCost"],
                GroupBy=[{"Type": "DIMENSION", "Key": "SERVICE"}],
                NextPageToken=response["NextPageToken"],
            )
            results.extend(response.get("ResultsByTime", []))

        normalized = normalize_cost_results(results)
        normalized["anomalies"] = self.capture("cost-anomalies", self._cost_anomalies, [])
        return normalized

    def _cost_anomalies(self) -> list[dict[str, Any]]:
        today = datetime.now(timezone.utc).date()
        client = self.client("ce", "us-east-1")
        self.budget.reserve("cost_explorer")
        response = client.get_anomalies(
            DateInterval={"StartDate": (today - timedelta(days=90)).isoformat(), "EndDate": today.isoformat()},
            MaxResults=100,
        )
        return [
            {
                "start": item["AnomalyStartDate"],
                "end": item.get("AnomalyEndDate"),
                "service": (item.get("DimensionValue") or "unknown"),
                "impact_usd": round(float(item.get("Impact", {}).get("TotalImpact", 0)), 2),
                "percentage": round(float(item.get("Impact", {}).get("TotalImpactPercentage", 0)), 1),
                "root_causes": item.get("RootCauses", []),
            }
            for item in response.get("Anomalies", [])
        ]

    def recommendations(self) -> list[dict[str, Any]]:
        return self.capture("cost-optimization-hub", self._recommendations, [])

    def _recommendations(self) -> list[dict[str, Any]]:
        client = self.client("cost-optimization-hub", "us-east-1")
        params: dict[str, Any] = {"maxResults": 100}
        rows: list[dict[str, Any]] = []
        while True:
            response = client.list_recommendations(**params)
            for item in response.get("items", []):
                rows.append(
                    {
                        "action": item.get("actionType", "Review"),
                        "resource_type": item.get("resourceType", "unknown"),
                        "resource_id": item.get("resourceId") or item.get("resourceArn", "unknown"),
                        "region": item.get("region", "global"),
                        "monthly_cost_usd": round(float(item.get("estimatedMonthlyCost", 0)), 2),
                        "monthly_savings_usd": round(float(item.get("estimatedMonthlySavings", 0)), 2),
                        "savings_percentage": round(float(item.get("estimatedSavingsPercentage", 0)), 1),
                        "effort": item.get("implementationEffort", "unknown"),
                        "restart_needed": item.get("restartNeeded", False),
                        "rollback_possible": item.get("rollbackPossible", False),
                        "source": item.get("source", "Cost Optimization Hub"),
                    }
                )
            token = response.get("nextToken")
            if not token:
                break
            params["nextToken"] = token
        return sorted(rows, key=lambda row: row["monthly_savings_usd"], reverse=True)

    def s3(self, depth: str = "basic") -> dict[str, list[dict[str, Any]]]:
        buckets = self.capture("s3-buckets", lambda: self._s3_buckets(depth), [])
        return {
            "buckets": buckets,
            "findings": s3_findings(buckets),
            "costs": self.capture("s3-costs", self._s3_costs, []),
            "depth": depth,
        }

    def _s3_buckets(self, depth: str) -> list[dict[str, Any]]:
        global_client = self.client("s3", self.regions[0])
        rows: list[dict[str, Any]] = []
        for entry in global_client.list_buckets().get("Buckets", []):
            name = entry["Name"]
            location = global_client.get_bucket_location(Bucket=name).get("LocationConstraint")
            region = "us-east-1" if location in (None, "") else ("eu-west-1" if location == "EU" else location)
            client = self.client("s3", region)
            versioning = client.get_bucket_versioning(Bucket=name).get("Status", "Disabled")
            logging = bool(client.get_bucket_logging(Bucket=name).get("LoggingEnabled"))
            lifecycle = self.capture(
                f"s3-lifecycle:{name}",
                lambda client=client, name=name: client.get_bucket_lifecycle_configuration(Bucket=name).get("Rules", []),
                [],
            )
            uploads, upload_bytes = self._multipart_uploads(client, name, depth == "deep")
            rows.append(
                {
                    "name": name,
                    "region": region,
                    "created_at": entry.get("CreationDate"),
                    "versioning": versioning,
                    "access_logging": logging,
                    "lifecycle_rules": len(lifecycle),
                    "has_abort_mpu_rule": any(r.get("AbortIncompleteMultipartUpload") for r in lifecycle),
                    "has_noncurrent_expiration": any(r.get("NoncurrentVersionExpiration") for r in lifecycle),
                    "transition_rules": sum(bool(r.get("Transitions")) for r in lifecycle),
                    "incomplete_mpu_count": len(uploads),
                    "incomplete_mpu_gb": None if upload_bytes is None else round(upload_bytes / 1024**3, 2),
                    "object_count": 0,
                    "total_storage_tb": 0.0,
                    "storage_classes": {},
                }
            )
        self._add_s3_cloudwatch_metrics(rows)
        return sorted(rows, key=lambda row: row["total_storage_tb"], reverse=True)

    def _multipart_uploads(self, client, bucket: str, deep: bool) -> tuple[list[dict[str, Any]], int | None]:
        uploads: list[dict[str, Any]] = []
        paginator = client.get_paginator("list_multipart_uploads")
        for page in paginator.paginate(Bucket=bucket):
            self.budget.reserve("s3_list")
            uploads.extend(page.get("Uploads", []))
        if not deep:
            return uploads, None

        size = 0
        for upload in uploads:
            parts = client.get_paginator("list_parts")
            for page in parts.paginate(Bucket=bucket, Key=upload["Key"], UploadId=upload["UploadId"]):
                self.budget.reserve("s3_list")
                size += sum(int(part.get("Size", 0)) for part in page.get("Parts", []))
        return uploads, size

    def _add_s3_cloudwatch_metrics(self, buckets: list[dict[str, Any]]) -> None:
        for region in sorted({row["region"] for row in buckets}):
            region_rows = [row for row in buckets if row["region"] == region]
            queries: list[dict[str, Any]] = []
            query_map: dict[str, tuple[dict[str, Any], str]] = {}
            index = 0
            for row in region_rows:
                for storage_type, storage_class in S3_STORAGE_TYPES.items():
                    query_id = f"m{index}"
                    index += 1
                    queries.append(self._s3_metric_query(query_id, row["name"], "BucketSizeBytes", storage_type))
                    query_map[query_id] = (row, storage_class)
                query_id = f"m{index}"
                index += 1
                queries.append(self._s3_metric_query(query_id, row["name"], "NumberOfObjects", "AllStorageTypes"))
                query_map[query_id] = (row, "OBJECTS")

            client = self.client("cloudwatch", region)
            for offset in range(0, len(queries), 500):
                batch = queries[offset : offset + 500]
                self.budget.reserve("cloudwatch", len(batch))
                response = self.capture(
                    f"s3-cloudwatch:{region}",
                    lambda batch=batch: client.get_metric_data(
                        MetricDataQueries=batch,
                        StartTime=datetime.now(timezone.utc) - timedelta(days=3),
                        EndTime=datetime.now(timezone.utc),
                        ScanBy="TimestampDescending",
                    ),
                    {"MetricDataResults": []},
                )
                for result in response.get("MetricDataResults", []):
                    values = result.get("Values", [])
                    if not values:
                        continue
                    row, metric = query_map[result["Id"]]
                    value = float(values[0])
                    if metric == "OBJECTS":
                        row["object_count"] = int(value)
                    elif value:
                        gb = value / 1024**3
                        row["storage_classes"][metric] = round(
                            row["storage_classes"].get(metric, 0.0) + gb, 2
                        )
            for row in region_rows:
                row["total_storage_tb"] = round(sum(row["storage_classes"].values()) / 1024, 2)

    @staticmethod
    def _s3_metric_query(query_id: str, bucket: str, metric: str, storage_type: str) -> dict[str, Any]:
        return {
            "Id": query_id,
            "MetricStat": {
                "Metric": {
                    "Namespace": "AWS/S3",
                    "MetricName": metric,
                    "Dimensions": [
                        {"Name": "BucketName", "Value": bucket},
                        {"Name": "StorageType", "Value": storage_type},
                    ],
                },
                "Period": 86400,
                "Stat": "Average",
            },
            "ReturnData": True,
        }

    def _s3_costs(self) -> list[dict[str, Any]]:
        client = self.client("ce", "us-east-1")
        today = datetime.now(timezone.utc).date()
        self.budget.reserve("cost_explorer")
        response = client.get_cost_and_usage(
            TimePeriod={"Start": _month_start(today, -5).isoformat(), "End": (today + timedelta(days=1)).isoformat()},
            Granularity="MONTHLY",
            Metrics=["UnblendedCost", "UsageQuantity"],
            Filter={"Dimensions": {"Key": "SERVICE", "Values": ["Amazon Simple Storage Service"]}},
            GroupBy=[{"Type": "DIMENSION", "Key": "USAGE_TYPE"}],
        )
        totals: Counter[str] = Counter()
        for result in response.get("ResultsByTime", []):
            for group in result.get("Groups", []):
                totals[group["Keys"][0]] += float(group["Metrics"]["UnblendedCost"]["Amount"])
        return [
            {"usage_type": key, "cost_usd": round(value, 2)}
            for key, value in totals.most_common()
            if value > 0.01
        ]

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

    def collect(self, s3_depth: str = "basic") -> dict[str, Any]:
        resources = self.resources()
        costs = self.costs()
        report = {
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "mode": "live",
            "regions": list(self.regions),
            "identity": self.identity(),
            "resources": resources,
            "resource_summary": resource_summary(resources),
            "costs": costs,
            "recommendations": self.recommendations(),
            "problems": self.problems(),
            "s3": self.s3(s3_depth),
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
    s3_buckets = [
        {
            "name": "weather-data-demo",
            "region": "ap-northeast-2",
            "created_at": "2021-06-01T00:00:00+00:00",
            "versioning": "Enabled",
            "access_logging": False,
            "lifecycle_rules": 6,
            "has_abort_mpu_rule": False,
            "has_noncurrent_expiration": False,
            "transition_rules": 6,
            "incomplete_mpu_count": 1001,
            "incomplete_mpu_gb": 103.07,
            "object_count": 12_100_000,
            "total_storage_tb": 342.6,
            "storage_classes": {
                "GLACIER_IR": 228556.8,
                "STANDARD": 34611.2,
                "DEEP_ARCHIVE": 86323.2,
                "GLACIER": 1341.4,
                "STANDARD_IA": 51.2,
            },
        },
        {
            "name": "data-lake-dev-demo",
            "region": "ap-northeast-2",
            "created_at": "2024-01-01T00:00:00+00:00",
            "versioning": "Disabled",
            "access_logging": False,
            "lifecycle_rules": 2,
            "has_abort_mpu_rule": True,
            "has_noncurrent_expiration": False,
            "transition_rules": 0,
            "incomplete_mpu_count": 836,
            "incomplete_mpu_gb": 638.86,
            "object_count": 820_000,
            "total_storage_tb": 21.0,
            "storage_classes": {"STANDARD": 21504.0},
        },
    ]
    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "mode": "demo",
        "regions": ["ap-northeast-2"],
        "identity": {"account_id": "000000000000", "arn": "demo", "principal_id": "demo"},
        "resources": resources,
        "resource_summary": resource_summary(resources),
        "costs": {
            "monthly": [
                {"month": "2026-02", "total_usd": 1840.0, "estimated": False},
                {"month": "2026-03", "total_usd": 1975.0, "estimated": False},
                {"month": "2026-04", "total_usd": 2050.0, "estimated": False},
                {"month": "2026-05", "total_usd": 2215.0, "estimated": False},
                {"month": "2026-06", "total_usd": 2090.0, "estimated": False},
                {"month": "2026-07", "total_usd": 1010.0, "estimated": True},
            ],
            "services": [
                {"service": "Amazon Simple Storage Service", "cost_usd": 4820.0},
                {"service": "Amazon Relational Database Service", "cost_usd": 2580.0},
                {"service": "Amazon Elastic Compute Cloud", "cost_usd": 1780.0},
            ],
            "anomalies": [
                {
                    "start": "2026-06-15",
                    "end": "2026-06-17",
                    "service": "Amazon Simple Storage Service",
                    "impact_usd": 124.0,
                    "percentage": 18.4,
                    "root_causes": [],
                }
            ],
        },
        "recommendations": [
            {
                "action": "Rightsize",
                "resource_type": "Ec2Instance",
                "resource_id": "i-demo1",
                "region": "ap-northeast-2",
                "monthly_cost_usd": 122.0,
                "monthly_savings_usd": 48.0,
                "savings_percentage": 39.3,
                "effort": "Low",
                "restart_needed": True,
                "rollback_possible": True,
                "source": "Cost Optimization Hub",
            },
            {
                "action": "Delete",
                "resource_type": "EbsVolume",
                "resource_id": "vol-unused-demo",
                "region": "ap-northeast-2",
                "monthly_cost_usd": 22.0,
                "monthly_savings_usd": 22.0,
                "savings_percentage": 100.0,
                "effort": "VeryLow",
                "restart_needed": False,
                "rollback_possible": False,
                "source": "Cost Optimization Hub",
            },
        ],
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
        "s3": {
            "buckets": s3_buckets,
            "findings": s3_findings(s3_buckets),
            "costs": [
                {"usage_type": "TimedStorage-GIR-ByteHrs", "cost_usd": 6090.0},
                {"usage_type": "Retrieval-GIR", "cost_usd": 1534.72},
                {"usage_type": "Requests-GIR", "cost_usd": 220.05},
                {"usage_type": "Retrieval-SIA", "cost_usd": 67.74},
            ],
            "depth": "deep",
        },
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
    parser.add_argument("--s3-depth", choices=("basic", "deep"), default="basic")
    parser.add_argument("--output", default="reports/latest.json")
    args = parser.parse_args()

    report = (
        demo_report()
        if args.demo
        else AwsAudit(args.regions or DEFAULT_REGIONS, args.profile, args.max_api_cost).collect(args.s3_depth)
    )
    path = Path(args.output)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(report, ensure_ascii=False, indent=2, default=_json_default) + "\n")
    print(f"Wrote {path} ({report['mode']} mode)")


if __name__ == "__main__":
    main()
