"""Ensure an S3-compatible bucket (MinIO) exists, idempotently, via boto3."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

import boto3
from botocore.config import Config
from botocore.exceptions import ClientError

if TYPE_CHECKING:
    from botocore.client import BaseClient

logger = logging.getLogger(__name__)

_MISSING_CODES = {"404", "NoSuchBucket", "NotFound"}


def create_client(endpoint: str, access_key: str, secret_key: str) -> BaseClient:
    return boto3.client(
        "s3",
        endpoint_url=endpoint,
        aws_access_key_id=access_key,
        aws_secret_access_key=secret_key,
        region_name="us-east-1",
        config=Config(signature_version="s3v4"),
    )


def ensure_bucket(client: BaseClient, bucket: str) -> bool:
    """Return True if the bucket was created, False if it already existed."""
    try:
        client.head_bucket(Bucket=bucket)
        logger.info("minio: bucket %s already exists", bucket)
        return False
    except ClientError as exc:
        code = str(exc.response.get("Error", {}).get("Code", ""))
        if code not in _MISSING_CODES:
            raise
    client.create_bucket(Bucket=bucket)
    logger.info("minio: created bucket %s", bucket)
    return True
