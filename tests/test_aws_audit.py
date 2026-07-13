import unittest

from aws_audit import ApiBudget, demo_report, resource_summary


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


if __name__ == "__main__":
    unittest.main()
