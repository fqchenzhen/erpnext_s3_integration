from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from enum import StrEnum


class StorageTarget(StrEnum):
	OBJECT_STORAGE = "OBJECT_STORAGE"
	LOCAL_TEMPORARY = "LOCAL_TEMPORARY"
	LOCAL_FILESYSTEM_REQUIRED = "LOCAL_FILESYSTEM_REQUIRED"


LOCAL_TEMPORARY_DOCTYPES = frozenset({"Prepared Report"})
LOCAL_FILESYSTEM_REQUIRED_DOCTYPES = frozenset(
	{
		"Transaction Deletion Record",
		"Import Supplier Invoice",
		"Chart of Accounts Importer",
		"Bank Statement Import",
		"Repost Item Valuation",
	}
)
_attached_to_doctype_context: ContextVar[str | None] = ContextVar(
	"attachment_storage_doctype", default=None
)


def get_storage_target(file_doc=None) -> StorageTarget:
	attached_to_doctype = file_doc.get("attached_to_doctype") if file_doc else None
	attached_to_doctype = attached_to_doctype or _attached_to_doctype_context.get()
	if attached_to_doctype in LOCAL_TEMPORARY_DOCTYPES:
		return StorageTarget.LOCAL_TEMPORARY
	if attached_to_doctype in LOCAL_FILESYSTEM_REQUIRED_DOCTYPES:
		return StorageTarget.LOCAL_FILESYSTEM_REQUIRED
	return StorageTarget.OBJECT_STORAGE


@contextmanager
def storage_context(attached_to_doctype: str) -> Iterator[None]:
	token = _attached_to_doctype_context.set(attached_to_doctype)
	try:
		yield
	finally:
		_attached_to_doctype_context.reset(token)
