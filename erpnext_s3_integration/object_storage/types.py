import re
from abc import ABC, abstractmethod
from collections.abc import Iterator, Mapping
from dataclasses import dataclass, field
from datetime import datetime
from email.utils import parsedate_to_datetime
from typing import Any, BinaryIO


@dataclass(frozen=True)
class ObjectInfo:
	key: str
	size: int = 0
	content_type: str | None = None
	etag: str | None = None
	last_modified: datetime | None = None
	metadata: Mapping[str, str] = field(default_factory=dict)
	tags: Mapping[str, str] = field(default_factory=dict)
	storage_class: str | None = None
	restore_status: str | None = None
	restore_expiry: datetime | None = None


@dataclass
class ObjectStream:
	body: Any
	info: ObjectInfo
	content_range: str | None = None

	def read(self, size: int = -1) -> bytes:
		return self.body.read(size)

	def close(self) -> None:
		close = getattr(self.body, "close", None)
		if close:
			close()


class ObjectStorageBackend(ABC):
	@abstractmethod
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
		pass

	@abstractmethod
	def head(self, key: str) -> ObjectInfo | None:
		pass

	def exists(self, key: str) -> bool:
		return self.head(key) is not None

	@abstractmethod
	def get(self, key: str, byte_range: tuple[int, int] | None = None) -> ObjectStream:
		pass

	@abstractmethod
	def delete(self, key: str) -> None:
		pass

	@abstractmethod
	def put_tags(self, key: str, tags: Mapping[str, str]) -> None:
		pass

	@abstractmethod
	def get_tags(self, key: str) -> dict[str, str]:
		pass

	@abstractmethod
	def list(self, prefix: str) -> Iterator[ObjectInfo]:
		pass

	@abstractmethod
	def restore(self, key: str, days: int, tier: str) -> None:
		pass


def restore_expiry_from_header(value: str | None) -> datetime | None:
	if not value:
		return None
	match = re.search(r'expiry-date="([^"]+)"', value)
	if not match:
		return None
	return parsedate_to_datetime(match.group(1)).replace(tzinfo=None)
