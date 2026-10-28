"""Focused safety tests for legacy attachment migration."""

import hashlib
import io
import tempfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import frappe
from frappe.tests import UnitTestCase

from erpnext_s3_integration.attachment_migration import (
	_cleanup_url,
	_is_local_url,
	_local_path,
	_migrate_url,
	_verify_remote,
)
from erpnext_s3_integration.object_storage.default_rules import DEFAULT_RULES
from erpnext_s3_integration.storage_policy import StorageTarget


class TestAttachmentMigration(UnitTestCase):
	def test_crm_rules_keep_active_attachments_online(self):
		policies = {rule.doctype: rule.policy for rule in DEFAULT_RULES}
		self.assertEqual(policies["CRM Lead"], "business-online")
		self.assertEqual(policies["CRM Deal"], "business-online")
		self.assertNotIn("Communication", policies)
		self.assertNotIn("Comment", policies)

	def test_local_paths_are_site_scoped(self):
		with tempfile.TemporaryDirectory() as root:
			local = Path(root) / "attachment.txt"
			local.write_bytes(b"content")
			with patch("frappe.get_site_path", return_value=root):
				self.assertEqual(_local_path("/files/attachment.txt"), local.resolve())
				for url in ("/files/../secret", "/files//etc/passwd", "/files/",
							"/private/files/../../etc/passwd"):
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

	@patch("erpnext_s3_integration.attachment_migration._migration_refs")
	@patch("erpnext_s3_integration.attachment_migration.get_storage_target")
	def test_filesystem_dependent_files_are_not_migrated(self, target, refs):
		target.return_value = StorageTarget.LOCAL_FILESYSTEM_REQUIRED
		refs.return_value = [frappe._dict(is_folder=0, is_private=1, attached_to_doctype="Bank Statement Import")]
		self.assertIsNone(_migrate_url("/private/files/import.csv", MagicMock()))

	@patch("erpnext_s3_integration.attachment_migration.frappe.db.exists", return_value=True)
	def test_cleanup_never_removes_a_still_referenced_local_url(self, exists):
		self.assertFalse(_cleanup_url("/files/still-used.pdf"))
		exists.assert_called_once_with("File", {"file_url": "/files/still-used.pdf"})
