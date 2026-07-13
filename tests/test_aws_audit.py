import unittest

from aws_audit import (
    ApiBudget,
    demo_report,
    normalize_cost_results,
    resource_summary,
    s3_findings,
    s3_tco,
)


class AwsAuditTests(unittest.TestCase):
    def test_demo_report_has_dashboard_contract(self):
        report = demo_report()
        self.assertEqual(report["mode"], "demo")
        self.assertTrue(report["resources"])
        self.assertIn("costs", report)
        self.assertIn("s3", report)

    def test_resource_summary_counts_services(self):
        summary = resource_summary([{"service": "ec2"}, {"service": "ec2"}, {"service": "rds"}])
        self.assertEqual(summary, {"ec2": 2, "rds": 1})

    def test_api_budget_stops_before_limit(self):
        budget = ApiBudget(max_usd=0.015)
        budget.reserve("cost_explorer")
        with self.assertRaises(RuntimeError):
            budget.reserve("cost_explorer")

    def test_cost_results_are_grouped_for_charts(self):
        result = normalize_cost_results(
            [
                {
                    "TimePeriod": {"Start": "2026-06-01", "End": "2026-07-01"},
                    "Estimated": False,
                    "Groups": [
                        {"Keys": ["Amazon S3"], "Metrics": {"UnblendedCost": {"Amount": "12.50"}}},
                        {"Keys": ["Amazon EC2"], "Metrics": {"UnblendedCost": {"Amount": "7.50"}}},
                    ],
                }
            ]
        )
        self.assertEqual(result["monthly"][0]["total_usd"], 20.0)
        self.assertEqual(result["services"][0], {"service": "Amazon S3", "cost_usd": 12.5})

    def test_s3_gir_loses_when_data_is_read_frequently(self):
        costs = {row["storage_class"]: row["monthly_usd"] for row in s3_tco(26, 1_460_000, 2)}
        self.assertGreater(costs["GLACIER_IR"], costs["STANDARD"])

    def test_s3_findings_preserve_session_checks(self):
        findings = s3_findings(
            [
                {
                    "name": "demo",
                    "incomplete_mpu_count": 3,
                    "incomplete_mpu_gb": 10,
                    "has_abort_mpu_rule": False,
                    "versioning": "Enabled",
                    "has_noncurrent_expiration": False,
                    "access_logging": False,
                }
            ]
        )
        self.assertEqual(
            {row["check"] for row in findings},
            {"incomplete_multipart", "abort_lifecycle", "noncurrent_versions", "access_evidence"},
        )


if __name__ == "__main__":
    unittest.main()
