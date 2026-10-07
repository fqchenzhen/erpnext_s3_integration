import inspect
import io
from unittest.mock import Mock, patch

import frappe
from frappe.core.doctype.file.file import File
from frappe.tests import UnitTestCase
from frappe.utils.backups import BackupGenerator

from erpnext_s3_integration import backup_hooks
from erpnext_s3_integration.overrides.s3_file import S3FileMixin


class TestFileCompatibility(UnitTestCase):
	def test_override_signatures_accept_current_file_parameters(self):
		for name, override in vars(S3FileMixin).items():
			if name.startswith("__") or isinstance(override, property):
				continue
			with self.subTest(method=name):
				upstream = inspect.signature(getattr(File, name))
				arguments = {key: None for key in upstream.parameters if key != "self"}
				inspect.signature(override).bind(None, **arguments)

	def test_folder_contents_are_rejected_for_local_and_object_urls(self):
		for url in ("/files/folder", "/s3/private/key/folder"):
			file = RemoteFile(url)
			file.is_folder = 1
			with (
				patch("erpnext_s3_integration.overrides.s3_file.ObjectStorageService") as service,
				self.assertRaises(frappe.ValidationError),
			):
				file.get_content()
			service.assert_not_called()

	def test_new_file_cannot_read_arbitrary_object_url(self):
		file = RemoteFile()
		file.is_new = lambda: True
		with (
			patch("erpnext_s3_integration.overrides.s3_file.ObjectStorageService") as service,
			self.assertRaises(frappe.PermissionError),
		):
			file.get_content()
		service.assert_not_called()

	def test_saved_object_content_is_read_and_stream_closed(self):
		stream = io.BytesIO(b"hello")
		with patch("erpnext_s3_integration.overrides.s3_file.ObjectStorageService") as service:
			service.return_value.backend.get.return_value = stream
			self.assertEqual(RemoteFile().get_content(encodings=()), b"hello")
		self.assertTrue(stream.closed)

	def test_private_public_visibility_change_is_rejected(self):
		with self.assertRaises(frappe.ValidationError):
			RemoteFile().handle_is_private_changed()

	def test_shared_object_deletion_preserves_content(self):
		for shared in (True, False):
			file = RemoteFile()
			file.delete_file_data_content = Mock()
			with patch.object(frappe.db, "exists", return_value=shared):
				file._delete_file_on_disk()
			file.delete_file_data_content.assert_called_once_with(only_thumbnail=shared)


class TestBackupCompatibility(UnitTestCase):
	def test_backup_generator_signature_and_output_fields(self):
		inspect.signature(BackupGenerator).bind("db", "user", "password", db_host="db")
		inspect.signature(BackupGenerator.get_backup).bind(None, older_than=6, ignore_files=True, force=True)
		with patch("frappe.utils.backups.BackupGenerator") as generator:
			for field in backup_hooks.BACKUP_RESULT_FIELDS:
				setattr(generator.return_value, field, f"/tmp/{field}")
			result = backup_hooks._create_backup(include_files=True)
		self.assertEqual(
			tuple(result),
			("backup_path_db", "backup_path_conf", "backup_path_files", "backup_path_private_files"),
		)

	def test_encrypted_filenames_share_the_same_backup_run(self):
		for suffix in (
			"database-enc.sql.gz",
			"site_config_backup-enc.json",
			"files-enc.tar",
			"private-files-enc.tar",
			"backup_complete.json",
		):
			self.assertEqual(
				backup_hooks._backup_run_id(f"20261007_010000-site-{suffix}"), "20261007_010000-site"
			)

	def test_incomplete_group_is_not_uploaded_or_marked_complete(self):
		settings = frappe._dict(
			enable_backup_storage=1,
			upload_database_backup=1,
			upload_public_files_backup=0,
			upload_private_files_backup=0,
		)
		with (
			patch.object(frappe, "get_single", return_value=settings),
			patch.object(backup_hooks, "_today_candidates", return_value=[]),
			patch.object(backup_hooks, "log_storage_sync"),
			patch.object(backup_hooks, "_upload_backup") as upload,
			patch.object(backup_hooks, "_upload_completion_marker") as marker,
		):
			self.assertFalse(
				backup_hooks.sync_backup_files(
					{"backup_path_db": "/tmp/20261007_010000-site-database.sql.gz"}
				)
			)
			upload.assert_not_called()
			marker.assert_not_called()


class RemoteFile(S3FileMixin):
	is_folder = 0
	object_storage_profile = "Profile"
	object_storage_key = "private/key"
	name = "FILE"

	def __init__(self, url="/s3/private/key/file"):
		self.file_url = url
		self.flags = frappe._dict()

	def get(self, key):
		return getattr(self, key, None)

	def is_new(self):
		return False
