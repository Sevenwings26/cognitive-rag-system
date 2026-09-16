# modules/connectors/sources/s3_connector.py
import uuid
import time
import logging
from typing import Generator, Dict, Any, Optional
from modules.connectors.base import BaseConnector, RawDocument

logger = logging.getLogger("s3_connector")

class S3Connector(BaseConnector):
    """
    Production Amazon S3 & S3-Compatible Storage Connector (MinIO, Wasabi, Ceph).
    """
    def __init__(self, connection_config: Dict[str, Any]):
        self.config_data = connection_config
        self.bucket_name = connection_config.get("bucket_name", "")
        self.prefix = connection_config.get("prefix", "")
        aws_access_key = connection_config.get("aws_access_key_id")
        aws_secret_key = connection_config.get("aws_secret_access_key")
        region = connection_config.get("region_name", "us-east-1")
        endpoint_url = connection_config.get("endpoint_url")
        self.s3_client = None

        try:
            import boto3
            client_kwargs = {"region_name": region}
            if endpoint_url:
                client_kwargs["endpoint_url"] = endpoint_url
            if aws_access_key and aws_secret_key:
                client_kwargs["aws_access_key_id"] = aws_access_key
                client_kwargs["aws_secret_access_key"] = aws_secret_key
            self.s3_client = boto3.client("s3", **client_kwargs)
        except Exception as e:
            logger.warning(f"S3 client initialization notice: {e}")

    def test_connection(self) -> Dict[str, Any]:
        start = time.perf_counter()
        if not self.s3_client:
            return {
                "success": False,
                "latency_ms": 0.0,
                "message": "AWS Boto3 SDK is not initialized or credentials are missing",
                "details": None
            }
        try:
            self.s3_client.head_bucket(Bucket=self.bucket_name)
            response = self.s3_client.list_objects_v2(Bucket=self.bucket_name, Prefix=self.prefix, MaxKeys=5)
            key_count = response.get("KeyCount", 0)
            sample_keys = [item["Key"] for item in response.get("Contents", [])[:3]]

            latency = round((time.perf_counter() - start) * 1000, 2)
            return {
                "success": True,
                "latency_ms": latency,
                "message": f"S3 Bucket '{self.bucket_name}' Reachable ({latency}ms)",
                "details": {
                    "bucket": self.bucket_name,
                    "prefix": self.prefix,
                    "sample_objects": sample_keys,
                    "object_count_preview": key_count
                }
            }
        except Exception as e:
            latency = round((time.perf_counter() - start) * 1000, 2)
            return {
                "success": False,
                "latency_ms": latency,
                "message": f"S3 Connection Failed: {str(e)}",
                "details": None
            }

    def fetch_documents(self) -> Generator[RawDocument, None, None]:
        if not self.s3_client:
            logger.warning(f"S3 client unavailable for bucket {self.bucket_name}")
            return
        try:
            paginator = self.s3_client.get_paginator("list_objects_v2")
            for page in paginator.paginate(Bucket=self.bucket_name, Prefix=self.prefix):
                for obj in page.get("Contents", []):
                    key = obj["Key"]
                    if key.endswith("/"):
                        continue
                    response = self.s3_client.get_object(Bucket=self.bucket_name, Key=key)
                    content_bytes = response["Body"].read()
                    filename = key.split("/")[-1]
                    mime_type = response.get("ContentType", "application/octet-stream")
                    doc_id = str(uuid.uuid4())
                    yield RawDocument(
                        doc_id=doc_id,
                        source_type="S3_BUCKET",
                        filename=filename,
                        content_bytes=content_bytes,
                        mime_type=mime_type,
                        metadata={
                            "external_id": key,
                            "s3_bucket": self.bucket_name,
                            "s3_key": key,
                            "etag": obj.get("ETag", "").strip('"'),
                            "last_modified": obj["LastModified"].isoformat() if hasattr(obj["LastModified"], "isoformat") else str(obj["LastModified"])
                        }
                    )
        except Exception as e:
            logger.error(f"S3 fetch failed for bucket {self.bucket_name}: {e}")
