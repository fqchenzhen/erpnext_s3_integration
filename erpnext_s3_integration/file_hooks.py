import io
import re

import frappe
from frappe import _
from frappe.utils import cint
from unidecode import unidecode


def _with_trailing_slash(value):
	value = (value or "").strip().strip("/")
	return f"{value}/" if value else ""


def _safe_file_name(file_name):
	file_name = unidecode(file_name or "unnamed_file").strip() or "unnamed_file"
	file_name = re.sub(r"[^A-Za-z0-9._-]+", "_", file_name)
	return file_name.strip("._") or "unnamed_file"


def _hash_for_key(content_hash=None):
	content_hash = re.sub(r"[^A-Za-z0-9]", "", content_hash or "")
	if len(content_hash) >= 4:
		return content_hash
	return frappe.generate_hash(length=32)


def generate_s3_key(
	file_doc,
	settings,
	preserve_existing_path=False,
	preserve_existing_s3_url=False,
):
	"""Generate an S3 key for a File document or file-like object."""
	folder_prefix = _with_trailing_slash(settings.get("folder_prefix"))

	file_url = getattr(file_doc, "file_url", None)
	if preserve_existing_s3_url and file_url and file_url.startswith("/s3/"):
		return file_url.replace("/s3/", "", 1)

	if preserve_existing_path and file_url and file_url.startswith("/"):
		base_path = file_url.lstrip("/")
	else:
		file_name = _safe_file_name(getattr(file_doc, "file_name", None))
		content_hash = _hash_for_key(getattr(file_doc, "content_hash", None))
		visibility = "private" if cint(getattr(file_doc, "is_private", 0)) else "public"
		base_path = (
			f"attachments/{visibility}/{content_hash[:2]}/{content_hash[2:4]}/{content_hash}-{file_name}"
		)

	return f"{folder_prefix}{base_path}"


def _get_file_doc_content(file_doc):
	content = getattr(file_doc, "_content", None)
	if content is None:
		content = file_doc.get("content")

	if isinstance(content, str):
		return content.encode()
	return content


def _can_reuse_existing_s3_url(file_doc):
	file_url = getattr(file_doc, "file_url", None)
	if not file_url or not file_url.startswith("/s3/") or file_doc.is_new():
		return False

	return frappe.db.get_value("File", file_doc.name, "file_url") == file_url


def _write_file_doc_to_s3(file_doc):
	settings = frappe.get_single("S3 Integration Settings")
	if not settings.enable_attachments_s3:
		return file_doc.save_file_on_filesystem()

	content = _get_file_doc_content(file_doc)
	if not content:
		frappe.throw(_("File content is required before uploading to S3."))

	file_doc.file_name = _safe_file_name(file_doc.file_name)
	s3_key = generate_s3_key(
		file_doc,
		settings,
		preserve_existing_s3_url=_can_reuse_existing_s3_url(file_doc),
	)

	from erpnext_s3_integration.s3_client import S3Client

	content_stream = io.BytesIO(content)
	content_type = file_doc.get("content_type") or file_doc.get("mime_type")
	S3Client().upload_fileobj(content_stream, s3_key, content_type, not cint(file_doc.is_private))

	file_doc.file_url = f"/s3/{s3_key}"
	file_doc.content = None

	return {"file_name": file_doc.file_name, "file_url": file_doc.file_url}


def _write_legacy_file_to_s3(fname, content, content_type=None, is_private=0):
	settings = frappe.get_single("S3 Integration Settings")
	if not settings.enable_attachments_s3:
		from frappe.utils.file_manager import save_file_on_filesystem

		return save_file_on_filesystem(fname, content, content_type=content_type, is_private=is_private)

	if isinstance(content, str):
		content = content.encode()
	if not content:
		frappe.throw(_("File content is required before uploading to S3."))

	from frappe.utils.file_manager import get_content_hash

	file_name = _safe_file_name(fname)
	file_doc = frappe._dict(
		{
			"file_name": file_name,
			"content_hash": get_content_hash(content),
			"is_private": is_private,
		}
	)
	s3_key = generate_s3_key(file_doc, settings)

	from erpnext_s3_integration.s3_client import S3Client

	S3Client().upload_fileobj(io.BytesIO(content), s3_key, content_type, not cint(is_private))
	return {"file_name": file_name, "file_url": f"/s3/{s3_key}"}


def write_file_to_s3(file_or_name, content=None, content_type=None, is_private=0):
	"""Frappe write_file hook that stores new file content in S3-compatible storage."""
	if getattr(file_or_name, "doctype", None) == "File":
		return _write_file_doc_to_s3(file_or_name)

	return _write_legacy_file_to_s3(
		file_or_name,
		content,
		content_type=content_type,
		is_private=is_private,
	)


def _is_s3_url(file_url):
	return bool(file_url and file_url.startswith("/s3/"))


def _delete_s3_url(file_url, force_cleanup=False):
	if not _is_s3_url(file_url):
		return False

	settings = frappe.get_single("S3 Integration Settings")
	if not force_cleanup and not settings.delete_from_s3_on_file_delete:
		return True

	from erpnext_s3_integration.s3_client import S3Client

	s3_key = file_url.replace("/s3/", "", 1)
	try:
		S3Client().delete_object(s3_key, raise_on_error=True)
	except Exception:
		if not force_cleanup:
			raise
		frappe.logger("s3_rollback_cleanup").exception("Could not remove rolled-back S3 object %s", s3_key)
	return True


def _delete_local_url(file_url):
	if not file_url or _is_s3_url(file_url) or file_url.startswith(("http://", "https://")):
		return

	from frappe.utils.file_manager import delete_file

	delete_file(file_url)


def delete_file_data_content(file_doc, only_thumbnail=False):
	"""Frappe delete_file_data_content hook that only intercepts S3-backed URLs."""
	force_cleanup = bool(file_doc.flags.new_file)
	if only_thumbnail:
		targets = [file_doc.thumbnail_url]
	else:
		targets = [file_doc.file_url, file_doc.thumbnail_url]

	for file_url in targets:
		if not _delete_s3_url(file_url, force_cleanup=force_cleanup):
			_delete_local_url(file_url)


def before_insert(file_doc, method):
	"""Deprecated compatibility hook. Uploads are handled by write_file_to_s3."""
	return


def on_trash(file_doc, method):
	"""Deprecated compatibility hook. Deletes are handled by delete_file_data_content."""
	return
