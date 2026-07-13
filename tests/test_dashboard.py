import unittest

from aws_audit import demo_report
from dashboard_app import create_app


class DashboardTests(unittest.TestCase):
    def test_dashboard_explains_tabs_and_recommendation_criteria(self):
        response = create_app().test_client().get("/")
        body = response.get_data(as_text=True)

        self.assertEqual(response.status_code, 200)
        self.assertIn("한눈에 보기", body)
        self.assertIn("S3 비용 검토", body)
        self.assertIn("별도의 자체 점수나 숨겨진 우선순위는 없습니다", body)
        self.assertNotIn("streamlit", body.casefold())

    def test_demo_refresh_keeps_dashboard_available(self):
        response = create_app().test_client().post(
            "/refresh", data={"mode": "demo"}, follow_redirects=True
        )

        self.assertEqual(response.status_code, 200)
        self.assertIn("예시 데이터를 새로 불러왔습니다", response.get_data(as_text=True))

    def test_live_refresh_uses_selected_options(self):
        calls = []

        def factory(regions, profile, max_api_cost):
            calls.append((regions, profile, max_api_cost))

            class Collector:
                @staticmethod
                def collect(depth):
                    report = demo_report()
                    report["mode"] = "live"
                    report["identity"]["account_id"] = "123456789012"
                    report["s3"]["depth"] = depth
                    return report

            return Collector()

        response = create_app(factory).test_client().post(
            "/refresh",
            data={
                "mode": "live",
                "regions": "ap-northeast-2, us-east-1",
                "profile": "readonly",
                "max_api_cost": "0.25",
                "deep_s3": "on",
            },
            follow_redirects=True,
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(calls, [(["ap-northeast-2", "us-east-1"], "readonly", 0.25)])
        self.assertIn("AWS 계정 123456789012", response.get_data(as_text=True))


if __name__ == "__main__":
    unittest.main()
