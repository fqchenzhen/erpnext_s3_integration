import frappe
from frappe import _


class S3FileMixin:
	def get_content(self, encodings=None) -> bytes | str:
		if self.file_url and self.file_url.startswith("/s3/"):
			if self.get("content"):
				self._content = self.content
				if getattr(self, "decode", False):
					from frappe.core.api.file import decode_file_content

					self._content = decode_file_content(self._content)
					self.decode = False
				return self._content
			if self.is_new() and not self.flags.copy_from_existing_file:
				frappe.throw(
					_("A new File cannot read content from an arbitrary S3 URL."),
					exc=frappe.PermissionError,
				)

			from erpnext_s3_integration.s3_client import S3Client

			s3_key = self.file_url.replace("/s3/", "", 1)
			s3_client = S3Client()

			stream = s3_client.download_as_stream(s3_key)
			try:
				self._content = stream.read()
			finally:
				stream.close()

			# looping will not result in slowdown, as the content is usually utf-8 or utf-8-sig
			# encoded so the first iteration will be enough most of the time
			if encodings is None:
				from frappe.core.doctype.file.file import FILE_ENCODING_OPTIONS

				encodings = FILE_ENCODING_OPTIONS

			for encoding in encodings:
				try:
					# read file with proper encoding if text
					self._content = self._content.decode(encoding)
					break
				except UnicodeDecodeError:
					# for .png, .jpg, etc
					continue

			return self._content

		return super().get_content(encodings)

	def get_full_path(self):
		if self.file_url and self.file_url.startswith("/s3/"):
			return self.file_url
		return super().get_full_path()

	def validate_file_path(self):
		if self.file_url and self.file_url.startswith("/s3/"):
			return
		super().validate_file_path()

	def validate_file_url(self):
		if self.file_url and self.file_url.startswith("/s3/"):
			return
		super().validate_file_url()

	def exists_on_disk(self):
		if self.file_url and self.file_url.startswith("/s3/"):
			from erpnext_s3_integration.s3_client import S3Client

			s3_key = self.file_url.replace("/s3/", "", 1)
			return S3Client().object_exists(s3_key)
		return super().exists_on_disk()

	def validate_file_on_disk(self):
		if self.file_url and self.file_url.startswith("/s3/"):
			return True
		return super().validate_file_on_disk()

	def _delete_file_on_disk(self):
		if self.file_url and self.file_url.startswith("/s3/"):
			shared_url = frappe.db.exists(
				"File",
				{"file_url": self.file_url, "name": ["!=", self.name]},
			)
			self.delete_file_data_content(only_thumbnail=bool(shared_url))
			return
		return super()._delete_file_on_disk()

	def generate_content_hash(self):
		if self.file_url and self.file_url.startswith("/s3/"):
			return
		super().generate_content_hash()

	def handle_is_private_changed(self):
		if self.file_url and self.file_url.startswith("/s3/"):
			frappe.throw(
				_(
					"Changing the visibility of an S3-backed File is not supported. Re-upload the file instead."
				),
				exc=frappe.ValidationError,
			)
		return super().handle_is_private_changed()
