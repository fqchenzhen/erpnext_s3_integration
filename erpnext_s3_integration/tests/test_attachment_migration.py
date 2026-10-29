"""Focused safety tests for legacy attachment migration."""

import hashlib
import io
import tempfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import ANY, MagicMock, patch

import frappe
from frappe.tests import UnitTestCase
from frappe.utils import add_days, now_datetime

from erpnext_s3_integration.attachment_migration import (
	_acquire_transfer_lock,
	_cleanup_url,
	_ensure_remote_file,
	_has_related_content_reference,
	_is_local_url,
	_linked_attach_fields,
	_local_path,
	_migrate_url,
	_migration_ineligibility,
	_release_transfer_lock,
	_renew_transfer_lock,
	_replace_attachment_comment_urls,
	_replace_linked_attach_fields,
	_scan_local_files,
	_transfer_lock_key,
	_verify_remote,
	cleanup_batch,
	migrate_batch,
	start_local_cleanup,
	start_local_migration,
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

	def test_valid_crm_lead_and_deal_files_migrate_without_deleting_local_source(self):
		for doctype, name in (("CRM Lead", "LEAD-1"), ("CRM Deal", "DEAL-1")):
			with self.subTest(doctype=doctype), tempfile.TemporaryDirectory() as root:
				path = Path(root) / "contract.pdf"
				payload = b"verified attachment"
				path.write_bytes(payload)
				digest = hashlib.md5(payload, usedforsecurity=False).hexdigest()
				url = "/files/contract.pdf"
				new_url = "/s3/attachments/public/contract.pdf"
				key = f"attachments/public/{digest}"
				ref = frappe._dict(
					name=f"FILE-{name}",
					file_name=path.name,
					is_folder=0,
					is_private=0,
					attached_to_doctype=doctype,
					attached_to_name=name,
				)
				service = SimpleNamespace(profile=SimpleNamespace(name="Profile", prefix="attachments"))

				with (
					patch("erpnext_s3_integration.attachment_migration._migration_refs", return_value=[ref]),
					patch("erpnext_s3_integration.attachment_migration.frappe.db.exists", return_value=True),
					patch(
						"erpnext_s3_integration.attachment_migration.get_storage_target",
						return_value=StorageTarget.OBJECT_STORAGE,
					),
					patch("erpnext_s3_integration.attachment_migration._local_path", return_value=path),
					patch("erpnext_s3_integration.attachment_migration._ensure_remote_file") as ensure_remote,
					patch(
						"erpnext_s3_integration.attachment_migration.generate_object_key",
						return_value=key,
					),
					patch(
						"erpnext_s3_integration.attachment_migration.build_object_url",
						return_value=new_url,
					),
					patch("erpnext_s3_integration.attachment_migration._replace_linked_attach_fields"),
					patch("erpnext_s3_integration.attachment_migration._replace_attachment_comment_urls"),
					patch("erpnext_s3_integration.attachment_migration.frappe.db.set_value") as set_value,
					patch("erpnext_s3_integration.attachment_migration.frappe.clear_document_cache"),
					patch("erpnext_s3_integration.attachment_migration.frappe.enqueue"),
				):
					self.assertEqual(_migrate_url(url, service), (key, len(payload)))

				ensure_remote.assert_called_once_with(path, service, key, digest, len(payload), path.name)
				set_value.assert_called_once_with(
					"File",
					ref.name,
					{
						"file_url": new_url,
						"object_storage_profile": "Profile",
						"object_storage_key": key,
						"object_storage_source_url": url,
						"object_storage_migrated_at": ANY,
						"content_hash": None,
					},
					update_modified=False,
				)
				self.assertTrue(path.is_file())

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

	def test_cleanup_respects_seven_day_grace_period(self):
		ref = frappe._dict(
			object_storage_migrated_at=add_days(now_datetime(), -6),
			object_storage_local_cleaned_at=None,
			attached_to_doctype="CRM Lead",
		)
		with (
			patch("erpnext_s3_integration.attachment_migration.frappe.db.exists", return_value=False),
			patch("erpnext_s3_integration.attachment_migration.frappe.get_all", return_value=[ref]),
		):
			self.assertFalse(_cleanup_url("/files/recent.pdf"))

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

	def test_unattached_or_unknown_file_is_not_eligible(self):
		with patch(
			"erpnext_s3_integration.attachment_migration.get_storage_target",
			return_value=StorageTarget.OBJECT_STORAGE,
		):
			self.assertEqual(
				_migration_ineligibility(
					frappe._dict(is_folder=0, attached_to_doctype=None, attached_to_name=None)
				),
				"unknown",
			)
			with patch("erpnext_s3_integration.attachment_migration.frappe.db.exists", return_value=False):
				self.assertEqual(
					_migration_ineligibility(
						frappe._dict(is_folder=0, attached_to_doctype="CRM Lead", attached_to_name="missing")
					),
					"unknown",
				)

	def test_prepared_report_files_remain_protected(self):
		with patch(
			"erpnext_s3_integration.attachment_migration.get_storage_target",
			return_value=StorageTarget.LOCAL_TEMPORARY,
		):
			self.assertEqual(
				_migration_ineligibility(
					frappe._dict(is_folder=0, attached_to_doctype="Prepared Report", attached_to_name="R-1")
				),
				"protected",
			)

	@patch("erpnext_s3_integration.attachment_migration._migration_refs")
	def test_shared_url_with_protected_file_is_not_migrated(self, refs):
		refs.return_value = [
			frappe._dict(
				is_folder=0,
				is_private=0,
				attached_to_doctype="CRM Lead",
				attached_to_name="LEAD-1",
			),
			frappe._dict(
				is_folder=0,
				is_private=0,
				attached_to_doctype="Prepared Report",
				attached_to_name="REPORT-1",
			),
		]
		with (
			patch("erpnext_s3_integration.attachment_migration.frappe.db.exists", return_value=True),
			patch(
				"erpnext_s3_integration.attachment_migration.get_storage_target",
				side_effect=lambda ref: (
					StorageTarget.LOCAL_TEMPORARY
					if ref.attached_to_doctype == "Prepared Report"
					else StorageTarget.OBJECT_STORAGE
				),
			),
			patch("erpnext_s3_integration.attachment_migration._local_path") as local_path,
		):
			self.assertIsNone(_migrate_url("/files/shared.pdf", MagicMock()))

		local_path.assert_not_called()

	@patch("erpnext_s3_integration.attachment_migration._migration_refs")
	def test_migration_skips_unattached_local_files(self, refs):
		refs.return_value = [
			frappe._dict(is_folder=0, is_private=0, attached_to_doctype=None, attached_to_name=None)
		]
		self.assertIsNone(_migrate_url("/files/system.json", MagicMock()))

	def test_preview_categories_are_conservative(self):
		rows = [
			frappe._dict(
				name="1",
				file_name="contract.pdf",
				file_url="/files/a.pdf",
				is_folder=0,
				is_private=0,
				attached_to_doctype="CRM Deal",
				attached_to_name="D-1",
			),
			frappe._dict(
				name="2",
				file_name="report.json.gz",
				file_url="/files/b.gz",
				is_folder=0,
				is_private=0,
				attached_to_doctype="Prepared Report",
				attached_to_name="P-1",
			),
			frappe._dict(
				name="3",
				file_name="system.json",
				file_url="/files/c.json",
				is_folder=0,
				is_private=0,
				attached_to_doctype=None,
				attached_to_name=None,
			),
			frappe._dict(
				name="4",
				file_name="missing.pdf",
				file_url="/files/missing.pdf",
				is_folder=0,
				is_private=0,
				attached_to_doctype="CRM Lead",
				attached_to_name="L-1",
			),
		]
		with (
			patch("erpnext_s3_integration.attachment_migration.frappe.get_all", return_value=rows),
			patch("erpnext_s3_integration.attachment_migration.frappe.db.exists", return_value=True),
			patch(
				"erpnext_s3_integration.attachment_migration.get_storage_target",
				side_effect=lambda ref: (
					StorageTarget.LOCAL_TEMPORARY
					if ref.attached_to_doctype == "Prepared Report"
					else StorageTarget.OBJECT_STORAGE
				),
			),
			patch("erpnext_s3_integration.attachment_migration._local_path") as path,
		):
			path.side_effect = lambda url: SimpleNamespace(is_file=lambda: "missing" not in url)
			preview = _scan_local_files()
		self.assertEqual(preview["counts"], {"eligible": 1, "protected": 1, "unknown": 1, "missing": 1})
		self.assertFalse(preview["truncated"])

	def test_truncated_preview_does_not_claim_eligibility_for_shared_url(self):
		rows = [
			frappe._dict(
				name="1",
				file_name="contract.pdf",
				file_url="/files/shared.pdf",
				is_folder=0,
				is_private=0,
				attached_to_doctype="CRM Deal",
				attached_to_name="D-1",
			),
			frappe._dict(
				name="2",
				file_name="other.pdf",
				file_url="/files/other.pdf",
				is_folder=0,
				is_private=0,
				attached_to_doctype="CRM Lead",
				attached_to_name="L-1",
			),
			frappe._dict(
				name="3",
				file_name="report.json.gz",
				file_url="/files/shared.pdf",
				is_folder=0,
				is_private=0,
				attached_to_doctype="Prepared Report",
				attached_to_name="P-1",
			),
		]
		with (
			patch("erpnext_s3_integration.attachment_migration.SCAN_LIMIT", 2),
			patch("erpnext_s3_integration.attachment_migration.frappe.get_all", return_value=rows) as get_all,
			patch("erpnext_s3_integration.attachment_migration.frappe.db.exists", return_value=True),
			patch(
				"erpnext_s3_integration.attachment_migration.get_storage_target",
				return_value=StorageTarget.OBJECT_STORAGE,
			),
			patch("erpnext_s3_integration.attachment_migration._local_path") as path,
		):
			path.return_value.is_file.return_value = True
			preview = _scan_local_files()

		self.assertTrue(preview["truncated"])
		self.assertEqual(preview["scanned_records"], 2)
		self.assertEqual(preview["counts"]["eligible"], 0)
		self.assertEqual(preview["counts"]["unknown"], 2)
		self.assertTrue(all(item["status"] == "unknown" for item in preview["sample"]))
		self.assertEqual(get_all.call_args.kwargs["limit"], 3)
		self.assertNotIn("limit_page_length", get_all.call_args.kwargs)

	@patch("erpnext_s3_integration.attachment_migration._require_manager")
	@patch("erpnext_s3_integration.attachment_migration._service")
	@patch(
		"erpnext_s3_integration.attachment_migration._scan_local_files",
		return_value={"truncated": False, "counts": {"eligible": 1}},
	)
	@patch("erpnext_s3_integration.attachment_migration._acquire_transfer_lock", return_value=None)
	@patch("erpnext_s3_integration.attachment_migration.frappe.enqueue")
	def test_shared_lock_blocks_overlapping_transfer_jobs(self, enqueue, _lock, _scan, _service, _manager):
		self.assertEqual(start_local_migration(), {"queued": False, "running": True})
		self.assertEqual(start_local_cleanup(confirm=1), {"queued": False, "running": True})
		enqueue.assert_not_called()

	@patch("erpnext_s3_integration.attachment_migration._renew_transfer_lock", return_value=False)
	@patch("erpnext_s3_integration.attachment_migration._local_files")
	@patch("erpnext_s3_integration.attachment_migration._cleanup_entries")
	def test_expired_lock_blocks_stale_batch(self, cleanup_entries, local_files, renew):
		migrate_batch(run_token="old-token")
		cleanup_batch(run_token="old-token")
		self.assertEqual(renew.call_count, 2)
		local_files.assert_not_called()
		cleanup_entries.assert_not_called()

	@patch("erpnext_s3_integration.attachment_migration.frappe.cache")
	@patch("erpnext_s3_integration.attachment_migration.frappe.db.after_rollback")
	def test_lock_acquisition_is_atomic_and_expires(self, _rollback, cache_method):
		cache = cache_method.return_value
		cache.make_key.return_value = b"test-db|attachment-transfer"
		cache.set.return_value = True
		token = _acquire_transfer_lock()
		self.assertEqual(len(token), 32)
		cache.set.assert_called_once_with(b"test-db|attachment-transfer", token, nx=True, ex=3600)
		_rollback.add.assert_called_once()

	@patch("erpnext_s3_integration.attachment_migration.frappe.cache")
	def test_empty_token_cannot_renew_lock(self, cache_method):
		self.assertFalse(_renew_transfer_lock(None))
		cache_method.assert_not_called()

	@patch("erpnext_s3_integration.attachment_migration.frappe.db.after_rollback")
	def test_redis_transfer_lock_is_atomic_and_owner_scoped(self, _rollback):
		token = _acquire_transfer_lock()
		self.assertIsNotNone(token)
		key = _transfer_lock_key()
		try:
			self.assertIsNone(_acquire_transfer_lock())
			self.assertTrue(_renew_transfer_lock(token))
			self.assertGreaterEqual(frappe.cache().ttl(key), 3590)
			_release_transfer_lock("another-owner")
			self.assertEqual(frappe.cache().get(key), token.encode())
		finally:
			_release_transfer_lock(token)

		self.assertIsNone(frappe.cache().get(key))
		self.assertFalse(_renew_transfer_lock(token))

	def test_no_eligible_files_does_not_queue_migration(self):
		with (
			patch("erpnext_s3_integration.attachment_migration._require_manager"),
			patch("erpnext_s3_integration.attachment_migration._service"),
			patch(
				"erpnext_s3_integration.attachment_migration._scan_local_files",
				return_value={"truncated": False, "counts": {"eligible": 0}},
			),
			patch("erpnext_s3_integration.attachment_migration._acquire_transfer_lock") as lock,
			patch("erpnext_s3_integration.attachment_migration.frappe.enqueue") as enqueue,
		):
			self.assertEqual(start_local_migration(), {"queued": False, "no_eligible": True})
			lock.assert_not_called()
			enqueue.assert_not_called()

	def test_truncated_preview_does_not_block_migration(self):
		with (
			patch("erpnext_s3_integration.attachment_migration._require_manager"),
			patch("erpnext_s3_integration.attachment_migration._service"),
			patch(
				"erpnext_s3_integration.attachment_migration._scan_local_files",
				return_value={"truncated": True, "counts": {"eligible": 0}},
			),
			patch("erpnext_s3_integration.attachment_migration._acquire_transfer_lock", return_value="token"),
			patch("erpnext_s3_integration.attachment_migration.frappe.enqueue") as enqueue,
		):
			self.assertEqual(start_local_migration(), {"queued": True})
			enqueue.assert_called_once()

	def test_cleanup_skips_are_recorded_in_sync_log(self):
		with (
			patch("erpnext_s3_integration.attachment_migration._renew_transfer_lock", return_value=True),
			patch(
				"erpnext_s3_integration.attachment_migration._cleanup_entries",
				return_value=[frappe._dict(name="F-1", object_storage_source_url="/files/recent.pdf")],
			),
			patch("erpnext_s3_integration.attachment_migration._cleanup_url", return_value=False),
			patch("erpnext_s3_integration.attachment_migration.frappe.db.savepoint"),
			patch("erpnext_s3_integration.attachment_migration._log") as log,
			patch("erpnext_s3_integration.attachment_migration._finish_transfer_lock"),
		):
			cleanup_batch(run_token="token")
			log.assert_called_once_with(
				"Skipped",
				"Local Attachment Cleanup Batch",
				"Start",
				message=(
					"Source URLs: 1; cleaned: 0; skipped: 1; failed: 0. "
					"Skipped copies may still be in the 7-day grace period or have active references."
				),
			)

	def test_empty_cleanup_still_records_batch_result(self):
		with (
			patch("erpnext_s3_integration.attachment_migration._renew_transfer_lock", return_value=True),
			patch("erpnext_s3_integration.attachment_migration._cleanup_entries", return_value=[]),
			patch("erpnext_s3_integration.attachment_migration._log") as log,
			patch("erpnext_s3_integration.attachment_migration._finish_transfer_lock"),
		):
			cleanup_batch(run_token="token")
			self.assertEqual(log.call_args.args[:3], ("Skipped", "Local Attachment Cleanup Batch", "Start"))
			self.assertIn("cleaned: 0", log.call_args.kwargs["message"])

	def test_source_change_after_remote_verification_never_switches_file_url(self):
		with tempfile.TemporaryDirectory() as root:
			path = Path(root) / "contract.pdf"
			path.write_bytes(b"original")
			ref = frappe._dict(
				name="FILE-1",
				file_name=path.name,
				is_folder=0,
				is_private=0,
				attached_to_doctype="CRM Lead",
				attached_to_name="LEAD-1",
			)
			service = SimpleNamespace(profile=SimpleNamespace(name="Profile", prefix="prefix"))
			with (
				patch("erpnext_s3_integration.attachment_migration._migration_refs", return_value=[ref]),
				patch("erpnext_s3_integration.attachment_migration.frappe.db.exists", return_value=True),
				patch(
					"erpnext_s3_integration.attachment_migration.get_storage_target",
					return_value=StorageTarget.OBJECT_STORAGE,
				),
				patch("erpnext_s3_integration.attachment_migration._local_path", return_value=path),
				patch(
					"erpnext_s3_integration.attachment_migration._ensure_remote_file",
					side_effect=lambda *_args: path.write_bytes(b"modified"),
				),
				patch("erpnext_s3_integration.attachment_migration._replace_linked_attach_fields") as linked,
				patch("erpnext_s3_integration.attachment_migration.frappe.db.set_value") as set_value,
			):
				with self.assertRaisesRegex(ValueError, "Local file changed during migration"):
					_migrate_url("/files/contract.pdf", service)
				linked.assert_not_called()
				set_value.assert_not_called()

	def test_source_replaced_with_symlink_after_remote_check_is_rejected(self):
		with tempfile.TemporaryDirectory() as root, tempfile.TemporaryDirectory() as outside:
			path = Path(root) / "contract.pdf"
			path.write_bytes(b"original")
			replacement = Path(outside) / "replacement.pdf"
			replacement.write_bytes(b"original")
			ref = frappe._dict(
				name="FILE-1",
				file_name=path.name,
				is_folder=0,
				is_private=0,
				attached_to_doctype="CRM Deal",
				attached_to_name="DEAL-1",
			)
			service = SimpleNamespace(profile=SimpleNamespace(name="Profile", prefix="prefix"))

			def swap_to_symlink(*_args):
				path.unlink()
				path.symlink_to(replacement)

			with (
				patch("erpnext_s3_integration.attachment_migration._migration_refs", return_value=[ref]),
				patch("erpnext_s3_integration.attachment_migration.frappe.db.exists", return_value=True),
				patch(
					"erpnext_s3_integration.attachment_migration.get_storage_target",
					return_value=StorageTarget.OBJECT_STORAGE,
				),
				patch("frappe.get_site_path", return_value=root),
				patch(
					"erpnext_s3_integration.attachment_migration._ensure_remote_file",
					side_effect=swap_to_symlink,
				),
				patch("erpnext_s3_integration.attachment_migration.frappe.db.set_value") as set_value,
			):
				with self.assertRaisesRegex(ValueError, "Symlinked attachment"):
					_migrate_url("/files/contract.pdf", service)
				set_value.assert_not_called()

	def test_encoded_comment_reference_prevents_local_cleanup(self):
		refs = [frappe._dict(attached_to_doctype="CRM Lead", attached_to_name="LEAD-1")]
		seen = []

		def has_reference(doctype, filters):
			seen.append((doctype, filters["content"][1]))
			return doctype == "Comment" and "%20" in filters["content"][1]

		with patch(
			"erpnext_s3_integration.attachment_migration.frappe.db.exists",
			side_effect=has_reference,
		):
			self.assertTrue(_has_related_content_reference(refs, "/files/contract copy.pdf"))
		self.assertEqual(
			seen,
			[
				("Comment", "%/files/contract copy.pdf%"),
				("Comment", "%/files/contract%20copy.pdf%"),
			],
		)

	def test_missing_local_copy_recovers_after_db_write_failure(self):
		with tempfile.TemporaryDirectory() as root:
			path = Path(root) / "attachment.pdf"
			payload = b"verified original content"
			path.write_bytes(payload)
			digest = hashlib.md5(payload, usedforsecurity=False).hexdigest()
			ref = frappe._dict(
				name="FILE-1",
				file_url="/s3/attachment.pdf",
				file_size=len(payload),
				object_storage_profile="Test Profile",
				object_storage_key=f"attachments/public/aa/bb/{digest}",
				object_storage_migrated_at=add_days(now_datetime(), -8),
				object_storage_local_cleaned_at=None,
				attached_to_doctype="CRM Deal",
				attached_to_name="DEAL-1",
			)
			backend = SimpleNamespace(
				head=lambda key: SimpleNamespace(size=len(payload)),
				get=lambda key: io.BytesIO(payload),
			)
			service = SimpleNamespace(backend=backend)
			with (
				patch("erpnext_s3_integration.attachment_migration.frappe.db.exists", return_value=False),
				patch("erpnext_s3_integration.attachment_migration.frappe.get_all", return_value=[ref]),
				patch("erpnext_s3_integration.attachment_migration._linked_attach_fields", return_value=[]),
				patch(
					"erpnext_s3_integration.attachment_migration._has_related_content_reference",
					return_value=False,
				),
				patch("erpnext_s3_integration.attachment_migration._local_path", return_value=path),
				patch("erpnext_s3_integration.attachment_migration.ObjectStorageService", return_value=service),
				patch(
					"erpnext_s3_integration.attachment_migration.frappe.db.set_value",
					side_effect=[RuntimeError("DB write failed"), None],
				) as set_value,
			):
				with self.assertRaisesRegex(RuntimeError, "DB write failed"):
					_cleanup_url("/files/attachment.pdf")
				self.assertFalse(path.exists())
				self.assertTrue(_cleanup_url("/files/attachment.pdf"))
				self.assertFalse(path.exists())
				self.assertEqual(set_value.call_count, 2)

	def test_missing_copy_without_known_original_size_is_not_marked_clean(self):
		with tempfile.TemporaryDirectory() as root:
			path = Path(root) / "missing.pdf"
			digest = hashlib.md5(b"original", usedforsecurity=False).hexdigest()
			ref = frappe._dict(
				name="FILE-1",
				file_url="/s3/attachment.pdf",
				file_size=None,
				object_storage_profile="Test Profile",
				object_storage_key=f"attachments/public/aa/bb/{digest}",
				object_storage_migrated_at=add_days(now_datetime(), -8),
				object_storage_local_cleaned_at=None,
				attached_to_doctype="CRM Lead",
				attached_to_name="LEAD-1",
			)
			with (
				patch("erpnext_s3_integration.attachment_migration.frappe.db.exists", return_value=False),
				patch("erpnext_s3_integration.attachment_migration.frappe.get_all", return_value=[ref]),
				patch("erpnext_s3_integration.attachment_migration._linked_attach_fields", return_value=[]),
				patch(
					"erpnext_s3_integration.attachment_migration._has_related_content_reference",
					return_value=False,
				),
				patch("erpnext_s3_integration.attachment_migration._local_path", return_value=path),
				patch("erpnext_s3_integration.attachment_migration.ObjectStorageService") as service,
				patch("erpnext_s3_integration.attachment_migration.frappe.db.set_value") as set_value,
			):
				self.assertFalse(_cleanup_url("/files/missing.pdf"))
				service.assert_not_called()
				set_value.assert_not_called()

	def test_missing_copy_with_mismatched_remote_content_stays_pending(self):
		with tempfile.TemporaryDirectory() as root:
			path = Path(root) / "missing.pdf"
			payload = b"verified original"
			digest = hashlib.md5(payload, usedforsecurity=False).hexdigest()
			ref = frappe._dict(
				name="FILE-1",
				file_url="/s3/attachment.pdf",
				file_size=len(payload),
				object_storage_profile="Test Profile",
				object_storage_key=f"attachments/public/aa/bb/{digest}",
				object_storage_migrated_at=add_days(now_datetime(), -8),
				object_storage_local_cleaned_at=None,
				attached_to_doctype="CRM Lead",
				attached_to_name="LEAD-1",
			)
			backend = SimpleNamespace(
				head=lambda key: SimpleNamespace(size=len(payload)),
				get=lambda key: io.BytesIO(b"x" * len(payload)),
			)
			with (
				patch("erpnext_s3_integration.attachment_migration.frappe.db.exists", return_value=False),
				patch("erpnext_s3_integration.attachment_migration.frappe.get_all", return_value=[ref]),
				patch("erpnext_s3_integration.attachment_migration._linked_attach_fields", return_value=[]),
				patch(
					"erpnext_s3_integration.attachment_migration._has_related_content_reference",
					return_value=False,
				),
				patch("erpnext_s3_integration.attachment_migration._local_path", return_value=path),
				patch(
					"erpnext_s3_integration.attachment_migration.ObjectStorageService",
					return_value=SimpleNamespace(backend=backend),
				),
				patch("erpnext_s3_integration.attachment_migration.frappe.db.set_value") as set_value,
			):
				with self.assertRaisesRegex(ValueError, "content verification failed"):
					_cleanup_url("/files/missing.pdf")
				set_value.assert_not_called()
