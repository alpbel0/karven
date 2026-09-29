import boto3
from botocore.stub import Stubber

from app.migrator.minio import ensure_bucket


def _client():
    return boto3.client(
        "s3",
        region_name="us-east-1",
        aws_access_key_id="test",
        aws_secret_access_key="test",
    )


def test_ensure_bucket_reports_existing() -> None:
    client = _client()
    stubber = Stubber(client)
    stubber.add_response("head_bucket", {}, {"Bucket": "karven-raw"})

    with stubber:
        assert ensure_bucket(client, "karven-raw") is False


def test_ensure_bucket_creates_when_missing() -> None:
    client = _client()
    stubber = Stubber(client)
    stubber.add_client_error(
        "head_bucket",
        service_error_code="404",
        http_status_code=404,
        expected_params={"Bucket": "karven-raw"},
    )
    stubber.add_response("create_bucket", {}, {"Bucket": "karven-raw"})

    with stubber:
        assert ensure_bucket(client, "karven-raw") is True
