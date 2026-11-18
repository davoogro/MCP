"""Offline tests for AWS MCP safety and compatibility controls."""

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

import athena_mcp_server as server
import aws_data_tools as aws


class SqlIntentTests(unittest.TestCase):
    def test_select_and_ctas_are_allowed(self):
        server._check_sql_intent("SELECT 1")
        server._check_sql_intent("SELECT 'DROP TABLE t' AS example")
        server._check_sql_intent("CREATE TABLE new_table AS SELECT 1")

    def test_destructive_requires_explicit_flag(self):
        for sql in ("DROP TABLE t", "DELETE FROM t", "CREATE OR REPLACE VIEW v AS SELECT 1",
                    "/* ok */ ALTER TABLE t ADD COLUMN c string"):
            with self.subTest(sql=sql), self.assertRaises(ValueError):
                server._check_sql_intent(sql)
            server._check_sql_intent(sql, True)

    def test_multiple_statements_rejected(self):
        with self.assertRaises(ValueError):
            server._check_sql_intent("SELECT 1; DROP TABLE t", True)

    def test_original_tool_does_not_run_destructive_sql(self):
        with patch.object(server, "execute_query") as execute:
            result = json.loads(server.query_athena("DROP TABLE t"))
        execute.assert_not_called()
        self.assertEqual(result["error"], "ValueError")


class AwsToolTests(unittest.TestCase):
    def setUp(self):
        self.manager = server.mcp._tool_manager

    def tool(self, tool_name, **kwargs):
        return json.loads(self.manager.get_tool(tool_name).fn(**kwargs))

    def test_original_tools_are_present(self):
        names = {tool.name for tool in self.manager.list_tools()}
        self.assertTrue({"query_athena", "list_databases", "list_tables", "describe_table",
                         "show_create_table", "test_connection", "sample_data",
                         "get_query_stats", "clear_cache"} <= names)

    def test_dry_run_never_contacts_aws(self):
        with patch.object(aws, "_client") as client:
            result = self.tool("s3_write_text", bucket="test-bucket", key="file.csv", text="a,b")
            glue = self.tool("glue_create_database", name="analysis")
            job = self.tool("glue_start_job", name="etl")
            query = self.tool("athena_start_query", sql="SELECT 1")
        client.assert_not_called()
        self.assertEqual([result["status"], glue["status"], job["status"], query["status"]],
                         ["planned"] * 4)
        self.assertNotIn("a,b", json.dumps(result))

    def test_s3_existing_object_requires_overwrite(self):
        fake = Mock()
        fake.head_object.return_value = {"ContentLength": 10}
        with patch.object(aws, "_client", return_value=fake):
            result = self.tool("s3_write_text", bucket="test-bucket", key="file.csv", text="x", dry_run=False)
        self.assertEqual(result["error"], "ValueError")
        fake.put_object.assert_not_called()

    def test_s3_empty_text_object(self):
        fake = Mock()
        fake.head_object.return_value = {"ContentLength": 0, "ContentType": "text/plain"}
        with patch.object(aws, "_client", return_value=fake):
            result = self.tool("s3_read_text", bucket="test-bucket", key="empty.txt")
        self.assertEqual(result["text"], "")
        fake.get_object.assert_not_called()

    def test_aws_client_error_is_redacted(self):
        from botocore.exceptions import ClientError
        error = ClientError({"Error": {"Code": "AccessDenied", "Message": "secret-sensitive-body"}}, "ListBuckets")
        result = aws._safe_error(error)
        self.assertEqual(result["error"], "AccessDenied")
        self.assertNotIn("secret-sensitive-body", json.dumps(result))

    def test_workgroups_use_next_token(self):
        fake = Mock()
        fake.list_work_groups.side_effect = [
            {"WorkGroups": [{"Name": "one"}], "NextToken": "page-2"},
            {"WorkGroups": [{"Name": "two"}]},
        ]
        with patch.object(aws, "_client", return_value=fake):
            result = self.tool("athena_list_workgroups", limit=2)
        self.assertEqual([item["Name"] for item in result], ["one", "two"])
        self.assertEqual(fake.list_work_groups.call_args_list[1].kwargs["NextToken"], "page-2")

    def test_athena_results_remove_header_row(self):
        fake = Mock()
        fake.get_query_results.return_value = {"ResultSet": {
            "ResultSetMetadata": {"ColumnInfo": [{"Name": "answer"}]},
            "Rows": [{"Data": [{"VarCharValue": "answer"}]},
                     {"Data": [{"VarCharValue": "1"}]}]}}
        with patch.object(aws, "_client", return_value=fake):
            result = self.tool("athena_get_results", query_execution_id="query-id")
        self.assertEqual(result["columns"], ["answer"])
        self.assertEqual(result["rows"], [["1"]])

    def test_lambda_configuration_excludes_environment(self):
        fake = Mock()
        fake.get_function.return_value = {"Configuration": {"FunctionName": "etl",
            "Environment": {"Variables": {"PASSWORD": "must-not-leak"}}},
            "Code": {"Location": "https://signed-download"}}
        with patch.object(aws, "_client", return_value=fake):
            result = self.tool("lambda_get_function", name="etl")
        self.assertEqual(result["configuration"]["FunctionName"], "etl")
        self.assertNotIn("must-not-leak", json.dumps(result))
        self.assertNotIn("signed-download", json.dumps(result))

    def test_artifact_transfer_is_scoped_and_dry_run(self):
        with tempfile.TemporaryDirectory() as directory, patch.object(aws, "_ARTIFACT_ROOT", Path(directory)):
            (Path(directory) / "sample.parquet").write_bytes(b"PAR1")
            with patch.object(aws, "_client") as client:
                upload = self.tool("s3_upload_artifact", bucket="test-bucket", key="sample.parquet",
                                   artifact_path="sample.parquet")
                download = self.tool("s3_download_artifact", bucket="test-bucket", key="other.parquet",
                                     artifact_path="other.parquet")
                escape = self.tool("s3_download_artifact", bucket="test-bucket", key="x",
                                   artifact_path="../secret.env")
            client.assert_not_called()
        self.assertEqual(upload["status"], "planned")
        self.assertEqual(download["status"], "planned")
        self.assertEqual(escape["error"], "ValueError")


if __name__ == "__main__":
    unittest.main()
