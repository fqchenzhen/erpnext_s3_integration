"""Focused safety tests for legacy attachment migration."""

import hashlib
import io
import tempfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import frappe
from frappe.tests import UnitTestCase
from frappe.utils import add_days, now_datetime

from erpnext_s3_integration.attachment_migration import (
	_cleanup_url,
	_ensure_remote_file,
	_has_related_content_reference,
	_is_local_url,
	_linked_attach_fields,
	_local_path,
	_migrate_url,
	_replace_attachment_comment_urls,
	_replace_linked_attach_fields,
	_verify_remote,
)
from erpnext_s3_integration.object_storage.default_rules import DEFAULT_RULES
from erpnext_s3_integration.setup import _custom_fields
from erpnext_s3_integration.storage_policy import StorageTarget


class TestAttachmentMigration(UnitTestCase):
	def test_crm_rules_keep_active_attachments_online(self):
		policies = {rule.doctype: rule.policy for rule in DEFAULT_RULES}
		self.assertEqual(policies["CRM Lead"], "business-online")
		self.assertEqual(policies["CRM Deal"], "business-online")
		self.assertNotIn("Communication", policies)
		self.assertNotIn("Comment", policies)

	def test_migration_source_url_length_fits_frappe_limit(self):
		fields = {field["fieldname"]: field for field in _custom_fields()["File"]}
		self.assertLessEqual(fields["object_storage_source_url"]["length"], 1000)

	@patch("erpnext_s3_integration.attachment_migration._migration_refs")
	def test_oversized_source_url_is_rejected_before_upload(self, refs):
		with self.assertRaisesRegex(ValueError, "source URL field limit"):
			_migrate_url("/files/" + "x" * 1000, MagicMock())

		refs.assert_not_called()

	def test_local_paths_are_site_scoped(self):
		with tempfile.TemporaryDirectory() as root:
			local = Path(root) / "attachment.txt"
			local.write_bytes(b"content")
			with patch("frappe.get_site_path", return_value=root):
				self.assertEqual(_local_path("/files/attachment.txt"), local.resolve())
				for url in (
					"/files/../secret",
					"/files//etc/passwd",
					"/files/",
					"/private/files/../../etc/passwd",
				):
					with self.subTest(url=url), self.assertRaises(ValueError):
						_local_path(url)

	def test_symlinked_attachment_is_rejected(self):
		with tempfile.TemporaryDirectory() as root, tempfile.TemporaryDirectory() as outside:
			(Path(outside) / "source").write_bytes(b"content")
			(Path(root) / "link").symlink_to(Path(outside) / "source")
			with patch("frappe.get_site_path", return_value=root):
				with self.assertRaises(ValueError):
					_local_path("/files/link")

	def test_only_actual_file_urls_are_in_scope(self):
		self.assertTrue(_is_local_url("/files/image.png"))
		self.assertTrue(_is_local_url("/private/files/document.pdf"))
		self.assertFalse(_is_local_url("/s3/attachments/private/abc"))
		self.assertFalse(_is_local_url("https://example.com/file.pdf"))

	def test_remote_must_match_size_and_bytes(self):
		backend = MagicMock()
		backend.head.return_value = SimpleNamespace(size=3)
		backend.get.side_effect = lambda key: io.BytesIO(b"abc")
		service = SimpleNamespace(backend=backend)
		digest = hashlib.md5(b"abc", usedforsecurity=False).hexdigest()
		_verify_remote(service, "key", digest, 3)
		with self.assertRaisesRegex(ValueError, "verification failed"):
			_verify_remote(service, "key", hashlib.md5(b"xyz", usedforsecurity=False).hexdigest(), 3)
		with self.assertRaisesRegex(ValueError, "size mismatch"):
			_verify_remote(service, "key", digest, 4)

	def test_interrupted_upload_is_repaired_on_retry(self):
		class PartialUploadBackend:
			data = None
			writes = 0

			def head(self, _key):
				return SimpleNamespace(size=len(self.data)) if self.data is not None else None

			def get(self, _key):
				return io.BytesIO(self.data)

			def put(self, _key, body, **_kwargs):
				self.writes += 1
				payload = body.read()
				self.data = payload[:2] if self.writes == 1 else payload
				if self.writes == 1:
					raise ConnectionError("connection dropped after partial write")

		backend = PartialUploadBackend()
		service = SimpleNamespace(backend=backend, put=backend.put)
		payload = b"complete payload"
		digest = hashlib.md5(payload, usedforsecurity=False).hexdigest()
		with tempfile.TemporaryDirectory() as root:
			path = Path(root) / "attachment.txt"
			path.write_bytes(payload)
			with self.assertRaises(ConnectionError):
				_ensure_remote_file(path, service, "key", digest, len(payload), path.name)
			_ensure_remote_file(path, service, "key", digest, len(payload), path.name)

		self.assertEqual(backend.writes, 2)
		self.assertEqual(backend.data, payload)

	def test_retry_does_not_upload_if_local_source_changed(self):
		backend = MagicMock()
		backend.head.return_value = SimpleNamespace(size=3)
		backend.get.return_value = io.BytesIO(b"bad")
		service = SimpleNamespace(backend=backend, put=MagicMock())
		expected = hashlib.md5(b"old", usedforsecurity=False).hexdigest()
		with tempfile.TemporaryDirectory() as root:
			path = Path(root) / "attachment.txt"
			path.write_bytes(b"new")
			with self.assertRaisesRegex(ValueError, "changed during migration"):
				_ensure_remote_file(path, service, "key", expected, 3, path.name)

		service.put.assert_not_called()

	@patch("erpnext_s3_integration.attachment_migration._migration_refs")
	@patch("erpnext_s3_integration.attachment_migration.get_storage_target")
	def test_filesystem_dependent_files_are_not_migrated(self, target, refs):
		target.return_value = StorageTarget.LOCAL_FILESYSTEM_REQUIRED
		refs.return_value = [
			frappe._dict(is_folder=0, is_private=1, attached_to_doctype="Bank Statement Import")
		]
		self.assertIsNone(_migrate_url("/private/files/import.csv", MagicMock()))

	@patch("erpnext_s3_integration.attachment_migration.frappe.db.exists", return_value=True)
	def test_cleanup_never_removes_a_still_referenced_local_url(self, exists):
		self.assertFalse(_cleanup_url("/files/still-used.pdf"))
		exists.assert_called_once_with("File", {"file_url": "/files/still-used.pdf"})

	def test_cleanup_keeps_source_changed_during_remote_verification(self):
		with tempfile.TemporaryDirectory() as root:
			path = Path(root) / "attachment.txt"
			path.write_bytes(b"original")
			digest = hashlib.md5(b"original", usedforsecurity=False).hexdigest()
			ref = frappe._dict(
				name="FILE-1",
				file_url="/s3/object",
				object_storage_profile="Test Profile",
				object_storage_key=f"attachments/public/ab/cd/{digest}",
				object_storage_migrated_at=add_days(now_datetime(), -8),
				object_storage_local_cleaned_at=None,
				attached_to_doctype="CRM Lead",
				attached_to_name="LEAD-1",
			)
			with (
				patch("erpnext_s3_integration.attachment_migration.frappe.db.exists", return_value=False),
				patch(
					"erpnext_s3_integration.attachment_migration.frappe.get_all",
					side_effect=[[ref], [ref]],
				),
				patch("erpnext_s3_integration.attachment_migration._linked_attach_fields", return_value=[]),
				patch(
					"erpnext_s3_integration.attachment_migration._has_related_content_reference",
					return_value=False,
				),
				patch("erpnext_s3_integration.attachment_migration._local_path", return_value=path),
				patch("erpnext_s3_integration.attachment_migration.ObjectStorageService"),
				patch(
					"erpnext_s3_integration.attachment_migration._verify_remote",
					side_effect=lambda *_args, **_kwargs: path.write_bytes(b"updated"),
				),
			):
				self.assertFalse(_cleanup_url("/files/attachment.txt"))

			self.assertEqual(path.read_bytes(), b"updated")

	def test_cleanup_rechecks_local_file_references_after_remote_verification(self):
		with tempfile.TemporaryDirectory() as root:
			path = Path(root) / "attachment.txt"
			path.write_bytes(b"original")
			digest = hashlib.md5(b"original", usedforsecurity=False).hexdigest()
			ref = frappe._dict(
				name="FILE-1",
				file_url="/s3/object",
				object_storage_profile="Test Profile",
				object_storage_key=f"attachments/public/ab/cd/{digest}",
				object_storage_migrated_at=add_days(now_datetime(), -8),
				object_storage_local_cleaned_at=None,
				attached_to_doctype="CRM Lead",
				attached_to_name="LEAD-1",
			)
			with (
				patch(
					"erpnext_s3_integration.attachment_migration.frappe.db.exists",
					side_effect=[False, True],
				),
				patch(
					"erpnext_s3_integration.attachment_migration.frappe.get_all",
					return_value=[ref],
				),
				patch("erpnext_s3_integration.attachment_migration._linked_attach_fields", return_value=[]),
				patch(
					"erpnext_s3_integration.attachment_migration._has_related_content_reference",
					return_value=False,
				),
				patch("erpnext_s3_integration.attachment_migration._local_path", return_value=path),
				patch("erpnext_s3_integration.attachment_migration.ObjectStorageService"),
				patch("erpnext_s3_integration.attachment_migration._verify_remote"),
			):
				self.assertFalse(_cleanup_url("/files/attachment.txt"))

			self.assertEqual(path.read_bytes(), b"original")

	@patch("erpnext_s3_integration.attachment_migration.frappe.get_doc")
	@patch("erpnext_s3_integration.attachment_migration.frappe.db.exists", return_value=True)
	def test_attach_fields_include_child_table_rows(self, _exists, get_doc):
		child_meta = SimpleNamespace(fields=[SimpleNamespace(fieldtype="Attach Image", fieldname="evidence")])
		child = frappe._dict(
			doctype="Invoice Item",
			name="ROW-1",
			meta=child_meta,
			evidence="/files/old.png",
		)
		parent_meta = SimpleNamespace(
			fields=[SimpleNamespace(fieldtype="Attach", fieldname="signed_copy")],
			get_table_fields=lambda: [SimpleNamespace(fieldname="items")],
		)
		parent = frappe._dict(
			meta=parent_meta,
			signed_copy="/files/old.png",
			items=[child],
		)
		get_doc.return_value = parent
		refs = [frappe._dict(attached_to_doctype="Sales Invoice", attached_to_name="INV-1")]

		self.assertEqual(
			_linked_attach_fields(refs, "/files/old.png"),
			[
				("Sales Invoice", "INV-1", "signed_copy"),
				("Invoice Item", "ROW-1", "evidence"),
			],
		)

	@patch("erpnext_s3_integration.attachment_migration.frappe.db.set_value")
	@patch("erpnext_s3_integration.attachment_migration.frappe.get_meta")
	@patch("erpnext_s3_integration.attachment_migration._linked_attach_fields")
	def test_attach_field_update_is_conditional_on_old_url(self, linked_fields, get_meta, set_value):
		linked_fields.return_value = [("Sales Invoice", "INV-1", "signed_copy")]
		get_meta.return_value = SimpleNamespace(issingle=False)

		_replace_linked_attach_fields([], "/files/old.pdf", "/s3/key/old.pdf")

		set_value.assert_called_once_with(
			"Sales Invoice",
			{"name": "INV-1", "signed_copy": "/files/old.pdf"},
			"signed_copy",
			"/s3/key/old.pdf",
			update_modified=False,
		)

	@patch("erpnext_s3_integration.attachment_migration.frappe.db.set_value")
	@patch("erpnext_s3_integration.attachment_migration.frappe.get_all")
	def test_generated_attachment_comment_uses_object_url(self, get_all, set_value):
		old_url = "/files/合同 副本.pdf"
		encoded_old_url = "/files/%E5%90%88%E5%90%8C%20%E5%89%AF%E6%9C%AC.pdf"
		get_all.return_value = [
			frappe._dict(name="COMMENT-1", content=f"<a href='{encoded_old_url}'>合同 副本.pdf</a>")
		]
		refs = [frappe._dict(attached_to_doctype="CRM Lead", attached_to_name="LEAD-1")]

		_replace_attachment_comment_urls(refs, old_url, "/s3/key/合同_副本.pdf")

		set_value.assert_called_once_with(
			"Comment",
			{
				"name": "COMMENT-1",
				"content": f"<a href='{encoded_old_url}'>合同 副本.pdf</a>",
			},
			"content",
			"<a href='/s3/key/%E5%90%88%E5%90%8C_%E5%89%AF%E6%9C%AC.pdf'>合同 副本.pdf</a>",
			update_modified=False,
		)

	@patch("erpnext_s3_integration.attachment_migration.frappe.db.exists", return_value=True)
	def test_related_comment_or_communication_url_blocks_cleanup(self, exists):
		refs = [frappe._dict(attached_to_doctype="CRM Lead", attached_to_name="LEAD-1")]

		self.assertTrue(_has_related_content_reference(refs, "/files/old.pdf"))

		exists.assert_called_once_with(
			"Comment",
			{
				"reference_doctype": "CRM Lead",
				"reference_name": "LEAD-1",
				"content": ["like", "%/files/old.pdf%"],
			},
		)

	def test_related_communication_body_blocks_cleanup(self):
		refs = [frappe._dict(attached_to_doctype="CRM Lead", attached_to_name="LEAD-1")]
		with patch(
			"erpnext_s3_integration.attachment_migration.frappe.db.exists",
			side_effect=[False, True],
		) as exists:
			self.assertTrue(_has_related_content_reference(refs, "/files/old.pdf"))

		self.assertEqual([call.args[0] for call in exists.call_args_list], ["Comment", "Communication"])

	def test_cleanup_protects_communication_and_comment_file_sources(self):
		for source_doctype in ("Communication", "Comment"):
			with self.subTest(source_doctype=source_doctype):
				with (
					patch("erpnext_s3_integration.attachment_migration.frappe.db.exists", return_value=False),
					patch(
						"erpnext_s3_integration.attachment_migration.frappe.get_all",
						return_value=[frappe._dict(attached_to_doctype=source_doctype)],
					),
				):
					self.assertFalse(_cleanup_url("/files/old.pdf"))
