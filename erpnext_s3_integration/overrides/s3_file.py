import frappe
from frappe import _

from erpnext_s3_integration.object_storage.service import ObjectStorageService


class S3FileMixin:
	@property
	def is_remote_file(self):
		if _is_object_url(self.file_url):
			return True
		return super().is_remote_file

	def save_file(
		self,
		content=None,
		decode=False,
		ignore_existing_file_check=False,
		overwrite=False,
	):
		from erpnext_s3_integration.file_hooks import attachment_storage_enabled

		return super().save_file(
			content=content,
			decode=decode,
			ignore_existing_file_check=ignore_existing_file_check or attachment_storage_enabled(),
			overwrite=overwrite,
		)

	def get_content(self, encodings=None) -> bytes | str:
		if not _is_object_url(self.file_url):
			return super().get_content(encodings)
		if self.get("content"):
			self._content = self.content
			return self._content
		if self.is_new() and not self.flags.copy_from_existing_file:
			frappe.throw(
				_("A new File cannot read content from an arbitrary object storage URL."),
				exc=frappe.PermissionError,
			)

		service = ObjectStorageService(self.object_storage_profile)
		stream = service.backend.get(self.object_storage_key)
		try:
			self._content = stream.read()
		finally:
			stream.close()

		if encodings is None:
			from frappe.core.doctype.file.file import FILE_ENCODING_OPTIONS

			encodings = FILE_ENCODING_OPTIONS
		for encoding in encodings:
			try:
				self._content = self._content.decode(encoding)
				break
			except UnicodeDecodeError:
				continue
		return self._content

	def get_full_path(self):
		if _is_object_url(self.file_url):
			return self.file_url
		return super().get_full_path()

	def validate_file_path(self):
		if not _is_object_url(self.file_url):
			return super().validate_file_path()

	def validate_file_url(self):
		if not _is_object_url(self.file_url):
			return super().validate_file_url()

	def exists_on_disk(self):
		if not _is_object_url(self.file_url):
			return super().exists_on_disk()
		if not self.object_storage_profile or not self.object_storage_key:
			return False
		return (
			ObjectStorageService(self.object_storage_profile).backend.head(self.object_storage_key)
			is not None
		)

	def validate_file_on_disk(self):
		if _is_object_url(self.file_url):
			return True
		return super().validate_file_on_disk()

	def _delete_file_on_disk(self):
		if not _is_object_url(self.file_url):
			return super()._delete_file_on_disk()
		shared = frappe.db.exists(
			"File",
			{
				"object_storage_profile": self.object_storage_profile,
				"object_storage_key": self.object_storage_key,
				"name": ["!=", self.name],
			},
		)
		self.delete_file_data_content(only_thumbnail=bool(shared))

	def generate_content_hash(self):
		if not _is_object_url(self.file_url):
			return super().generate_content_hash()

	def handle_is_private_changed(self):
		if _is_object_url(self.file_url):
			frappe.throw(
				_("Changing object storage visibility is not supported. Re-upload the File instead."),
				exc=frappe.ValidationError,
			)
		return super().handle_is_private_changed()


def _is_object_url(file_url: str | None) -> bool:
	return bool(file_url and file_url.startswith("/s3/"))
