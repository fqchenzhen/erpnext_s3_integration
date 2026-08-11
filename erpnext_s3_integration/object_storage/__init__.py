from typing import TYPE_CHECKING

if TYPE_CHECKING:
	from erpnext_s3_integration.object_storage.service import ObjectStorageService

__all__ = ["ObjectStorageService", "get_backend"]


def __getattr__(name: str):
	if name == "ObjectStorageService":
		from erpnext_s3_integration.object_storage.service import ObjectStorageService

		return ObjectStorageService
	if name == "get_backend":
		from erpnext_s3_integration.object_storage.factory import get_backend

		return get_backend
	raise AttributeError(name)
