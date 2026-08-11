import threading
import time
from collections import OrderedDict
from collections.abc import Mapping
from typing import BinaryIO

import frappe

from erpnext_s3_integration.object_storage.errors import normalized_storage_error
from erpnext_s3_integration.object_storage.factory import get_backend

CLIENT_CACHE_MAX_SIZE = 32
CLIENT_CACHE_IDLE_SECONDS = 15 * 60
_backend_cache: OrderedDict[tuple[str, str], tuple[float, object]] = OrderedDict()
_cache_lock = threading.Lock()


class ObjectStorageService:
	def __init__(self, profile_name: str, *, require_enabled: bool = True):
		self.profile = frappe.get_doc("Object Storage Profile", profile_name)
		if require_enabled and not self.profile.enabled:
			raise frappe.ValidationError(f"Object Storage Profile {profile_name} is disabled")
		self.backend = _get_cached_backend(self.profile)

	def key(self, relative_key: str) -> str:
		parts = [self.profile.prefix.strip("/"), relative_key.lstrip("/")]
		return "/".join(part for part in parts if part)

	def put(
		self,
		key: str,
		body: BinaryIO,
		*,
		content_type: str | None = None,
		content_length: int | None = None,
		metadata: Mapping[str, str] | None = None,
		tags: Mapping[str, str] | None = None,
	):
		return self.backend.put(
			key,
			body,
			content_type=content_type,
			content_length=content_length,
			metadata=metadata,
			tags=tags,
		)


def clear_backend_cache(profile_name: str | None = None) -> None:
	with _cache_lock:
		if profile_name is None:
			_backend_cache.clear()
			return
		for cache_key in [key for key in _backend_cache if key[0] == profile_name]:
			_backend_cache.pop(cache_key, None)


def _get_cached_backend(profile):
	now = time.monotonic()
	fingerprint = profile.config_fingerprint()
	cache_key = (profile.name, fingerprint)
	with _cache_lock:
		_evict_idle_clients(now)
		cached = _backend_cache.pop(cache_key, None)
		if cached:
			_backend_cache[cache_key] = (now, cached[1])
			return cached[1]

		try:
			backend = get_backend(profile)
		except Exception as exc:
			raise normalized_storage_error(exc, "initialize") from exc
		_backend_cache[cache_key] = (now, backend)
		while len(_backend_cache) > CLIENT_CACHE_MAX_SIZE:
			_backend_cache.popitem(last=False)
		return backend


def _evict_idle_clients(now: float) -> None:
	for cache_key, (last_used, _) in list(_backend_cache.items()):
		if now - last_used > CLIENT_CACHE_IDLE_SECONDS:
			_backend_cache.pop(cache_key, None)
