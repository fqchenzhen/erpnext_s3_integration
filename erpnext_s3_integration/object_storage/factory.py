from erpnext_s3_integration.object_storage.errors import ObjectConfigurationError
from erpnext_s3_integration.object_storage.types import ObjectStorageBackend


def get_backend(profile) -> ObjectStorageBackend:
	if profile.provider == "Alibaba Cloud OSS":
		from erpnext_s3_integration.object_storage.alibaba_oss import AlibabaOSSBackend

		return AlibabaOSSBackend(profile)

	if profile.provider in {"AWS S3", "MinIO", "Custom S3"}:
		from erpnext_s3_integration.object_storage.boto3_s3 import Boto3S3Backend

		return Boto3S3Backend(profile)

	raise ObjectConfigurationError(f"Unsupported object storage provider: {profile.provider}")
