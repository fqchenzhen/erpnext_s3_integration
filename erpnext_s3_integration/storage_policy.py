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


def get_storage_target(file_doc=None) -> StorageTarget:
	attached_to_doctype = file_doc.get("attached_to_doctype") if file_doc else None
	if attached_to_doctype in LOCAL_TEMPORARY_DOCTYPES:
		return StorageTarget.LOCAL_TEMPORARY
	if attached_to_doctype in LOCAL_FILESYSTEM_REQUIRED_DOCTYPES:
		return StorageTarget.LOCAL_FILESYSTEM_REQUIRED
	return StorageTarget.OBJECT_STORAGE
