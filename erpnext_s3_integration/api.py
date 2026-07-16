import mimetypes
import os

import frappe
from frappe.core.doctype.file.utils import find_file_by_url
from frappe.utils.response import FORCE_DOWNLOAD_EXTENSIONS
from werkzeug.wrappers import Response
from werkzeug.wsgi import wrap_file


@frappe.whitelist(allow_guest=True, methods=["GET"])  # nosemgrep
def get_file():
	"""Serve an authorized S3-backed File through a stream or temporary redirect."""
	s3_key = frappe.form_dict.get("key")
	if not s3_key:
		raise frappe.PageDoesNotExistError()

	settings = frappe.get_single("S3 Integration Settings")
	file_doc = find_file_by_url(f"/s3/{s3_key}")
	if not file_doc:
		raise frappe.PermissionError()

	# If stream_from_s3 is enabled, stream it directly, otherwise return presigned URL redirect
	from erpnext_s3_integration.s3_client import S3Client

	s3_client = S3Client()

	if settings.stream_from_s3:
		try:
			stream = s3_client.download_as_stream(s3_key)
			response = Response(wrap_file(frappe.request.environ, stream), direct_passthrough=True)
			mime_type = mimetypes.guess_type(file_doc.file_name or "")[0] or "application/octet-stream"
			response.headers["Content-Type"] = mime_type
			response.headers["X-Content-Type-Options"] = "nosniff"
			response.headers["Cache-Control"] = (
				"private, no-store" if file_doc.is_private else "public, max-age=3600"
			)

			if os.path.splitext(file_doc.file_name or "")[1].lower() in FORCE_DOWNLOAD_EXTENSIONS:
				response.headers.add("Content-Disposition", "attachment", filename=file_doc.file_name)
				response.headers["Content-Security-Policy"] = "sandbox"
			return response
		except Exception as e:
			frappe.log_error(f"Error streaming file from S3: {e}")
			raise frappe.DoesNotExistError()
	else:
		# Return a temporary redirect to the S3 URL
		url = s3_client.generate_presigned_url(s3_key, expires_in=3600)
		if not url:
			raise frappe.DoesNotExistError()

		frappe.local.response["type"] = "redirect"
		frappe.local.response["location"] = url
