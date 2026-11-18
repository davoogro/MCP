import boto3
import pytest
from moto import mock_aws

from s3_mcp.ops import activate_settings


@pytest.fixture
def aws(monkeypatch):
    """Entorno S3 aislado con moto. No usa la red ni imprime credenciales."""
    monkeypatch.setenv("AWS_EC2_METADATA_DISABLED", "true")
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "AKIATESTING1234")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "aws-secret-should-not-win")
    monkeypatch.setenv("AWS_SESSION_TOKEN", "aws-session-should-not-leak")
    monkeypatch.setenv("S3_ACCESS_KEY_ID", "AKIATESTING1234")
    monkeypatch.setenv("S3_SECRET_ACCESS_KEY", "s3-secret-value-xyz")
    monkeypatch.setenv("S3_SESSION_TOKEN", "")
    monkeypatch.setenv("S3_REGION", "us-east-1")
    monkeypatch.setenv("S3_ENDPOINT_URL", "")
    monkeypatch.setenv("S3_BUCKETS", "demo")
    monkeypatch.setenv("S3_DEFAULT_BUCKET", "demo")
    monkeypatch.setenv("S3_BUCKET_ALLOWLIST_ONLY", "false")
    monkeypatch.setenv("S3_MAX_SCAN_OBJECTS", "5000")
    monkeypatch.setenv("S3_MAX_READ_BYTES", "5242880")
    monkeypatch.setenv("S3_MAX_PARQUET_READ_BYTES", str(256 * 1024 * 1024))
    monkeypatch.setenv("S3_MAX_COUNT_BYTES", str(2 * 1024 * 1024 * 1024))
    monkeypatch.setenv("S3_MAX_RESPONSE_CHARS", "80000")
    with mock_aws():
        activate_settings()
        client = boto3.client(
            "s3",
            region_name="us-east-1",
            aws_access_key_id="AKIATESTING1234",
            aws_secret_access_key="s3-secret-value-xyz",
        )
        client.create_bucket(Bucket="demo")
        yield client
        activate_settings()
