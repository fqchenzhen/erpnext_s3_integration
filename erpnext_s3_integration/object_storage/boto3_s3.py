from collections.abc import Iterator, Mapping
from typing import BinaryIO

import boto3
from botocore.config import Config

from erpnext_s3_integration.object_storage.errors import is_not_found, normalized_storage_error
from erpnext_s3_integration.object_storage.types import (
	ObjectInfo,
	ObjectStorageBackend,
	ObjectStream,
	restore_expiry_from_header,
)


class Boto3S3Backend(ObjectStorageBackend):
	def __init__(self, profile):
		self.bucket = profile.bucket
		self.server_side_encryption = (
			profile.server_side_encryption if profile.server_side_encryption != "Bucket Default" else None
		)
		credentials = {}
		if profile.credential_mode == "AccessKey":
			credentials = {
				"aws_access_key_id": profile.get_password("access_key_id"),
				"aws_secret_access_key": profile.get_password("access_key_secret"),
			}
		self.client = boto3.client(
			"s3",
			region_name=profile.region or None,
			endpoint_url=profile.endpoint_url or None,
			config=Config(
				signature_version="s3v4",
				s3={"addressing_style": (profile.addressing_style or "auto").lower()},
				retries={"max_attempts": 3, "mode": "standard"},
			),
			**credentials,
		)

	def put(
		self,
		key: str,
		body: BinaryIO,
		*,
		content_type: str | None = None,
		content_length: int | None = None,
		metadata: Mapping[str, str] | None = None,
		tags: Mapping[str, str] | None = None,
	) -> ObjectInfo:
		from urllib.parse import urlencode

		params = {"Bucket": self.bucket, "Key": key, "Body": body, "Metadata": dict(metadata or {})}
		if content_type:
			params["ContentType"] = content_type
		if content_length is not None:
			params["ContentLength"] = content_length
		if tags:
			params["Tagging"] = urlencode(tags)
		if self.server_side_encryption:
			params["ServerSideEncryption"] = self.server_side_encryption
		try:
			result = self.client.put_object(**params)
		except Exception as exc:
			raise normalized_storage_error(exc, "put") from exc
		return ObjectInfo(
			key=key, size=content_length or 0, content_type=content_type, etag=result.get("ETag")
		)

	def head(self, key: str) -> ObjectInfo | None:
		try:
			result = self.client.head_object(Bucket=self.bucket, Key=key)
		except Exception as exc:
			if is_not_found(exc):
				return None
			raise normalized_storage_error(exc, "head") from exc
		return _info_from_result(key, result)

	def get(self, key: str, byte_range: tuple[int, int] | None = None) -> ObjectStream:
		params = {"Bucket": self.bucket, "Key": key}
		if byte_range:
			params["Range"] = f"bytes={byte_range[0]}-{byte_range[1]}"
		try:
			result = self.client.get_object(**params)
		except Exception as exc:
			raise normalized_storage_error(exc, "get") from exc
		return ObjectStream(result["Body"], _info_from_result(key, result), result.get("ContentRange"))

	def delete(self, key: str) -> None:
		try:
			self.client.delete_object(Bucket=self.bucket, Key=key)
		except Exception as exc:
			raise normalized_storage_error(exc, "delete") from exc

	def put_tags(self, key: str, tags: Mapping[str, str]) -> None:
		try:
			self.client.put_object_tagging(
				Bucket=self.bucket,
				Key=key,
				Tagging={"TagSet": [{"Key": k, "Value": v} for k, v in tags.items()]},
			)
		except Exception as exc:
			raise normalized_storage_error(exc, "put tags") from exc

	def get_tags(self, key: str) -> dict[str, str]:
		try:
			result = self.client.get_object_tagging(Bucket=self.bucket, Key=key)
		except Exception as exc:
			raise normalized_storage_error(exc, "get tags") from exc
		return {tag["Key"]: tag["Value"] for tag in result.get("TagSet", [])}

	def list(self, prefix: str) -> Iterator[ObjectInfo]:
		try:
			paginator = self.client.get_paginator("list_objects_v2")
			for page in paginator.paginate(Bucket=self.bucket, Prefix=prefix):
				for item in page.get("Contents", []):
					yield ObjectInfo(
						key=item["Key"],
						size=item.get("Size", 0),
						etag=item.get("ETag"),
						last_modified=item.get("LastModified"),
						storage_class=item.get("StorageClass"),
					)
		except Exception as exc:
			raise normalized_storage_error(exc, "list") from exc

	def restore(self, key: str, days: int, tier: str) -> None:
		try:
			self.client.restore_object(
				Bucket=self.bucket,
				Key=key,
				RestoreRequest={"Days": days, "GlacierJobParameters": {"Tier": tier}},
			)
		except Exception as exc:
			raise normalized_storage_error(exc, "restore") from exc


def _info_from_result(key: str, result: dict) -> ObjectInfo:
	return ObjectInfo(
		key=key,
		size=result.get("ContentLength", 0),
		content_type=result.get("ContentType"),
		etag=result.get("ETag"),
		last_modified=result.get("LastModified"),
		metadata=result.get("Metadata", {}),
		storage_class=result.get("StorageClass"),
		restore_status=result.get("Restore"),
		restore_expiry=restore_expiry_from_header(result.get("Restore")),
	)
