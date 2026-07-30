import unittest

from aws_audit import _demo_network, demo_report, subnet_tier, vpc_topology
from dashboard_app.presentation import build_topology


def _network(**overrides):
    data = _demo_network()
    data.update(overrides)
    return data


def _findings(topology, check):
    return [row for row in topology[0]["findings"] if row["check"] == check]


class SubnetTierTests(unittest.TestCase):
    def test_default_route_decides_the_tier(self):
        self.assertEqual(subnet_tier("igw-1"), "public")
        self.assertEqual(subnet_tier("nat-1"), "private")
        self.assertEqual(subnet_tier(""), "isolated")
        self.assertEqual(subnet_tier("tgw-1"), "routed")


class VpcTopologyTests(unittest.TestCase):
    def setUp(self):
        self.topology = vpc_topology("ap-northeast-2", _demo_network())
        self.vpc = self.topology[0]

    def test_subnets_are_placed_by_route_table_not_by_name(self):
        by_id = {row["subnet_id"]: row for row in self.vpc["subnets"]}
        self.assertEqual(by_id["subnet-public-a"]["tier"], "public")
        self.assertEqual(by_id["subnet-app-a"]["tier"], "private")
        self.assertEqual(by_id["subnet-db-a"]["tier"], "isolated")

    def test_subnet_contents_come_from_network_interfaces(self):
        by_id = {row["subnet_id"]: row for row in self.vpc["subnets"]}
        self.assertEqual(by_id["subnet-public-a"]["resource_counts"], {"ec2": 1, "nat": 1})
        self.assertEqual(by_id["subnet-app-a"]["resource_counts"], {"lambda": 1})
        self.assertEqual(by_id["subnet-db-a"]["resource_counts"], {"rds": 1})
        ec2 = next(
            row for row in by_id["subnet-public-a"]["resources"] if row["kind"] == "ec2"
        )
        self.assertIn("portfolio-api", ec2["label"])

    def test_subnet_inheriting_the_main_route_table_is_marked(self):
        by_id = {row["subnet_id"]: row for row in self.vpc["subnets"]}
        self.assertTrue(by_id["subnet-public-a"]["route_table_explicit"])
        self.assertFalse(by_id["subnet-public-c"]["route_table_explicit"])
        self.assertEqual(by_id["subnet-public-c"]["route_table_id"], "rtb-main")
        self.assertTrue(_findings(self.topology, "implicit_main_route_table"))

    def test_duplicate_names_are_reported_with_their_route_difference(self):
        self.assertEqual(self.vpc["duplicate_route_table_names"], {"db-rt": ["rtb-db-1", "rtb-db-2"]})
        self.assertEqual(self.vpc["duplicate_subnet_names"], {"db-private": ["subnet-db-a", "subnet-db-c"]})
        detail = _findings(self.topology, "duplicate_route_table_name")[0]["detail"]
        self.assertIn("라우트 내용이 서로 달라", detail)

    def test_identical_duplicates_are_not_flagged_as_conflicting(self):
        data = _demo_network()
        for table in data["route_tables"]:
            if table["RouteTableId"] == "rtb-db-2":
                table["Routes"] = list(
                    next(t for t in data["route_tables"] if t["RouteTableId"] == "rtb-db-1")["Routes"]
                )
        detail = _findings(vpc_topology("ap-northeast-2", data), "duplicate_route_table_name")[0]["detail"]
        self.assertIn("라우트 내용은 동일합니다", detail)

    def test_traffic_routed_through_an_instance_is_high_severity(self):
        finding = _findings(self.topology, "instance_routed_traffic")[0]
        self.assertEqual(finding["severity"], "high")
        self.assertIn("100.64.0.0/10", finding["title"])
        self.assertIn("eni-router", finding["detail"])

    def test_peering_without_a_route_is_reported(self):
        data = _demo_network()
        for table in data["route_tables"]:
            table["Routes"] = [row for row in table["Routes"] if "VpcPeeringConnectionId" not in row]
        self.assertTrue(_findings(vpc_topology("ap-northeast-2", data), "unrouted_peering"))
        self.assertFalse(_findings(self.topology, "unrouted_peering"))

    def test_gateways_and_endpoints_are_scoped_to_the_vpc(self):
        self.assertEqual([row["igw_id"] for row in self.vpc["internet_gateways"]], ["igw-demo"])
        self.assertEqual(self.vpc["nat_gateways"][0]["subnet_id"], "subnet-public-a")
        self.assertEqual(self.vpc["peerings"][0]["peer_cidr"], "10.30.0.0/16")
        self.assertEqual(self.vpc["endpoints"][0]["route_table_ids"], ["rtb-app"])

    def test_other_vpcs_resources_are_not_mixed_in(self):
        data = _network(
            vpcs=_demo_network()["vpcs"]
            + [{"VpcId": "vpc-other", "CidrBlock": "10.90.0.0/16", "Tags": [{"Key": "Name", "Value": "other"}]}]
        )
        data["subnets"].append(
            {
                "SubnetId": "subnet-other",
                "VpcId": "vpc-other",
                "CidrBlock": "10.90.0.0/24",
                "AvailabilityZone": "ap-northeast-2a",
                "AvailableIpAddressCount": 251,
                "Tags": [{"Key": "Name", "Value": "other-a"}],
            }
        )
        topology = vpc_topology("ap-northeast-2", data)
        self.assertEqual([row["vpc_id"] for row in topology], ["vpc-demo", "vpc-other"])
        self.assertEqual([row["subnet_id"] for row in topology[1]["subnets"]], ["subnet-other"])

    def test_terminated_instances_are_ignored(self):
        data = _demo_network()
        data["reservations"][0]["Instances"][0]["State"] = {"Name": "terminated"}
        subnet = next(
            row
            for row in vpc_topology("ap-northeast-2", data)[0]["subnets"]
            if row["subnet_id"] == "subnet-public-a"
        )
        kinds = {row["kind"] for row in subnet["resources"]}
        self.assertNotIn("ec2", kinds)
        self.assertIn("interface", kinds)


