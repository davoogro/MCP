"""Offline QuickSight workflow and publication safety tests."""

import json
import unittest
from unittest.mock import Mock, patch

import athena_mcp_server as server
import quicksight_tools as quicksight


ACCOUNT = "123456789012"
SOURCE = f"arn:aws:quicksight:us-east-1:{ACCOUNT}:datasource/source-id"
DATASET = f"arn:aws:quicksight:us-east-1:{ACCOUNT}:dataset/dataset-id"


class QuickSightTests(unittest.TestCase):
    def tool(self, tool_name, **kwargs):
        return json.loads(server.mcp._tool_manager.get_tool(tool_name).fn(**kwargs))

    def test_glue_types_are_mapped_explicitly(self):
        self.assertEqual(quicksight._glue_type_to_qs("varchar(100)"), "STRING")
        self.assertEqual(quicksight._glue_type_to_qs("decimal(12,2)"), "DECIMAL")
        self.assertEqual(quicksight._glue_type_to_qs("timestamp"), "DATETIME")
        with self.assertRaises(ValueError):
            quicksight._glue_type_to_qs("array<string>")

    def test_full_creation_chain_is_dry_run(self):
        source = self.tool("quicksight_create_athena_data_source", data_source_id="source-id",
                           name="Test", aws_account_id=ACCOUNT)
        dataset = self.tool("quicksight_create_athena_data_set", data_set_id="dataset-id",
                            name="Test", data_source_arn=SOURCE, database="analytics", table="orders",
                            columns=[{"Name": "order_id", "Type": "STRING"}], aws_account_id=ACCOUNT)
        definition = quicksight._table_definition(DATASET, ["order_id"], "Overview")
        analysis = self.tool("quicksight_create_analysis", analysis_id="analysis-id", name="Test",
                             definition=definition, aws_account_id=ACCOUNT)
        dashboard = self.tool("quicksight_create_dashboard", dashboard_id="dashboard-id", name="Test",
                              definition=definition, aws_account_id=ACCOUNT)
        publication = self.tool("quicksight_publish_dashboard_version", dashboard_id="dashboard-id",
                                version_number=1, aws_account_id=ACCOUNT)
        self.assertEqual([item.get("status") for item in (source, dataset, analysis, dashboard, publication)],
                         ["planned"] * 5)

    def test_publication_requires_explicit_confirmation(self):
        with patch.object(quicksight, "_client") as client:
            result = self.tool("quicksight_publish_dashboard_version", dashboard_id="dashboard-id",
                               version_number=1, aws_account_id=ACCOUNT, dry_run=False)
        self.assertEqual(result["error"], "ValueError")
        client.assert_not_called()

    def test_update_chain_and_sharing_plan(self):
        definition = quicksight._table_definition(DATASET, ["order_id"], "Overview")
        dataset = self.tool("quicksight_update_athena_data_set", data_set_id="dataset-id",
                            name="Test", data_source_arn=SOURCE, database="analytics", table="orders",
                            columns=[{"Name": "order_id", "Type": "STRING"}], aws_account_id=ACCOUNT)
        analysis = self.tool("quicksight_update_analysis", analysis_id="analysis-id", name="Test",
                             definition=definition, aws_account_id=ACCOUNT)
        dashboard = self.tool("quicksight_update_dashboard", dashboard_id="dashboard-id", name="Test",
                              definition=definition, aws_account_id=ACCOUNT)
        principal = f"arn:aws:quicksight:us-east-1:{ACCOUNT}:user/default/analyst"
        sharing = self.tool("quicksight_grant_dashboard_read", dashboard_id="dashboard-id",
                            principal_arn=principal, aws_account_id=ACCOUNT)
        self.assertEqual([item.get("status") for item in (dataset, analysis, dashboard, sharing)],
                         ["planned"] * 4)

    def test_sharing_rejects_public_and_requires_confirmation(self):
        public = self.tool("quicksight_grant_dashboard_read", dashboard_id="dashboard-id",
                           principal_arn="arn:aws:quicksight:::publicAnonymousUser/*", aws_account_id=ACCOUNT)
        self.assertEqual(public["error"], "ValueError")
        principal = f"arn:aws:quicksight:us-east-1:{ACCOUNT}:user/default/analyst"
        with patch.object(quicksight, "_client") as client:
            result = self.tool("quicksight_grant_dashboard_read", dashboard_id="dashboard-id",
                               principal_arn=principal, aws_account_id=ACCOUNT, dry_run=False)
        self.assertEqual(result["error"], "ValueError")
        client.assert_not_called()

    def test_list_follows_next_token(self):
        fake = Mock()
        fake.list_dashboards.side_effect = [
            {"DashboardSummaryList": [{"DashboardId": "one"}], "NextToken": "next"},
            {"DashboardSummaryList": [{"DashboardId": "two"}]},
        ]
        with patch.object(quicksight, "_client", return_value=fake):
            result = quicksight._list("list_dashboards", "DashboardSummaryList", limit=2,
                                      account_id=ACCOUNT)
        self.assertEqual([item["DashboardId"] for item in result], ["one", "two"])
        self.assertEqual(fake.list_dashboards.call_args_list[1].kwargs["NextToken"], "next")


if __name__ == "__main__":
    unittest.main()
