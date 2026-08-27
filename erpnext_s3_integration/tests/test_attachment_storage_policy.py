import io
import zipfile
from unittest.mock import MagicMock, patch

import frappe
from frappe.tests import UnitTestCase

from erpnext_s3_integration.file_hooks import write_file_to_object_storage
from erpnext_s3_integration.object_urls import build_object_url, object_key_candidates
from erpnext_s3_integration.overrides.s3_file import S3FileMixin
from erpnext_s3_integration.storage_policy import StorageTarget, get_storage_target, storage_context

FILESYSTEM_REQUIRED_DOCTYPES = (
	"Transaction Deletion Record",
	"Import Supplier Invoice",
	"Chart of Accounts Importer",
	"Bank Statement Import",
	"Repost Item Valuation",
)


class TestStoragePolicy(UnitTestCase):
	def test_storage_targets_are_centralized(self):
		self.assertEqual(
			get_storage_target(frappe._dict(attached_to_doctype="Prepared Report")),
			StorageTarget.LOCAL_TEMPORARY,
		)
		for doctype in FILESYSTEM_REQUIRED_DOCTYPES:
			with self.subTest(doctype=doctype):
				self.assertEqual(
					get_storage_target(frappe._dict(attached_to_doctype=doctype)),
					StorageTarget.LOCAL_FILESYSTEM_REQUIRED,
				)
		self.assertEqual(
			get_storage_target(frappe._dict(attached_to_doctype="Sales Invoice")),
			StorageTarget.OBJECT_STORAGE,
		)

	@patch("erpnext_s3_integration.file_hooks.ObjectStorageService")
	@patch("erpnext_s3_integration.file_hooks.attachment_storage_enabled", return_value=True)
	def test_prepared_report_uses_private_filesystem_when_oss_is_enabled(
		self, _enabled, service_class
	):
		save_local = MagicMock(return_value={"file_url": "/private/files/report.json.gz"})
		file_doc = frappe._dict(
			doctype="File",
			attached_to_doctype="Prepared Report",
			save_file_on_filesystem=save_local,
		)

		result = write_file_to_object_storage(file_doc)

		self.assertEqual(result["file_url"], "/private/files/report.json.gz")
		save_local.assert_called_once_with()
		service_class.assert_not_called()

	@patch("erpnext_s3_integration.file_hooks.ObjectStorageService")
	@patch("erpnext_s3_integration.file_hooks.attachment_storage_enabled", return_value=True)
	def test_path_dependent_doctypes_do_not_call_object_storage(self, _enabled, service_class):
		for doctype in FILESYSTEM_REQUIRED_DOCTYPES:
			with self.subTest(doctype=doctype):
				save_local = MagicMock(return_value={"file_url": "/private/files/import.csv"})
				file_doc = frappe._dict(
					doctype="File",
					attached_to_doctype=doctype,
					save_file_on_filesystem=save_local,
				)
				write_file_to_object_storage(file_doc)
				save_local.assert_called_once_with()
		service_class.assert_not_called()

	def test_virtual_url_sanitizes_filename_and_preserves_hash_key(self):
		key = "attachments/private/40/bf/40bfd238"
		self.assertEqual(
			build_object_url(key, "发票%?#/副本.pdf"),
			"/s3/attachments/private/40/bf/40bfd238/发票____副本.pdf",
		)
		self.assertEqual(
			object_key_candidates(f"{key}/invoice.pdf"),
			(f"{key}/invoice.pdf", key),
		)

	@patch("erpnext_s3_integration.file_hooks.ObjectStorageService")
	@patch("erpnext_s3_integration.file_hooks.frappe.utils.file_manager.save_file_on_filesystem")
	@patch("erpnext_s3_integration.file_hooks.attachment_storage_enabled", return_value=True)
	def test_legacy_bank_conversion_context_uses_local_filesystem(
		self, _enabled, save_local, service_class
	):
		save_local.return_value = {"file_url": "/private/files/converted.csv"}

		with storage_context("Bank Statement Import"):
			result = write_file_to_object_storage("converted.csv", b"content", is_private=1)

		self.assertEqual(result["file_url"], "/private/files/converted.csv")
		save_local.assert_called_once_with(
			"converted.csv", b"content", content_type=None, is_private=1
		)
		service_class.assert_not_called()


class TestObjectStorageZip(UnitTestCase):
	@patch("frappe.core.api.file.get_max_extract_size", return_value=1024)
	@patch("erpnext_s3_integration.overrides.s3_file.frappe.delete_doc")
	@patch("erpnext_s3_integration.overrides.s3_file.frappe.new_doc")
	def test_object_zip_extracts_from_memory(self, new_doc, delete_doc, _max_size):
		content = _zip_content({"folder/report.csv": b"a,b\n1,2", ".hidden": b"skip"})
		child = MagicMock(name="child")
		child.name = "FILE-CHILD"
		new_doc.return_value = child
		source = _RemoteZip(content)

		files = source.unzip()

		self.assertEqual(files, [child])
		self.assertEqual(child.file_name, "report.csv")
		self.assertEqual(child.content, b"a,b\n1,2")
		child.save.assert_called_once_with()
		delete_doc.assert_called_once_with("File", "FILE-ZIP")

	@patch("frappe.core.api.file.get_max_extract_size", return_value=1024)
	@patch("erpnext_s3_integration.overrides.s3_file.frappe.delete_doc")
	@patch("erpnext_s3_integration.overrides.s3_file.frappe.new_doc")
	def test_object_zip_rolls_back_created_children(self, new_doc, delete_doc, _max_size):
		first = MagicMock()
		first.name = "FILE-1"
		second = MagicMock()
		second.name = "FILE-2"
		second.save.side_effect = RuntimeError("write failed")
		new_doc.side_effect = [first, second]

		with self.assertRaisesRegex(RuntimeError, "write failed"):
			_RemoteZip(_zip_content({"one.txt": b"1", "two.txt": b"2"})).unzip()

		delete_doc.assert_called_once_with("File", "FILE-1", ignore_permissions=True, force=True)

	@patch("frappe.core.api.file.get_max_extract_size", return_value=1)
	@patch("erpnext_s3_integration.overrides.s3_file.frappe.throw", side_effect=ValueError)
	@patch("erpnext_s3_integration.overrides.s3_file.frappe.new_doc")
	def test_object_zip_rejects_declared_size_over_limit(self, new_doc, _throw, _max_size):
		with self.assertRaises(ValueError):
			_RemoteZip(_zip_content({"large.txt": b"12"})).unzip()
		new_doc.assert_not_called()


class _RemoteZip(S3FileMixin):
	file_url = "/s3/attachments/private/key/archive.zip"
	file_name = "archive.zip"
	name = "FILE-ZIP"
	folder = "Home/Attachments"
	is_private = 1
	attached_to_doctype = "Sales Invoice"
	attached_to_name = "SINV-1"

	def __init__(self, content):
		self._zip_content = content

	def get_content(self, encodings=None):
		return self._zip_content


def _zip_content(files: dict[str, bytes]) -> bytes:
	buffer = io.BytesIO()
	with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
		for file_name, content in files.items():
			archive.writestr(file_name, content)
	return buffer.getvalue()