class BuildTopologyTests(unittest.TestCase):
    def setUp(self):
        self.view = build_topology(demo_report()["topology"])

    def test_first_vpc_is_selected_by_default(self):
        self.assertEqual(self.view["selected_vpc"], "vpc-demo")
        self.assertEqual(self.view["selected"]["counts"]["subnets"], 5)
        self.assertEqual(self.view["selected"]["counts"]["empty_subnets"], 1)

    def test_unknown_vpc_falls_back_to_the_first_one(self):
        self.assertEqual(build_topology(demo_report()["topology"], "vpc-missing")["selected_vpc"], "vpc-demo")

    def test_empty_topology_selects_nothing(self):
        view = build_topology([])
        self.assertIsNone(view["selected"])
        self.assertEqual(view["vpcs"], [])

    def test_tier_groups_are_ordered_and_split_by_zone(self):
        groups = self.view["selected"]["tier_groups"]
        self.assertEqual([row["tier"] for row in groups], ["public", "private", "isolated"])
        public = groups[0]
        self.assertEqual([column["az"] for column in public["columns"]], ["ap-northeast-2a", "ap-northeast-2c"])

    def test_rows_are_labelled_in_korean_for_the_dashboard(self):
        subnet = next(
            row for row in self.view["selected"]["subnets"] if row["subnet_id"] == "subnet-app-a"
        )
        self.assertEqual(subnet["tier_label"], "비공개(NAT 경유)")
        self.assertEqual(subnet["summary"], "Lambda 함수 1개")
        isolated = next(
            row for row in self.view["selected"]["subnets"] if row["subnet_id"] == "subnet-db-c"
        )
        self.assertEqual(isolated["default_target"], "없음")
        self.assertEqual(isolated["summary"], "리소스 없음")

    def test_duplicate_route_tables_carry_a_flag_and_subnet_labels(self):
        tables = {row["route_table_id"]: row for row in self.view["selected"]["route_tables"]}
        self.assertTrue(tables["rtb-db-1"]["duplicate_name"])
        self.assertFalse(tables["rtb-app"]["duplicate_name"])
        self.assertEqual(tables["rtb-app"]["subnet_labels"], ["app-a"])

    def test_every_finding_gets_a_recommended_action(self):
        findings = self.view["selected"]["findings"]
        self.assertTrue(findings)
        for finding in findings:
            self.assertTrue(finding["action"])
            self.assertIn(finding["severity_label"], {"높음", "보통", "참고"})


if __name__ == "__main__":
    unittest.main()
