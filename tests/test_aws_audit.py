import unittest

from aws_audit import ApiBudget, demo_report, normalize_cost_results, resource_summary


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


if __name__ == "__main__":
    unittest.main()
