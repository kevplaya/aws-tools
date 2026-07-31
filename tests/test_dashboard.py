import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from aws_audit import demo_report
from dashboard_app import create_app
from dashboard_app.storage import SnapshotStore


class DashboardTests(unittest.TestCase):
    def setUp(self):
        self.temporary_directory = TemporaryDirectory()
        self.database_path = Path(self.temporary_directory.name) / "dashboard.db"

    def tearDown(self):
        self.temporary_directory.cleanup()

    def test_dashboard_explains_tabs_and_recommendation_criteria(self):
        response = create_app(database_path=self.database_path).test_client().get("/")
        body = response.get_data(as_text=True)

        self.assertEqual(response.status_code, 200)
        self.assertIn("한눈에 보기", body)
        self.assertIn("S3 비용 검토", body)
        self.assertIn("별도의 자체 점수나 숨겨진 우선순위는 없습니다", body)
        self.assertNotIn("streamlit", body.casefold())

    def test_topology_tab_shows_the_selected_vpc_map(self):
        client = create_app(database_path=self.database_path).test_client()

        body = client.get("/?vpc=vpc-demo").get_data(as_text=True)

        self.assertIn("네트워크 토폴로지", body)
        self.assertIn("비공개(NAT 경유)", body)
        self.assertIn("메인 라우팅 테이블 상속", body)
        self.assertIn("라우팅 테이블 이름 중복: db-rt", body)
        self.assertIn("portfolio-api", body)

    def test_topology_tab_survives_a_report_without_network_data(self):
        report = demo_report()
        report.pop("topology")
        store = SnapshotStore(self.database_path)
        report["mode"] = "live"
        store.save(report)

        body = create_app(database_path=self.database_path).test_client().get("/").get_data(as_text=True)

        self.assertIn("조회한 VPC가 없습니다", body)

    def test_demo_refresh_keeps_dashboard_available(self):
        response = create_app(database_path=self.database_path).test_client().post(
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

        app = create_app(factory, database_path=self.database_path)
        response = app.test_client().post(
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
        self.assertEqual(app.test_client().get("/health").get_json()["snapshot_count"], 1)

    def test_restart_loads_latest_live_snapshot_without_aws_call(self):
        def factory(regions, profile, max_api_cost):
            class Collector:
                @staticmethod
                def collect(depth):
                    report = demo_report()
                    report["mode"] = "live"
                    report["identity"]["account_id"] = "123456789012"
                    return report

            return Collector()

        create_app(factory, database_path=self.database_path).test_client().post(
            "/refresh", data={"mode": "live"}
        )

        def fail_if_called(*args):
            raise AssertionError("startup must not call AWS")

        restarted = create_app(fail_if_called, database_path=self.database_path)
        body = restarted.test_client().get("/").get_data(as_text=True)

        self.assertIn("AWS 계정 123456789012", body)
        self.assertIn("현재 화면: DB #1", body)
        self.assertEqual(restarted.test_client().get("/health").get_json()["mode"], "live")

    def test_snapshot_store_keeps_complete_report(self):
        report = demo_report()
        report["mode"] = "live"
        report["custom_evidence"] = {"checked": True}
        store = SnapshotStore(self.database_path)

        metadata = store.save(report)
        loaded, loaded_metadata = store.latest()

        self.assertEqual(metadata.id, 1)
        self.assertEqual(loaded["custom_evidence"], {"checked": True})
        self.assertEqual(loaded_metadata.resource_count, len(report["resources"]))


if __name__ == "__main__":
    unittest.main()
