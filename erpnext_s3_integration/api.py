import mimetypes
import os
from urllib.parse import quote

import frappe
from frappe import _
from frappe.core.doctype.file.utils import find_file_by_url
from frappe.utils.response import FORCE_DOWNLOAD_EXTENSIONS
from werkzeug.http import http_date
from werkzeug.wrappers import Response
from werkzeug.wsgi import wrap_file

from erpnext_s3_integration.object_storage.service import ObjectStorageService
from erpnext_s3_integration.object_urls import object_key_candidates, object_url_from_identifier

ARCHIVE_CLASSES = {"Archive", "ColdArchive", "DeepColdArchive", "GLACIER", "DEEP_ARCHIVE"}


@frappe.whitelist(allow_guest=True, methods=["GET"])  # nosemgrep
def get_file():
	"""Stream an authorized File through Frappe without exposing a provider URL."""
	key = frappe.form_dict.get("key")
	if not key:
		raise frappe.PageDoesNotExistError()
	file_doc = _authorized_file(key)
	object_key = file_doc.object_storage_key

	service = ObjectStorageService(file_doc.object_storage_profile)
	info = service.backend.head(object_key)
	if not info:
		raise frappe.DoesNotExistError()
	if _restore_required(info.storage_class, info.restore_status):
		return _restore_required_response(file_doc)

	byte_range = None
	status = 200
	if frappe.get_single("Object Storage Settings").enable_range_requests:
		try:
			byte_range = parse_range_header(frappe.request.headers.get("Range"), info.size)
		except ValueError:
			return Response(status=416, headers={"Content-Range": f"bytes */{info.size}"})
	if byte_range:
		status = 206

	stream = service.backend.get(object_key, byte_range)
	response = Response(
		wrap_file(frappe.request.environ, stream),
		status=status,
		direct_passthrough=True,
	)
	response.call_on_close(stream.close)
	_set_headers(response, file_doc, info, byte_range, stream.content_range)
	return response


@frappe.whitelist(allow_guest=True, methods=["GET"])  # nosemgrep
def get_file_status(key: str) -> dict:
	file_doc = _authorized_file(key)
	object_key = file_doc.object_storage_key
	info = ObjectStorageService(file_doc.object_storage_profile).backend.head(object_key)
	if not info:
		raise frappe.DoesNotExistError()
	restore_required = _restore_required(info.storage_class, info.restore_status)
	request = None
	if restore_required and frappe.session.user != "Guest":
		request = frappe.db.get_value(
			"Object Restore Request",
			{
				"object_storage_profile": file_doc.object_storage_profile,
				"object_key": object_key,
				"status": ["in", ["Requested", "Restoring", "Ready"]],
			},
			["name", "status", "requested_at", "last_checked_at", "expires_at"],
			as_dict=True,
		)
	can_restore = False
	if restore_required and frappe.session.user != "Guest":
		from erpnext_s3_integration.erpnext_s3_integration.doctype.object_restore_request.object_restore_request import (
			can_request_restore,
		)

		can_restore = can_request_restore(frappe.session.user)
	return {
		"file": file_doc.name,
		"file_name": file_doc.file_name,
		"download_ready": not restore_required,
		"restore_required": restore_required,
		"storage_class": info.storage_class or "Standard",
		"restore_request": request,
		"estimated_time": _restore_estimate(info.storage_class),
		"can_request_restore": can_restore,
	}


def parse_range_header(value: str | None, total_size: int) -> tuple[int, int] | None:
	if not value:
		return None
	if not value.startswith("bytes=") or "," in value or total_size <= 0:
		raise ValueError("Unsupported Range header")
	start_text, separator, end_text = value[6:].partition("-")
	if not separator:
		raise ValueError("Invalid Range header")
	if not start_text:
		length = int(end_text)
		if length <= 0:
			raise ValueError("Invalid suffix range")
		return max(total_size - length, 0), total_size - 1
	start = int(start_text)
	end = int(end_text) if end_text else total_size - 1
	if start < 0 or start >= total_size or end < start:
		raise ValueError("Unsatisfiable Range header")
	return start, min(end, total_size - 1)


def _set_headers(response, file_doc, info, byte_range, provider_content_range) -> None:
	filename = file_doc.file_name or "download"
	content_type = info.content_type or mimetypes.guess_type(filename)[0] or "application/octet-stream"
	force_download = os.path.splitext(filename)[1].lower() in FORCE_DOWNLOAD_EXTENSIONS
	disposition = "attachment" if force_download else "inline"
	ascii_name = "".join(character if ord(character) < 128 else "_" for character in filename)
	response.headers["Content-Type"] = content_type
	response.headers["Content-Disposition"] = (
		f"{disposition}; filename=\"{ascii_name.replace(chr(34), '_')}\"; filename*=UTF-8''{quote(filename)}"
	)
	response.headers["X-Content-Type-Options"] = "nosniff"
	response.headers["Accept-Ranges"] = "bytes"
	response.headers["Cache-Control"] = "private, no-store" if file_doc.is_private else "public, max-age=3600"
	if info.etag:
		response.headers["ETag"] = info.etag
	if info.last_modified:
		response.headers["Last-Modified"] = http_date(info.last_modified)
	if byte_range:
		length = byte_range[1] - byte_range[0] + 1
		response.headers["Content-Length"] = str(length)
		response.headers["Content-Range"] = (
			provider_content_range or f"bytes {byte_range[0]}-{byte_range[1]}/{info.size}"
		)
	else:
		response.headers["Content-Length"] = str(info.size)
	if force_download:
		response.headers["Content-Security-Policy"] = "sandbox"


def _restore_required(storage_class: str | None, restore_status: str | None) -> bool:
	if storage_class not in ARCHIVE_CLASSES:
		return False
	return not (restore_status and 'ongoing-request="false"' in restore_status)


def _restore_required_response(file_doc) -> Response:
	message = _("This object is archived. Create or check an Object Restore Request before downloading it.")
	return Response(
		message,
		status=409,
		content_type="text/plain; charset=utf-8",
		headers={"X-Object-Restore-Required": "1", "X-File-Name": file_doc.name, "Cache-Control": "no-store"},
	)


def _authorized_file(key: str):
	file_doc = find_file_by_url(object_url_from_identifier(key))
	if file_doc and file_doc.object_storage_key:
		return file_doc

	for object_key in object_key_candidates(key):
		files = frappe.get_all("File", filters={"object_storage_key": object_key}, fields="*")
		for file_data in files:
			file_doc = frappe.get_doc(doctype="File", **file_data)
			if file_doc.is_downloadable():
				return file_doc
	raise frappe.PermissionError()


def _restore_estimate(storage_class: str | None) -> str:
	return {
		"Archive": _("Usually minutes; actual time and charges depend on the selected restore tier."),
		"GLACIER": _("Usually minutes to hours; actual time and charges depend on the provider."),
		"ColdArchive": _("Usually hours; Alibaba Cloud restore time and charges apply."),
		"DeepColdArchive": _("Usually hours; Alibaba Cloud restore time and charges apply."),
		"DEEP_ARCHIVE": _("Usually hours; actual time and charges depend on the provider."),
	}.get(storage_class, _("Provider restore time and charges may apply."))
