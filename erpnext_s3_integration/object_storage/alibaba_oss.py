from collections.abc import Iterator, Mapping
from typing import Any, BinaryIO

import alibabacloud_oss_v2 as oss
from alibabacloud_credentials.client import Client as CredentialsClient
from alibabacloud_credentials.models import Config as CredentialsConfig

from erpnext_s3_integration.object_storage.errors import is_not_found, normalized_storage_error
from erpnext_s3_integration.object_storage.types import (
	ObjectInfo,
	ObjectStorageBackend,
	ObjectStream,
	restore_expiry_from_header,
)


class RefreshingCredentialsProvider(oss.credentials.CredentialsProvider):
	def __init__(self, config: CredentialsConfig | None = None):
		self.client = CredentialsClient(config)

	def get_credentials(self) -> oss.credentials.Credentials:
		credential = self.client.get_credential()
		return oss.credentials.Credentials(
			credential.access_key_id,
			credential.access_key_secret,
			credential.security_token,
		)


class _AlibabaResponseBody:
	def __init__(self, body: Any):
		self._body = body
		self._chunks = iter(body.iter_bytes())
		self._buffer = bytearray()
		self._eof = False
		self._closed = False

	def read(self, size: int | None = -1) -> bytes:
		if self._closed or size == 0:
			return b""
		if size is None or size < 0:
			return self._read_all()

		while len(self._buffer) < size and not self._eof:
			chunk = self._next_chunk()
			if chunk:
				self._buffer.extend(chunk)

		data = bytes(self._buffer[:size])
		del self._buffer[:size]
		return data

	def close(self) -> None:
		if self._closed:
			return
		self._closed = True
		self._eof = True
		self._buffer.clear()
		self._body.close()

	def _read_all(self) -> bytes:
		chunks = [bytes(self._buffer)] if self._buffer else []
		self._buffer.clear()
		while not self._eof:
			chunk = self._next_chunk()
			if chunk:
				chunks.append(chunk)
		return b"".join(chunks)

	def _next_chunk(self) -> bytes | None:
		try:
			return next(self._chunks)
		except StopIteration:
			self._eof = True
			return None
		except Exception as exc:
			raise normalized_storage_error(exc, "get") from exc


class AlibabaOSSBackend(ObjectStorageBackend):
	def __init__(self, profile):
		self.bucket = profile.bucket
		self.server_side_encryption = (
			profile.server_side_encryption if profile.server_side_encryption != "Bucket Default" else None
		)
		config = oss.Config(
			region=profile.region,
			endpoint=profile.endpoint_url or None,
			credentials_provider=self._credentials(profile),
			use_internal_endpoint=bool(profile.use_internal_endpoint),
			connect_timeout=10,
			readwrite_timeout=120,
			retry_max_attempts=3,
		)
		self.client = oss.Client(config)

	@staticmethod
	def _credentials(profile):
		mode = profile.credential_mode
		if mode == "AccessKey":
			return oss.credentials.StaticCredentialsProvider(
				profile.get_password("access_key_id"),
				profile.get_password("access_key_secret"),
			)
		if mode == "ECS Instance RAM Role":
			config = CredentialsConfig(
				type="ecs_ram_role",
				role_name=profile.ram_role_name or None,
				disable_imds_v1=True,
				enable_imds_v2=True,
			)
			return RefreshingCredentialsProvider(config)
		return RefreshingCredentialsProvider()

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
		try:
			result = self.client.put_object(
				oss.PutObjectRequest(
					bucket=self.bucket,
					key=key,
					body=body,
					content_type=content_type,
					content_length=content_length,
					metadata=dict(metadata or {}),
					tagging=_encode_tags(tags) if tags else None,
					server_side_encryption=self.server_side_encryption,
				)
			)
		except Exception as exc:
			raise normalized_storage_error(exc, "put") from exc
		return ObjectInfo(key=key, size=content_length or 0, content_type=content_type, etag=result.etag)

	def head(self, key: str) -> ObjectInfo | None:
		try:
			result = self.client.head_object(oss.HeadObjectRequest(bucket=self.bucket, key=key))
		except Exception as exc:
			if is_not_found(exc):
				return None
			raise normalized_storage_error(exc, "head") from exc
		return _info_from_result(key, result)

	def get(self, key: str, byte_range: tuple[int, int] | None = None) -> ObjectStream:
		range_header = f"bytes={byte_range[0]}-{byte_range[1]}" if byte_range else None
		try:
			result = self.client.get_object(
				oss.GetObjectRequest(bucket=self.bucket, key=key, range_header=range_header)
			)
		except Exception as exc:
			raise normalized_storage_error(exc, "get") from exc
		return ObjectStream(
			_AlibabaResponseBody(result.body),
			_info_from_result(key, result),
			result.content_range,
		)

	def delete(self, key: str) -> None:
		try:
			self.client.delete_object(oss.DeleteObjectRequest(bucket=self.bucket, key=key))
		except Exception as exc:
			raise normalized_storage_error(exc, "delete") from exc

	def put_tags(self, key: str, tags: Mapping[str, str]) -> None:
		tag_set = oss.models.TagSet(tags=[oss.models.Tag(key=k, value=v) for k, v in tags.items()])
		try:
			self.client.put_object_tagging(
				oss.PutObjectTaggingRequest(
					bucket=self.bucket,
					key=key,
					tagging=oss.models.Tagging(tag_set=tag_set),
				)
			)
		except Exception as exc:
			raise normalized_storage_error(exc, "put tags") from exc

	def get_tags(self, key: str) -> dict[str, str]:
		try:
			result = self.client.get_object_tagging(
				oss.GetObjectTaggingRequest(bucket=self.bucket, key=key)
			)
		except Exception as exc:
			raise normalized_storage_error(exc, "get tags") from exc
		return {tag.key: tag.value for tag in (result.tag_set.tags or [])}

	def list(self, prefix: str) -> Iterator[ObjectInfo]:
		token = None
		while True:
			try:
				result = self.client.list_objects_v2(
					oss.ListObjectsV2Request(
						bucket=self.bucket,
						prefix=prefix,
						continuation_token=token,
					)
				)
			except Exception as exc:
				raise normalized_storage_error(exc, "list") from exc
			for item in result.contents or []:
				yield ObjectInfo(
					key=item.key,
					size=item.size or 0,
					etag=item.etag,
					last_modified=item.last_modified,
					storage_class=item.storage_class,
				)
			if not result.is_truncated:
				break
			token = result.next_continuation_token

	def restore(self, key: str, days: int, tier: str) -> None:
		info = self.head(key)
		job_parameters = None
		if info and info.storage_class in {"ColdArchive", "DeepColdArchive"}:
			job_parameters = oss.models.JobParameters(tier=tier)
		request = oss.models.RestoreRequest(days=days, job_parameters=job_parameters)
		try:
			self.client.restore_object(
				oss.RestoreObjectRequest(bucket=self.bucket, key=key, restore_request=request)
			)
		except Exception as exc:
			raise normalized_storage_error(exc, "restore") from exc


def _encode_tags(tags: Mapping[str, str]) -> str:
	from urllib.parse import urlencode

	return urlencode(tags)


def _info_from_result(key: str, result) -> ObjectInfo:
	return ObjectInfo(
		key=key,
		size=result.content_length or 0,
		content_type=result.content_type,
		etag=result.etag,
		last_modified=result.last_modified,
		metadata=dict(result.metadata or {}),
		storage_class=result.storage_class,
		restore_status=result.restore,
		restore_expiry=restore_expiry_from_header(result.restore),
	)
