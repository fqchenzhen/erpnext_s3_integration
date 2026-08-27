import io
import mimetypes

import frappe
from frappe import _
from frappe.core.doctype.file.utils import get_content_hash
from frappe.utils import cint

from erpnext_s3_integration.object_storage.service import ObjectStorageService
from erpnext_s3_integration.object_urls import (
	build_object_url,
	is_object_url,
	object_key_candidates,
)
from erpnext_s3_integration.storage_policy import StorageTarget, get_storage_target


def attachment_storage_enabled() -> bool:
	return bool(
		frappe.db.exists("DocType", "Object Storage Settings")
		and frappe.db.get_single_value("Object Storage Settings", "enable_attachment_storage")
	)


def generate_object_key(content_hash: str, is_private: bool, prefix: str = "") -> str:
	content_hash = (content_hash or "").strip().lower()
	if len(content_hash) < 4:
		raise ValueError("A content hash is required to generate an object key")
	visibility = "private" if is_private else "public"
	relative = f"attachments/{visibility}/{content_hash[:2]}/{content_hash[2:4]}/{content_hash}"
	return "/".join(part for part in (prefix.strip("/"), relative) if part)


def _content(file_doc) -> bytes | None:
	content = getattr(file_doc, "_content", None) or file_doc.get("content")
	return content.encode() if isinstance(content, str) else content


def _write_file_doc(file_doc, storage_target: StorageTarget):
	if storage_target != StorageTarget.OBJECT_STORAGE or not attachment_storage_enabled():
		return file_doc.save_file_on_filesystem()

	settings = frappe.get_single("Object Storage Settings")
	profile_name = settings.attachment_storage_profile
	service = ObjectStorageService(profile_name)
	content = _content(file_doc)
	if not content:
		frappe.throw(_("File content is required before uploading to object storage."))

	key = generate_object_key(file_doc.content_hash, cint(file_doc.is_private), service.profile.prefix)
	content_type = file_doc.get("content_type") or mimetypes.guess_type(file_doc.file_name or "")[0]
	service.put(
		key,
		io.BytesIO(content),
		content_type=content_type,
		content_length=len(content),
		metadata={"content-hash": file_doc.content_hash},
	)
	file_doc.object_storage_profile = profile_name
	file_doc.object_storage_key = key
	_allow_storage_fields(file_doc)
	file_doc.file_url = build_object_url(key, file_doc.file_name)
	file_doc.content = None
	return {
		"file_name": file_doc.file_name,
		"file_url": file_doc.file_url,
		"object_storage_profile": profile_name,
		"object_storage_key": key,
	}


def write_file_to_object_storage(file_or_name, content=None, content_type=None, is_private=0):
	"""Store File content using the active attachment profile."""
	storage_target = get_storage_target(file_or_name if getattr(file_or_name, "doctype", None) else None)
	if getattr(file_or_name, "doctype", None) == "File":
		return _write_file_doc(file_or_name, storage_target)

	if storage_target != StorageTarget.OBJECT_STORAGE or not attachment_storage_enabled():
		from frappe.utils.file_manager import save_file_on_filesystem

		return save_file_on_filesystem(
			file_or_name, content, content_type=content_type, is_private=is_private
		)
	return _write_legacy_file(file_or_name, content, content_type, is_private)


def _write_legacy_file(file_name, content, content_type, is_private) -> dict:
	content = content.encode() if isinstance(content, str) else content
	if content is None:
		frappe.throw(_("File content is required before uploading to object storage."))
	settings = frappe.get_single("Object Storage Settings")
	service = ObjectStorageService(settings.attachment_storage_profile)
	content_hash = get_content_hash(content)
	key = generate_object_key(content_hash, cint(is_private), service.profile.prefix)
	service.put(
		key,
		io.BytesIO(content),
		content_type=content_type or mimetypes.guess_type(file_name or "")[0],
		content_length=len(content),
		metadata={"content-hash": content_hash},
	)
	profile_name = service.profile.name
	_register_rollback_cleanup(profile_name, key)
	return {
		"file_name": file_name,
		"file_url": build_object_url(key, file_name),
		"object_storage_profile": profile_name,
		"object_storage_key": key,
	}


