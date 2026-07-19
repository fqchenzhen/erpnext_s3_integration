import datetime
import io
from unittest.mock import MagicMock, patch
from urllib.parse import parse_qs, urlsplit

import frappe
from botocore.exceptions import ClientError
from frappe.tests.utils import FrappeTestCase

from erpnext_s3_integration import api
from erpnext_s3_integration.backup_hooks import _backup_date, cleanup_old_backups
from erpnext_s3_integration.file_hooks import generate_s3_key
from erpnext_s3_integration.s3_client import S3Client, classify_s3_exception


class TestS3Integration(FrappeTestCase):
	def setUp(self):
		self.settings = frappe.get_doc("S3 Integration Settings", "S3 Integration Settings")
		self.settings.aws_access_key_id = "test_key"
		self.settings.aws_secret_access_key = "test_secret"
		self.settings.region_name = "us-east-1"
		self.settings.bucket_name = "test-bucket"
		self.settings.folder_prefix = "test-prefix"
		self.settings.provider = "AWS S3"
		self.settings.addressing_style = "auto"
		self.settings.endpoint_url = ""
		self.settings.use_path_style = 0
		self.settings.enable_attachments_s3 = 1
		self.settings.enable_backups_s3 = 0
		self.settings.stream_from_s3 = 0
		self.settings.delete_from_s3_on_file_delete = 1

		# Save to DB so get_single works natively during tests
		self.settings.flags.ignore_mandatory = True
		self.settings.save(ignore_permissions=True)

		# For tests we won't actually encrypt to DB to avoid complexities
		# We'll mock get_password
		patcher = patch(
			"erpnext_s3_integration.s3_client.S3Client.get_password",
			return_value="test_secret",
		)
		self.mock_get_password = patcher.start()
		self.addCleanup(patcher.stop)

	@patch("erpnext_s3_integration.backup_hooks.frappe.utils.get_system_timezone", return_value="Asia/Jakarta")
	@patch("erpnext_s3_integration.backup_hooks.os.path.getmtime")
	def test_backup_date_uses_frappe_system_timezone(self, mock_getmtime, _mock_get_system_timezone):
		mock_getmtime.return_value = datetime.datetime(
			2026,
			7,
			18,
			19,
			0,
			3,
			tzinfo=datetime.UTC,
		).timestamp()

		self.assertEqual(_backup_date("backup.sql.gz"), "2026-07-19")

	@patch("boto3.client")
	def test_s3_client_init(self, mock_boto_client):
		S3Client()
		mock_boto_client.assert_called_once()
		kwargs = mock_boto_client.call_args.kwargs
		self.assertEqual(kwargs["config"].signature_version, "s3v4")

		# Test path style config
		self.settings.use_path_style = 1
		self.settings.endpoint_url = "http://localhost:9000"
		self.settings.save(ignore_permissions=True)
		S3Client()

		kwargs = mock_boto_client.call_args[1]
		self.assertEqual(kwargs["endpoint_url"], "http://localhost:9000")
		self.assertTrue(kwargs["config"].s3["addressing_style"] == "path")

		self.settings.use_path_style = 0
		self.settings.addressing_style = "virtual"
		self.settings.save(ignore_permissions=True)
		S3Client()
		kwargs = mock_boto_client.call_args[1]
		self.assertTrue(kwargs["config"].s3["addressing_style"] == "virtual")

		self.settings.provider = "Alibaba Cloud OSS"
		self.settings.region_name = "ap-southeast-5"
		self.settings.endpoint_url = "https://s3.oss-ap-southeast-5.aliyuncs.com"
		self.settings.addressing_style = "auto"
		self.settings.save(ignore_permissions=True)
		S3Client()
		kwargs = mock_boto_client.call_args.kwargs
		self.assertEqual(kwargs["config"].signature_version, "s3")
		self.assertEqual(kwargs["config"].s3["addressing_style"], "virtual")

	def test_alibaba_presigned_url_uses_virtual_host_and_s3_v2(self):
		self.settings.provider = "Alibaba Cloud OSS"
		self.settings.region_name = "ap-southeast-5"
		self.settings.endpoint_url = "https://s3.oss-ap-southeast-5.aliyuncs.com"
		self.settings.addressing_style = "auto"
		self.settings.save(ignore_permissions=True)

		url = S3Client().generate_presigned_url("attachments/private/test.txt")
		parsed_url = urlsplit(url)
		query = parse_qs(parsed_url.query)

		self.assertEqual(
			parsed_url.hostname,
			"test-bucket.s3.oss-ap-southeast-5.aliyuncs.com",
		)
		self.assertNotIn("test-bucket", parsed_url.path)
		self.assertIn("AWSAccessKeyId", query)

	@patch("frappe.utils.redis_wrapper.RedisWrapper.lpush")
	@patch("erpnext_s3_integration.s3_client.S3Client.upload_fileobj")
	def test_file_upload_hook(self, mock_upload, mock_lpush):
		# We use frappe.get_doc but ensure content is handled like an upload
		file_doc = frappe.get_doc(
			{
				"doctype": "File",
				"file_name": "test_s3_upload.txt",
				"content": b"test content",  # Bytes
				"is_private": 1,
			}
		)

		# Bypass frappe's local path validation for S3 urls in tests
		file_doc.validate_file_path = lambda: None
		file_doc.validate_file_url = lambda: None
		file_doc.validate_file_on_disk = lambda: None

		file_doc.insert()

		# Check if upload was called
		self.assertTrue(mock_upload.called)

		# Check if file URL was updated appropriately
		self.assertTrue(file_doc.file_url.startswith("/s3/test-prefix/attachments/private/"))

		# Assert content is cleared so it isn't saved to disk
		self.assertIsNone(file_doc.content)

	@patch("frappe.utils.redis_wrapper.RedisWrapper.lpush")
	@patch("erpnext_s3_integration.s3_client.S3Client.delete_object")
	@patch("erpnext_s3_integration.s3_client.S3Client.upload_fileobj")
	def test_file_delete_hook(
		self,
		mock_upload,
		mock_delete,
		mock_lpush,
	):
		file_doc = frappe.get_doc(
			{
				"doctype": "File",
				"file_name": "test_s3_delete.txt",
				"content": b"test content",
				"is_private": 1,
			}
		)

		file_doc.validate_file_path = lambda: None
		file_doc.validate_file_url = lambda: None
		file_doc.validate_file_on_disk = lambda: None

		file_doc.insert()

		# Now delete it
		file_doc.delete()

		# Verify S3 delete was called
		self.assertTrue(mock_delete.called)

	def test_generate_s3_key(self):
		file_doc = frappe.get_doc(
			{
				"doctype": "File",
				"file_name": "My test file 123.txt",
				"attached_to_doctype": "Sales Invoice",
				"content_hash": "a1b2c3d4e5f6",
				"is_private": 0,
			}
		)

		key = generate_s3_key(file_doc, self.settings)
		self.assertTrue(key.startswith("test-prefix/attachments/public/a1/b2/"))
		self.assertTrue(key.endswith("a1b2c3d4e5f6-My_test_file_123.txt"))

	def test_new_file_cannot_reuse_supplied_s3_key(self):
		file_doc = frappe.get_doc(
			{
				"doctype": "File",
				"file_name": "replacement.txt",
				"file_url": "/s3/protected/existing-key",
				"content_hash": "a1b2c3d4e5f6",
				"is_private": 0,
			}
		)

		key = generate_s3_key(file_doc, self.settings)
		self.assertNotEqual(key, "protected/existing-key")
		self.assertEqual(
			generate_s3_key(file_doc, self.settings, preserve_existing_s3_url=True),
			"protected/existing-key",
		)

	def test_new_file_cannot_read_arbitrary_s3_url(self):
		file_doc = frappe.get_doc(
			{
				"doctype": "File",
				"file_name": "existing-key.txt",
				"file_url": "/s3/backups/site/secret.sql.gz",
				"is_private": 0,
			}
		)

		with self.assertRaises(frappe.PermissionError):
			file_doc.insert(ignore_permissions=True)

	def test_uploads_use_private_object_acl(self):
		s3_client = object.__new__(S3Client)
		s3_client.bucket_name = "test-bucket"
		s3_client._client = MagicMock()

		s3_client.upload_fileobj(io.BytesIO(b"content"), "test.txt", "text/plain", is_public=True)

		extra_args = s3_client._client.upload_fileobj.call_args.kwargs["ExtraArgs"]
		self.assertEqual(extra_args, {"ACL": "private", "ContentType": "text/plain"})

	def test_upload_retries_without_acl_when_provider_disables_acls(self):
		s3_client = object.__new__(S3Client)
		s3_client.bucket_name = "test-bucket"
		s3_client._client = MagicMock()
		s3_client._client.upload_fileobj.side_effect = [
			ClientError(
				{"Error": {"Code": "AccessControlListNotSupported"}},
				"PutObject",
			),
			None,
		]

		s3_client.upload_fileobj(io.BytesIO(b"content"), "test.txt", "text/plain")

		second_call = s3_client._client.upload_fileobj.call_args_list[1]
		self.assertEqual(second_call.kwargs["ExtraArgs"], {"ContentType": "text/plain"})

	def test_alibaba_error_code_is_classified_as_configuration(self):
		error = ClientError(
			{
				"Error": {"Code": "InvalidArgument"},
				"ResponseMetadata": {
					"HTTPStatusCode": 400,
					"HTTPHeaders": {"x-oss-ec": "0017-00000804"},
				},
			},
			"PutObject",
		)

		details = classify_s3_exception(error)

		self.assertEqual(details["category"], "provider_configuration")
		self.assertEqual(details["error_code"], "0017-00000804")

	@patch("erpnext_s3_integration.s3_client.S3Client.generate_presigned_url")
	def test_existing_s3_file_access_still_works_when_uploads_disabled(self, mock_generate_presigned_url):
		mock_generate_presigned_url.return_value = "https://example.com/test-file"

		self.settings.enable_attachments_s3 = 0
		self.settings.stream_from_s3 = 0
		self.settings.save(ignore_permissions=True)

		file_doc = frappe.get_doc(
			{
				"doctype": "File",
				"file_name": "existing_on_s3.txt",
				"file_url": "/s3/test-prefix/existing_on_s3.txt",
				"is_private": 0,
			}
		)
		file_doc.flags.copy_from_existing_file = True
		file_doc.insert(ignore_permissions=True)

		self.addCleanup(lambda: frappe.db.delete("File", {"name": file_doc.name}))

		frappe.local.form_dict = frappe._dict({"key": "test-prefix/existing_on_s3.txt"})
		frappe.local.response = frappe._dict()

		api.get_file()

		self.assertEqual(frappe.local.response["type"], "redirect")
		self.assertEqual(frappe.local.response["location"], "https://example.com/test-file")

	@patch("erpnext_s3_integration.s3_client.S3Client.download_as_stream")
	def test_stream_forces_unsafe_content_to_download(self, mock_download_as_stream):
		mock_download_as_stream.return_value = io.BytesIO(b"<script>alert(1)</script>")
		self.settings.enable_attachments_s3 = 0
		self.settings.stream_from_s3 = 1
		self.settings.delete_from_s3_on_file_delete = 0
		self.settings.save(ignore_permissions=True)

		file_doc = frappe.get_doc(
			{
				"doctype": "File",
				"file_name": "unsafe.html",
				"file_url": "/s3/test-prefix/unsafe.html",
				"is_private": 0,
			}
		)
		file_doc.flags.copy_from_existing_file = True
		file_doc.insert(ignore_permissions=True)
		self.addCleanup(lambda: frappe.db.delete("File", {"name": file_doc.name}))

		frappe.local.form_dict = frappe._dict({"key": "test-prefix/unsafe.html"})
		frappe.local.response = frappe._dict()
		frappe.local.request = frappe._dict(environ={})

		response = api.get_file()

		self.assertIn("attachment", response.headers["Content-Disposition"])
		self.assertEqual(response.headers["X-Content-Type-Options"], "nosniff")
		self.assertEqual(response.headers["Content-Security-Policy"], "sandbox")
		response.close()

	@patch("erpnext_s3_integration.backup_hooks.log_s3_sync")
	def test_cleanup_old_backups_uses_public_client(self, mock_log_s3_sync):
		s3_client = MagicMock()
		s3_client.bucket_name = "test-bucket"
		paginator = MagicMock()
		paginator.paginate.return_value = [
			{
				"Contents": [
					{
						"Key": "backups/site/old-file.sql.gz",
						"LastModified": frappe.utils.add_days(frappe.utils.now_datetime(), -10),
					}
				]
			}
		]
		s3_client.client.get_paginator.return_value = paginator

		cleanup_old_backups(s3_client, "backups/site/", 7)

		s3_client.client.get_paginator.assert_called_once_with("list_objects_v2")
		s3_client.delete_object.assert_called_once_with(
			"backups/site/old-file.sql.gz",
			raise_on_error=True,
		)
		self.assertTrue(mock_log_s3_sync.called)