def copy_object_reference(file_doc, method=None):
	if not is_object_url(file_doc.file_url):
		return
	_allow_storage_fields(file_doc)
	if file_doc.object_storage_key:
		return
	existing = frappe.db.get_value(
		"File",
		{"file_url": file_doc.file_url},
		["object_storage_profile", "object_storage_key"],
		as_dict=True,
	)
	if existing:
		file_doc.object_storage_profile = existing.object_storage_profile
		file_doc.object_storage_key = existing.object_storage_key
		return
	identifier = file_doc.file_url.removeprefix("/s3/")
	for object_key in object_key_candidates(identifier):
		existing = frappe.db.get_value(
			"File",
			{"object_storage_key": object_key},
			["object_storage_profile", "object_storage_key"],
			as_dict=True,
		)
		if existing:
			file_doc.object_storage_profile = existing.object_storage_profile
			file_doc.object_storage_key = existing.object_storage_key
			return


def _allow_storage_fields(file_doc) -> None:
	if not getattr(file_doc, "flags", None):
		file_doc.flags = frappe._dict()
	fields = file_doc.flags.get("ignore_permlevel_for_fields", [])
	file_doc.flags.ignore_permlevel_for_fields = list(
		dict.fromkeys([*fields, "object_storage_profile", "object_storage_key"])
	)


def _register_rollback_cleanup(profile_name: str, key: str) -> None:
	frappe.db.after_rollback.add(lambda: _cleanup_rolled_back_upload(profile_name, key))


def delete_file_data_content(file_doc, only_thumbnail=False):
	"""Schedule object deletion only after the surrounding database transaction commits."""
	if not is_object_url(file_doc.file_url):
		return _delete_local(file_doc, only_thumbnail)
	_delete_local_thumbnail(file_doc)
	if only_thumbnail:
		return
	if not file_doc.object_storage_profile or not file_doc.object_storage_key:
		frappe.log_error("Object-backed File is missing profile or key", "Object Storage Delete")
		return

	profile_name = file_doc.object_storage_profile
	key = file_doc.object_storage_key
	if file_doc.flags.new_file:
		try:
			_delete_if_unreferenced(profile_name, key)
		except Exception:
			frappe.log_error(title="Object Storage Rollback Cleanup", message=frappe.get_traceback())
		return
	if not frappe.db.get_single_value("Object Storage Settings", "delete_on_last_reference"):
		return

	def enqueue_delete():
		frappe.enqueue(
			"erpnext_s3_integration.file_hooks.delete_object_if_unreferenced",
			queue="short",
			deduplicate=True,
			job_id=f"object-delete-{frappe.generate_hash(key, 16)}",
			profile_name=profile_name,
			key=key,
		)

	frappe.db.after_commit.add(enqueue_delete)


def delete_object_if_unreferenced(profile_name: str, key: str, attempt: int = 0) -> None:
	try:
		_delete_if_unreferenced(profile_name, key)
	except Exception:
		if attempt < 2:
			frappe.enqueue(
				"erpnext_s3_integration.file_hooks.delete_object_if_unreferenced",
				queue="short",
				profile_name=profile_name,
				key=key,
				attempt=attempt + 1,
			)
		else:
			frappe.log_error(title="Object Storage Delete", message=frappe.get_traceback())


def _delete_if_unreferenced(profile_name: str, key: str) -> None:
	if frappe.db.exists("File", {"object_storage_profile": profile_name, "object_storage_key": key}):
		return
	ObjectStorageService(profile_name, require_enabled=False).backend.delete(key)


def _cleanup_rolled_back_upload(profile_name: str, key: str) -> None:
	try:
		_delete_if_unreferenced(profile_name, key)
	except Exception:
		frappe.log_error(title="Object Storage Rollback Cleanup", message=frappe.get_traceback())


def _delete_local_thumbnail(file_doc) -> None:
	thumbnail_url = file_doc.thumbnail_url
	if thumbnail_url and not thumbnail_url.startswith(("http://", "https://", "/s3/")):
		from frappe.utils.file_manager import delete_file

		delete_file(thumbnail_url)


def _delete_local(file_doc, only_thumbnail: bool):
	from frappe.utils.file_manager import delete_file

	urls = [file_doc.thumbnail_url] if only_thumbnail else [file_doc.file_url, file_doc.thumbnail_url]
	for file_url in urls:
		if file_url and not file_url.startswith(("http://", "https://", "/s3/")):
			delete_file(file_url)
