import datetime
import io
import tempfile
from pathlib import Path
from unittest.mock import MagicMock, patch

import frappe
from botocore.exceptions import ClientError
from frappe.tests import UnitTestCase
from werkzeug.wsgi import FileWrapper

from erpnext_s3_integration.api import _authorized_file, parse_range_header
from erpnext_s3_integration.file_hooks import (
	_delete_if_unreferenced,
	generate_object_key,
	write_file_to_object_storage,
)
from erpnext_s3_integration.object_storage.classification import (
	Classification,
	choose_hottest,
	classify_file,
	get_default_retention_policy,
)
from erpnext_s3_integration.object_storage.errors import classify_storage_error
from erpnext_s3_integration.object_storage.types import ObjectInfo, ObjectStream


class TestAppAccess(UnitTestCase):
	@patch("erpnext_s3_integration.permissions.frappe.get_roles", return_value=["System Manager"])
	def test_system_manager_can_see_app_entry(self, _get_roles):
		from erpnext_s3_integration.permissions import can_open_object_storage_app

		self.assertTrue(can_open_object_storage_app())

	@patch("erpnext_s3_integration.permissions.frappe.get_roles", return_value=["Accounts User"])
	def test_regular_user_cannot_see_app_entry(self, _get_roles):
		from erpnext_s3_integration.permissions import can_open_object_storage_app

		self.assertFalse(can_open_object_storage_app())


class TestKeysAndRanges(UnitTestCase):
	def test_content_hash_key_does_not_include_filename(self):
		key = generate_object_key("a1b2c3d4e5f6", True, "erpnext/site-a")
		self.assertEqual(key, "erpnext/site-a/attachments/private/a1/b2/a1b2c3d4e5f6")

	@patch("erpnext_s3_integration.file_hooks.ObjectStorageService")
	@patch("erpnext_s3_integration.file_hooks.frappe.get_single")
	@patch("erpnext_s3_integration.file_hooks.attachment_storage_enabled", return_value=True)
	def test_upload_preserves_unicode_filename(self, _enabled, get_single, service_class):
		get_single.return_value = frappe._dict(attachment_storage_profile="Attachments")
		service = service_class.return_value
		service.profile = frappe._dict(prefix="site-a")
		file_doc = frappe._dict(
			doctype="File",
			file_name="供应商合同.pdf",
			content=b"content",
			_content=b"content",
			content_hash="040f06fd774092478d450774f5ba30c5da78acc8",
			is_private=1,
			content_type="application/pdf",
		)
		result = write_file_to_object_storage(file_doc)
		self.assertEqual(file_doc.file_name, "供应商合同.pdf")
		self.assertNotIn("供应商合同", file_doc.object_storage_key)
		self.assertEqual(result["object_storage_profile"], "Attachments")
		self.assertTrue(result["file_url"].endswith("/供应商合同.pdf"))
		service.put.assert_called_once()
		metadata = service.put.call_args.kwargs["metadata"]
		self.assertEqual(metadata, {"content-hash": file_doc.content_hash})
		self.assertNotIn("original-filename-utf8", metadata)

	def test_range_header_variants(self):
		self.assertEqual(parse_range_header("bytes=2-5", 10), (2, 5))
		self.assertEqual(parse_range_header("bytes=7-", 10), (7, 9))
		self.assertEqual(parse_range_header("bytes=-3", 10), (7, 9))
		with self.assertRaises(ValueError):
			parse_range_header("bytes=10-11", 10)
		with self.assertRaises(ValueError):
			parse_range_header("bytes=0-1,4-5", 10)

	@patch("erpnext_s3_integration.file_hooks._register_rollback_cleanup")
	@patch("erpnext_s3_integration.file_hooks.ObjectStorageService")
	@patch("erpnext_s3_integration.file_hooks.frappe.get_single")
	@patch("erpnext_s3_integration.file_hooks.attachment_storage_enabled", return_value=True)
	def test_legacy_file_manager_upload_uses_same_private_hash_key(
		self, _enabled, get_single, service_class, register_cleanup
	):
		get_single.return_value = frappe._dict(attachment_storage_profile="Attachments")
		service = service_class.return_value
		service.profile = frappe._dict(name="Attachments", prefix="site-a")
		result = write_file_to_object_storage(
			"旧接口合同.pdf", b"legacy-content", content_type="application/pdf", is_private=1
		)
		self.assertTrue(result["file_url"].startswith("/s3/site-a/attachments/private/"))
		self.assertTrue(result["file_url"].endswith("/旧接口合同.pdf"))
		self.assertNotIn("旧接口合同", result["object_storage_key"])
		service.put.assert_called_once()
		register_cleanup.assert_called_once()

	@patch("erpnext_s3_integration.api.ObjectStorageService")
	@patch("erpnext_s3_integration.api.find_file_by_url")
	@patch("erpnext_s3_integration.api.frappe.get_single")
	def test_download_response_has_206_and_unicode_filename(self, get_single, find_file, service_class):
		from erpnext_s3_integration.api import get_file

		get_single.return_value = frappe._dict(enable_range_requests=1)
		find_file.return_value = frappe._dict(
			file_name="合同.html",
			name="FILE-1",
			file_url="/s3/key/合同.html",
			object_storage_key="key",
			object_storage_profile="Attachments",
			is_private=1,
		)
		backend = service_class.return_value.backend
		backend.head.return_value = ObjectInfo("key", 6, "text/html", etag="etag")
		backend.get.return_value = ObjectStream(
			io.BytesIO(b"bcd"),
			ObjectInfo("key", 3, "text/html", etag="etag"),
			"bytes 1-3/6",
		)
		frappe.local.form_dict = frappe._dict(key="key/合同.html")
		frappe.local.request = frappe._dict(environ={}, headers={"Range": "bytes=1-3"})
		response = get_file()
		self.assertEqual(response.status_code, 206)
		self.assertEqual(response.headers["Content-Range"], "bytes 1-3/6")
		self.assertIn("%E5%90%88%E5%90%8C.html", response.headers["Content-Disposition"])
		self.assertEqual(response.headers["Content-Security-Policy"], "sandbox")
		backend.head.assert_called_once_with("key")
		backend.get.assert_called_once_with("key", (1, 3))
		response.close()

	@patch("erpnext_s3_integration.api.frappe.get_doc")
	@patch("erpnext_s3_integration.api.frappe.get_all")
	@patch("erpnext_s3_integration.api.find_file_by_url", return_value=None)
	def test_legacy_url_resolves_authorized_shared_reference(self, _find_file, get_all, get_doc):
		get_all.return_value = [{"name": "FILE-1"}]
		file_doc = frappe._dict(name="FILE-1", object_storage_key="key")
		file_doc.is_downloadable = MagicMock(return_value=True)
		get_doc.return_value = file_doc

		self.assertIs(_authorized_file("key"), file_doc)
		get_all.assert_called_once_with("File", filters={"object_storage_key": "key"}, fields="*")

	@patch("erpnext_s3_integration.api.frappe.get_doc")
	@patch("erpnext_s3_integration.api.frappe.get_all", return_value=[{"name": "FILE-1"}])
	@patch("erpnext_s3_integration.api.find_file_by_url", return_value=None)
	def test_legacy_url_rejects_unauthorized_references(self, _find_file, _get_all, get_doc):
		file_doc = frappe._dict(name="FILE-1", object_storage_key="key")
		file_doc.is_downloadable = MagicMock(return_value=False)
		get_doc.return_value = file_doc

		with self.assertRaises(frappe.PermissionError):
			_authorized_file("key")

	@patch("erpnext_s3_integration.file_hooks.ObjectStorageService")
	@patch("erpnext_s3_integration.file_hooks.frappe.db.exists", return_value=False)
	def test_last_reference_delete_uses_provider_without_version_id(self, _exists, service_class):
		_delete_if_unreferenced("Attachments", "key")
		service_class.assert_called_once_with("Attachments", require_enabled=False)
		service_class.return_value.backend.delete.assert_called_once_with("key")

	@patch("erpnext_s3_integration.file_hooks.ObjectStorageService")
	@patch("erpnext_s3_integration.file_hooks.frappe.db.exists", return_value=True)
	def test_shared_reference_prevents_provider_delete(self, _exists, service_class):
		_delete_if_unreferenced("Attachments", "key")
		service_class.assert_not_called()


class TestClassification(UnitTestCase):
	def setUp(self):
		self.file = frappe._dict(
			file_name="合同.PDF",
			attached_to_doctype="Sales Invoice",
			attached_to_field="invoice_copy",
			retention_override=None,
		)

	def test_precedence_is_manual_then_field_then_doctype_then_mime(self):
		rules = [
			_rule("MIME Type", "business-archive", mime_type="application/pdf", priority=1),
			_rule("DocType", "business-online", reference_doctype="Sales Invoice", priority=1),
			_rule(
				"DocType and Field",
				"permanent-hot",
				reference_doctype="Sales Invoice",
				reference_field="invoice_copy",
				priority=50,
			),
		]
		self.assertEqual(classify_file(self.file, rules).policy, "permanent-hot")
		self.file.retention_override = "business-archive"
		self.assertEqual(classify_file(self.file, rules).source, "Manual File Override")

	def test_unmatched_file_uses_configured_default(self):
		self.file.attached_to_doctype = "ToDo"
		self.assertEqual(classify_file(self.file, [], "unclassified").policy, "unclassified")

	def test_shared_object_uses_hottest_classification(self):
		classifications = [
			Classification("business-archive", "archive", "DocType"),
			Classification("permanent-hot", "legal", "Manual File Override"),
			Classification("business-online", "document", "MIME Type"),
		]
		self.assertEqual(choose_hottest(classifications).policy, "permanent-hot")

	def test_all_unmatched_files_keep_safe_unclassified_default(self):
		settings = frappe._dict(default_retention_policy="unclassified")
		self.assertEqual(get_default_retention_policy(settings, "Attachments"), "unclassified")
		self.assertEqual(get_default_retention_policy(settings, "Backups"), "unclassified")


def _rule(scope, policy, priority=100, **values):
	return frappe._dict(
		enabled=1, scope=scope, retention_policy=policy, category="test", priority=priority, **values
	)


class TestRamPolicy(UnitTestCase):
	def test_backup_policy_is_prefix_scoped_without_wildcard_actions(self):
		from erpnext_s3_integration.object_storage.ram_policy import build_ram_policy

		profile = frappe._dict(
			provider="Alibaba Cloud OSS",
			purpose="Backups",
			bucket="private-backups",
			prefix="erpnext/site-a",
		)
		settings = frappe._dict(backup_retention_days=365, lifecycle_reviewed=1)
		policy = build_ram_policy(profile, settings)
		actions = [action for statement in policy["Statement"] for action in statement["Action"]]
		self.assertNotIn("oss:*", actions)
		self.assertIn("oss:RestoreObject", actions)
		self.assertIn("oss:GetBucketInfo", actions)
		self.assertEqual(
			policy["Statement"][1]["Condition"]["StringLike"]["oss:Prefix"], ["erpnext/site-a/*"]
		)

	def test_attachment_policy_skips_disabled_tag_delete_and_restore(self):
		from erpnext_s3_integration.object_storage.ram_policy import build_ram_policy

		profile = frappe._dict(purpose="Attachments", bucket="attachments", prefix="site-a")
		settings = frappe._dict(
			delete_on_last_reference=0,
			enable_auto_classification=0,
			enable_archive_restore=0,
		)
		policy = build_ram_policy(profile, settings)
		actions = policy["Statement"][0]["Action"]
		bucket_actions = policy["Statement"][-1]["Action"]
		self.assertNotIn("oss:DeleteObject", actions)
		self.assertNotIn("oss:PutObjectTagging", actions)
		self.assertNotIn("oss:RestoreObject", actions)
		self.assertNotIn("oss:ListObjects", actions)
		self.assertEqual(bucket_actions, ["oss:GetBucketInfo", "oss:GetBucketLifecycle"])


class TestSetupDefaults(UnitTestCase):
	@patch("erpnext_s3_integration.setup.frappe.db.set_single_value")
	@patch("erpnext_s3_integration.setup.frappe.get_meta")
	@patch("erpnext_s3_integration.setup.frappe.db.get_singles_dict")
	def test_missing_single_settings_receive_defaults(
		self, get_singles_dict, get_meta, set_single_value
	):
		from erpnext_s3_integration.setup import _seed_missing_settings_defaults

		get_singles_dict.return_value = {"restore_days": "7"}
		get_meta.return_value.fields = [
			frappe._dict(fieldname="restore_days", default="1"),
			frappe._dict(fieldname="restore_check_interval_minutes", default="15"),
			frappe._dict(fieldname="restore_retry_limit", default="3"),
			frappe._dict(fieldname="last_backup_sync", default=None),
		]

		_seed_missing_settings_defaults()

		set_single_value.assert_called_once_with(
			"Object Storage Settings",
			{
				"restore_check_interval_minutes": "15",
				"restore_retry_limit": "3",
			},
			update_modified=False,
		)

	def test_historical_defaults_migrate_to_30_90_without_deletion(self):
		from erpnext_s3_integration.setup import _recommended_attachment_lifecycle_update

		for current in ((30, 365, 0, 0), (30, 90, 365, 0)):
			with self.subTest(current=current):
				self.assertEqual(
					_recommended_attachment_lifecycle_update(current),
					{
						"attachment_lifecycle_ia_days": 30,
						"attachment_lifecycle_archive_days": 90,
						"attachment_lifecycle_cold_archive_days": 0,
						"attachment_lifecycle_delete_days": 0,
						"attachment_lifecycle_reviewed": 0,
					},
				)

	def test_custom_180_day_lifecycle_is_not_overwritten(self):
		from erpnext_s3_integration.setup import _recommended_attachment_lifecycle_update

		self.assertIsNone(_recommended_attachment_lifecycle_update((30, 180, 0, 0)))


class TestSetupAssistantLifecycle(UnitTestCase):
	@patch("erpnext_s3_integration.object_storage.setup_assistant._profile")
	def test_attention_message_uses_configured_archive_days(self, get_profile):
		from erpnext_s3_integration.object_storage.bucket_setup import (
			bucket_verification_fingerprint,
		)
		from erpnext_s3_integration.object_storage.setup_assistant import _attachment_archive

		profile = frappe._dict(
			name="Attachments",
			provider="Alibaba Cloud OSS",
			purpose="Attachments",
			environment="Production",
			region="ap-southeast-5",
			endpoint_url="https://oss-ap-southeast-5-internal.aliyuncs.com",
			bucket="attachments",
			prefix="",
		)
		profile.last_bucket_verification_fingerprint = bucket_verification_fingerprint(profile)
		get_profile.return_value = profile
		for archive_days in (90, 180):
			with self.subTest(archive_days=archive_days):
				settings = frappe._dict(
					enable_attachment_storage=1,
					attachment_storage_profile=profile.name,
					attachment_lifecycle_reviewed=0,
					attachment_lifecycle_ia_days=30,
					attachment_lifecycle_archive_days=archive_days,
					attachment_lifecycle_cold_archive_days=0,
					attachment_lifecycle_delete_days=0,
				)
				feature = _attachment_archive(settings)
				self.assertIn(f"{archive_days}d Archive", feature["summary"])


class TestBucketSetup(UnitTestCase):
	@patch("erpnext_s3_integration.object_storage.bucket_setup.AlibabaOSSBackend")
	def test_production_bucket_requires_zrs_but_missing_lifecycle_is_only_a_warning(self, backend_class):
		import alibabacloud_oss_v2 as oss

		from erpnext_s3_integration.object_storage.bucket_setup import inspect_bucket_configuration

		backend_class.return_value.client.get_bucket_info.return_value = oss.GetBucketInfoResult(
			bucket_info=oss.BucketInfo(
				location="oss-ap-southeast-5",
				acl="private",
				block_public_access=True,
				data_redundancy_type="LRS",
			)
		)
		backend_class.return_value.client.get_bucket_lifecycle.return_value = oss.GetBucketLifecycleResult(
			lifecycle_configuration=oss.LifecycleConfiguration(rules=[])
		)
		profile = frappe._dict(
			provider="Alibaba Cloud OSS",
			purpose="Attachments",
			environment="Production",
			region="ap-southeast-5",
			endpoint_url="https://oss-ap-southeast-5-internal.aliyuncs.com",
			bucket="attachments",
			prefix="",
		)
		settings = frappe._dict(
			attachment_lifecycle_ia_days=30,
			attachment_lifecycle_archive_days=90,
			attachment_lifecycle_cold_archive_days=0,
			attachment_lifecycle_delete_days=0,
		)
		result = inspect_bucket_configuration(profile, settings)
		self.assertEqual(result["status"], "Failed")
		self.assertEqual(
			{item["code"]: item["status"] for item in result["checks"]},
			{
				"REGION": "Passed",
				"PRIVATE_BUCKET": "Passed",
				"BLOCK_PUBLIC_ACCESS": "Passed",
				"REDUNDANCY": "Failed",
				"LIFECYCLE": "Warning",
			},
		)

	def test_recommended_attachment_lifecycle_is_tag_scoped_and_never_deletes(self):
		from erpnext_s3_integration.object_storage.bucket_setup import build_bucket_setup_plan

		profile = frappe._dict(region="ap-southeast-5", bucket="attachments", prefix="")
		settings = frappe._dict(
			attachment_lifecycle_ia_days=30,
			attachment_lifecycle_archive_days=90,
			attachment_lifecycle_cold_archive_days=0,
			attachment_lifecycle_delete_days=0,
		)
		plan = build_bucket_setup_plan(profile, settings)
		self.assertEqual(plan["prefix"], "attachments/")
		self.assertEqual((plan["tag_key"], plan["tag_value"]), ("retention", "business-archive"))
		self.assertEqual(
			plan["transitions"],
			[
				{"days": 30, "storage_class": "IA"},
				{"days": 90, "storage_class": "Archive"},
			],
		)
		self.assertEqual(plan["summary"], "30d IA → 90d Archive → Never delete")
		self.assertIsNone(plan["expiration_days"])

	def test_custom_180_day_lifecycle_is_reflected_in_dynamic_summary(self):
		from erpnext_s3_integration.object_storage.bucket_setup import build_bucket_setup_plan

		profile = frappe._dict(region="ap-southeast-5", bucket="attachments", prefix="")
		settings = frappe._dict(
			attachment_lifecycle_ia_days=30,
			attachment_lifecycle_archive_days=180,
			attachment_lifecycle_cold_archive_days=0,
			attachment_lifecycle_delete_days=0,
		)
		plan = build_bucket_setup_plan(profile, settings)
		self.assertEqual(plan["summary"], "30d IA → 180d Archive → Never delete")

	def test_bucket_security_fingerprint_does_not_expire_when_lifecycle_days_change(self):
		from erpnext_s3_integration.object_storage.bucket_setup import bucket_verification_fingerprint

		profile = frappe._dict(
			provider="Alibaba Cloud OSS",
			purpose="Attachments",
			environment="Production",
			region="ap-southeast-5",
			endpoint_url="https://oss-ap-southeast-5-internal.aliyuncs.com",
			bucket="attachments",
			prefix="",
		)
		before = frappe._dict(attachment_lifecycle_ia_days=30)
		after = frappe._dict(attachment_lifecycle_ia_days=60)
		self.assertEqual(
			bucket_verification_fingerprint(profile, before),
			bucket_verification_fingerprint(profile, after),
		)

	def test_lifecycle_match_accepts_sdk_storage_class_enums(self):
		import alibabacloud_oss_v2 as oss

		from erpnext_s3_integration.object_storage.bucket_setup import _rule_matches

		rule = oss.LifecycleRule(
			prefix="attachments/",
			status="Enabled",
			tags=[oss.Tag(key="retention", value="business-archive")],
			transitions=[
				oss.LifecycleRuleTransition(days=30, storage_class=oss.StorageClassType.IA),
				oss.LifecycleRuleTransition(days=90, storage_class=oss.StorageClassType.ARCHIVE),
			],
		)
		plan = {
			"prefix": "attachments/",
			"transitions": [
				{"days": 30, "storage_class": "IA"},
				{"days": 90, "storage_class": "Archive"},
			],
			"expiration_days": None,
		}
		self.assertTrue(_rule_matches(rule, plan))


class TestStorageErrors(UnitTestCase):
	def test_boto_authorization_error(self):
		error = ClientError(
			{"Error": {"Code": "AccessDenied"}, "ResponseMetadata": {"HTTPStatusCode": 403}},
			"PutObject",
		)
		details = classify_storage_error(error)
		self.assertEqual(details.category, "Authorization")
		self.assertFalse(details.retryable)

	def test_raw_provider_error_is_normalized_at_backend_boundary(self):
		from erpnext_s3_integration.object_storage.errors import (
			ObjectPermissionError,
			normalized_storage_error,
		)

		error = ClientError(
			{"Error": {"Code": "AccessDenied"}, "ResponseMetadata": {"HTTPStatusCode": 403}},
			"GetObject",
		)
		normalized = normalized_storage_error(error, "get")
		self.assertIsInstance(normalized, ObjectPermissionError)
		self.assertEqual(normalized.code, "AccessDenied")

	def test_alibaba_operation_error_unwraps_service_error(self):
		from alibabacloud_oss_v2.exceptions import OperationError, ServiceError

		service_error = ServiceError(
			status_code=404,
			code="NoSuchKey",
			request_id="request-id",
			message="The specified key does not exist.",
			ec="0026-00000001",
			timestamp="2026-08-11T00:00:00Z",
			request_target="HEAD https://example.invalid/missing",
		)
		error = OperationError(name="HeadObject", error=service_error)

		details = classify_storage_error(error)
		self.assertEqual(details.category, "Not Found")
		self.assertEqual(details.code, "NoSuchKey")


class TestProviderAdapters(UnitTestCase):
	@patch("erpnext_s3_integration.object_storage.boto3_s3.boto3.client")
	def test_boto_put_is_private_and_uses_sse(self, boto_client):
		from erpnext_s3_integration.object_storage.boto3_s3 import Boto3S3Backend

		client = boto_client.return_value
		client.put_object.return_value = {"ETag": "etag"}
		profile = _profile(provider="AWS S3", credential_mode="Default Credential Chain")
		backend = Boto3S3Backend(profile)
		backend.put("key", io.BytesIO(b"data"), content_type="text/plain", content_length=4)
		params = client.put_object.call_args.kwargs
		self.assertNotIn("ACL", params)
		self.assertEqual(params["ServerSideEncryption"], "AES256")

	@patch("erpnext_s3_integration.object_storage.alibaba_oss.oss.Client")
	def test_alibaba_put_uses_native_sdk_model(self, oss_client):
		from erpnext_s3_integration.object_storage.alibaba_oss import AlibabaOSSBackend

		client = oss_client.return_value
		client.put_object.return_value = MagicMock(etag="etag")
		profile = _profile(provider="Alibaba Cloud OSS", credential_mode="AccessKey")
		backend = AlibabaOSSBackend(profile)
		backend.put("key", io.BytesIO(b"data"), content_type="text/plain", content_length=4)
		request = client.put_object.call_args.args[0]
		self.assertEqual(request.bucket, "private-bucket")
		self.assertEqual(request.key, "key")
		self.assertEqual(request.server_side_encryption, "AES256")

	@patch("erpnext_s3_integration.object_storage.alibaba_oss.oss.Client")
	def test_alibaba_backup_objects_force_sse_when_bucket_default_is_selected(self, oss_client):
		from erpnext_s3_integration.object_storage.alibaba_oss import AlibabaOSSBackend

		profile = _profile(
			provider="Alibaba Cloud OSS",
			purpose="Backups",
			credential_mode="AccessKey",
			server_side_encryption="Bucket Default",
		)
		backend = AlibabaOSSBackend(profile)
		self.assertEqual(backend.server_side_encryption, "AES256")

	@patch("erpnext_s3_integration.object_storage.alibaba_oss.oss.Client")
	def test_alibaba_get_adapts_no_size_reader(self, oss_client):
		from erpnext_s3_integration.object_storage.alibaba_oss import AlibabaOSSBackend

		body = NoSizeOSSBody([b"first-", b"second", b"-third"])
		oss_client.return_value.get_object.return_value = _alibaba_get_result(body, 18)
		backend = AlibabaOSSBackend(_profile(provider="Alibaba Cloud OSS", credential_mode="AccessKey"))

		stream = backend.get("key")
		self.assertEqual(stream.read(), b"first-second-third")
		self.assertEqual(stream.read(), b"")
		self.assertEqual(body.read_calls, 0)

	@patch("erpnext_s3_integration.object_storage.alibaba_oss.oss.Client")
	def test_alibaba_get_supports_sized_and_remaining_reads(self, oss_client):
		from erpnext_s3_integration.object_storage.alibaba_oss import AlibabaOSSBackend

		body = NoSizeOSSBody([b"abc", b"defgh", b"ijkl"])
		oss_client.return_value.get_object.return_value = _alibaba_get_result(body, 12)
		stream = AlibabaOSSBackend(
			_profile(provider="Alibaba Cloud OSS", credential_mode="AccessKey")
		).get("key")

		self.assertEqual(stream.read(0), b"")
		self.assertEqual(stream.read(8), b"abcdefgh")
		self.assertEqual(stream.read(), b"ijkl")
		self.assertEqual(stream.read(8), b"")
		self.assertEqual(body.read_calls, 0)

	@patch("erpnext_s3_integration.object_storage.alibaba_oss.oss.Client")
	def test_alibaba_get_streams_through_werkzeug_and_closes_once(self, oss_client):
		from erpnext_s3_integration.object_storage.alibaba_oss import AlibabaOSSBackend

		payload = b"a" * 8_192 + b"b" * 8_192 + b"tail"
		body = NoSizeOSSBody([payload[:5_000], payload[5_000:12_000], payload[12_000:]])
		oss_client.return_value.get_object.return_value = _alibaba_get_result(body, len(payload))
		stream = AlibabaOSSBackend(
			_profile(provider="Alibaba Cloud OSS", credential_mode="AccessKey")
		).get("key", (0, len(payload) - 1))

		wrapper = FileWrapper(stream, buffer_size=8_192)
		self.assertEqual(b"".join(wrapper), payload)
		wrapper.close()
		stream.close()
		self.assertEqual(body.close_calls, 1)
		self.assertEqual(body.read_calls, 0)
		request = oss_client.return_value.get_object.call_args.args[0]
		self.assertEqual(request.range_header, f"bytes=0-{len(payload) - 1}")

	@patch("erpnext_s3_integration.object_storage.alibaba_oss.oss.Client")
	def test_alibaba_stream_errors_are_normalized(self, oss_client):
		from erpnext_s3_integration.object_storage.alibaba_oss import AlibabaOSSBackend
		from erpnext_s3_integration.object_storage.errors import ObjectNetworkError

		body = FailingOSSBody()
		oss_client.return_value.get_object.return_value = _alibaba_get_result(body, 8)
		stream = AlibabaOSSBackend(
			_profile(provider="Alibaba Cloud OSS", credential_mode="AccessKey")
		).get("key")

		with self.assertRaises(ObjectNetworkError):
			stream.read()


class TestServiceCache(UnitTestCase):
	@patch("erpnext_s3_integration.object_storage.service.get_backend")
	@patch("erpnext_s3_integration.object_storage.service.frappe.get_doc")
	def test_backend_reused_by_profile_fingerprint_and_invalidated(self, get_doc, get_backend):
		from erpnext_s3_integration.object_storage.service import (
			ObjectStorageService,
			clear_backend_cache,
		)

		fingerprint = {"value": "fingerprint-1"}
		profile = frappe._dict(name="Profile", enabled=1, prefix="site-a")
		profile.config_fingerprint = lambda: fingerprint["value"]
		get_doc.return_value = profile
		get_backend.side_effect = [object(), object()]
		clear_backend_cache()

		first = ObjectStorageService("Profile").backend
		second = ObjectStorageService("Profile").backend
		self.assertIs(first, second)
		get_backend.assert_called_once()

		fingerprint["value"] = "fingerprint-2"
		third = ObjectStorageService("Profile").backend
		self.assertIsNot(first, third)
		self.assertEqual(get_backend.call_count, 2)
		clear_backend_cache()


class TestGuidedSetup(UnitTestCase):
	@patch(
		"erpnext_s3_integration.erpnext_s3_integration.doctype.object_storage_profile.object_storage_profile.frappe.only_for"
	)
	def test_jakarta_recommendations_match_environment(self, _only_for):
		from erpnext_s3_integration.erpnext_s3_integration.doctype.object_storage_profile.object_storage_profile import (
			apply_recommended_jakarta_settings,
		)

		production = apply_recommended_jakarta_settings("Production")
		development = apply_recommended_jakarta_settings("Development")
		self.assertEqual(
			production["endpoint_url"], "https://oss-ap-southeast-5-internal.aliyuncs.com"
		)
		self.assertEqual(production["credential_mode"], "ECS Instance RAM Role")
		self.assertEqual(
			development["endpoint_url"], "https://oss-ap-southeast-5.aliyuncs.com"
		)
		self.assertEqual(development["credential_mode"], "AccessKey")

	def test_review_confirmations_do_not_change_profile_fingerprint(self):
		from erpnext_s3_integration.erpnext_s3_integration.doctype.object_storage_profile.object_storage_profile import (
			ObjectStorageProfile,
		)

		profile = ObjectStorageProfile(
			{
				"doctype": "Object Storage Profile",
				"profile_name": "Fingerprint",
				"provider": "Alibaba Cloud OSS",
				"purpose": "Attachments",
				"environment": "Development",
				"credential_mode": "ECS Instance RAM Role",
				"region": "ap-southeast-5",
				"endpoint_url": "https://oss-ap-southeast-5.aliyuncs.com",
				"bucket": "attachments",
			}
		)
		settings = frappe._dict(
			delete_on_last_reference=1,
			enable_auto_classification=1,
			enable_archive_restore=0,
			retention_rules_reviewed=0,
			attachment_lifecycle_reviewed=0,
		)
		before = profile.config_fingerprint(settings)
		settings.retention_rules_reviewed = 1
		settings.attachment_lifecycle_reviewed = 1
		self.assertEqual(profile.config_fingerprint(settings), before)

	def test_failed_test_detail_is_shown_as_profile_blocker(self):
		from erpnext_s3_integration.erpnext_s3_integration.doctype.object_storage_settings.object_storage_settings import (
			_profile_blockers,
		)

		profile = frappe._dict(
			purpose="Backups",
			bucket="backups",
			provider="Alibaba Cloud OSS",
			environment="Development",
			confirm_private_bucket=1,
			confirm_block_public_access=1,
			last_test_status="Failed",
			last_test_details='[{"status":"Failed","title":"Delete object","detail":"AccessDenied: oss:DeleteObject"}]',
			enabled=0,
		)
		blockers = _profile_blockers(profile, "Backups")
		self.assertIn("AccessDenied: oss:DeleteObject", blockers[0]["message"])


def _profile(**overrides):
	values = {
		"bucket": "private-bucket",
		"region": "ap-southeast-5",
		"endpoint_url": "https://oss-ap-southeast-5-internal.aliyuncs.com",
		"use_internal_endpoint": 1,
		"addressing_style": "Auto",
		"server_side_encryption": "AES256",
		"ram_role_name": "ERPNextRole",
	}
	values.update(overrides)
	profile = frappe._dict(values)
	profile.get_password = lambda fieldname: {"access_key_id": "key-id", "access_key_secret": "secret"}[
		fieldname
	]
	return profile


def _alibaba_get_result(body, content_length):
	return MagicMock(
		body=body,
		content_length=content_length,
		content_type="application/octet-stream",
		etag="etag",
		last_modified=None,
		metadata={},
		storage_class="Standard",
		restore=None,
		content_range=None,
	)


class NoSizeOSSBody:
	def __init__(self, chunks):
		self.chunks = chunks
		self.read_calls = 0
		self.close_calls = 0

	def read(self):
		self.read_calls += 1
		return b"".join(self.chunks)

	def iter_bytes(self):
		yield from self.chunks

	def close(self):
		self.close_calls += 1


class FailingOSSBody(NoSizeOSSBody):
	def __init__(self):
		super().__init__([])

	def iter_bytes(self):
		yield b"partial"
		raise ConnectionError("stream interrupted")


class TestFullHealthCheck(UnitTestCase):
	@patch("erpnext_s3_integration.object_storage.healthcheck._set_result")
	@patch("erpnext_s3_integration.object_storage.healthcheck.ObjectStorageService")
	@patch("erpnext_s3_integration.object_storage.healthcheck.frappe.get_doc")
	@patch("erpnext_s3_integration.object_storage.healthcheck.frappe.only_for")
	def test_full_test_covers_range_tags_and_delete(self, _only_for, get_doc, service_class, set_result):
		from erpnext_s3_integration.object_storage.healthcheck import run_full_test

		profile = MagicMock(environment="Production", purpose="Attachments")
		get_doc.return_value = profile
		backend = InMemoryBackend()
		service = service_class.return_value
		service.key.return_value = "prefix/.erpnext-storage-test/test.txt"
		service.backend = backend
		result = run_full_test("Profile")
		self.assertEqual(result["status"], "Passed")
		self.assertTrue(
			any(step["title"] == "Range download for HTTP 206 support" for step in result["steps"])
		)
		self.assertFalse(backend.present)
		set_result.assert_called_once()

	def test_failed_full_test_disables_profile(self):
		from erpnext_s3_integration.object_storage.healthcheck import _set_result

		profile = MagicMock()
		profile.config_fingerprint.return_value = "fingerprint"
		_set_result(profile, "Failed", [{"status": "Failed"}], frappe._dict())
		values = profile.db_set.call_args.args[0]
		self.assertEqual(values["enabled"], 0)


class InMemoryBackend:
	def __init__(self):
		self.present = False
		self.payload = b""
		self.tags = {}

	def put(self, key, body, **kwargs):
		self.present = True
		self.payload = body.read()

	def head(self, key):
		if not self.present:
			return None
		return ObjectInfo(key, len(self.payload), "text/plain; charset=utf-8")

	def get(self, key, byte_range=None):
		payload = self.payload
		if byte_range:
			payload = payload[byte_range[0] : byte_range[1] + 1]
		return ObjectStream(io.BytesIO(payload), ObjectInfo(key, len(payload), "text/plain"))

	def put_tags(self, key, tags):
		self.tags = dict(tags)

	def get_tags(self, key):
		return self.tags

	def delete(self, key):
		self.present = False

	def exists(self, key):
		return self.present

	def list(self, prefix):
		if self.present:
			yield ObjectInfo("prefix/.erpnext-storage-test/test.txt", len(self.payload))


class TestRestoreHeaders(UnitTestCase):
	def test_restore_expiry_header_is_parsed(self):
		from erpnext_s3_integration.object_storage.restore import _expiry_from_restore_header

		value = 'ongoing-request="false", expiry-date="Sun, 09 Aug 2026 12:00:00 GMT"'
		self.assertEqual(_expiry_from_restore_header(value), datetime.datetime(2026, 8, 9, 12, 0))

	def test_restoring_header_changes_request_to_ready(self):
		from erpnext_s3_integration.object_storage.restore import _update_restoring

		doc = frappe._dict(status="Restoring", ready_at=None, expires_at=None, restore_days=1)
		info = ObjectInfo(
			"key",
			storage_class="Archive",
			restore_status='ongoing-request="false", expiry-date="Sun, 09 Aug 2026 12:00:00 GMT"',
		)
		_update_restoring(doc, info)
		self.assertEqual(doc.status, "Ready")
		self.assertEqual(doc.expires_at, datetime.datetime(2026, 8, 9, 12, 0))


class TestBackupRetention(UnitTestCase):
	def test_cleanup_keeps_latest_successful_daily_restore_points(self):
		from erpnext_s3_integration.backup_hooks import cleanup_old_backups

		service = MagicMock()
		now = datetime.datetime.now(datetime.UTC)
		service.backend.list.return_value = [
			ObjectInfo("prefix/2026-08-09/0900-database.sql.gz", last_modified=now),
			ObjectInfo("prefix/2026-08-09/0900-site_config_backup.json", last_modified=now),
			ObjectInfo("prefix/2026-08-10/0900-database.sql.gz", last_modified=now),
			ObjectInfo("prefix/2026-08-10/0900-site_config_backup.json", last_modified=now),
			ObjectInfo("prefix/2026-08-11/0800-database.sql.gz", last_modified=now),
			ObjectInfo("prefix/2026-08-11/0800-site_config_backup.json", last_modified=now),
			ObjectInfo("prefix/2026-08-11/0900-database.sql.gz", last_modified=now + datetime.timedelta(seconds=1)),
			ObjectInfo(
				"prefix/2026-08-11/0900-site_config_backup.json",
				last_modified=now + datetime.timedelta(seconds=1),
			),
		]
		self.assertEqual(
			cleanup_old_backups(service, "prefix", 2, {"database", "site-config"}), 4
		)
		self.assertEqual(
			{call.args[0] for call in service.backend.delete.call_args_list},
			{
				"prefix/2026-08-09/0900-database.sql.gz",
				"prefix/2026-08-09/0900-site_config_backup.json",
				"prefix/2026-08-11/0800-database.sql.gz",
				"prefix/2026-08-11/0800-site_config_backup.json",
			},
		)

	def test_failed_date_does_not_count_as_restore_point(self):
		from erpnext_s3_integration.backup_hooks import cleanup_old_backups

		service = MagicMock()
		now = datetime.datetime.now(datetime.UTC)
		service.backend.list.return_value = [
			ObjectInfo("prefix/2026-08-09/0900-database.sql.gz", last_modified=now),
			ObjectInfo("prefix/2026-08-09/0900-site_config_backup.json", last_modified=now),
			ObjectInfo("prefix/2026-08-09/0900-backup_complete.json", last_modified=now),
			ObjectInfo("prefix/2026-08-10/0900-database.sql.gz", last_modified=now),
			ObjectInfo("prefix/2026-08-11/0900-database.sql.gz", last_modified=now),
			ObjectInfo("prefix/2026-08-11/0900-site_config_backup.json", last_modified=now),
			ObjectInfo("prefix/2026-08-11/0900-backup_complete.json", last_modified=now),
		]
		self.assertEqual(
			cleanup_old_backups(service, "prefix", 2, {"database", "site-config"}), 1
		)
		service.backend.delete.assert_called_once_with(
			"prefix/2026-08-10/0900-database.sql.gz"
		)

	def test_database_backup_includes_matching_site_configuration(self):
		from erpnext_s3_integration.backup_hooks import _paths_from_result

		settings = frappe._dict(
			upload_database_backup=1,
			upload_public_files_backup=0,
			upload_private_files_backup=0,
		)
		paths = _paths_from_result(
			{
				"backup_path_db": "/tmp/20260811_020000-site-database.sql.gz",
				"backup_path_conf": "/tmp/20260811_020000-site-site_config_backup.json",
			},
			settings,
		)
		self.assertEqual(
			paths,
			[
				"/tmp/20260811_020000-site-database.sql.gz",
				"/tmp/20260811_020000-site-site_config_backup.json",
			],
		)

	@patch("frappe.utils.backups.BackupGenerator")
	def test_backup_generator_returns_site_configuration_without_temp_cleanup(self, generator_class):
		from erpnext_s3_integration.backup_hooks import _create_backup

		generator = generator_class.return_value
		generator.backup_path_db = "/tmp/run-database.sql.gz"
		generator.backup_path_conf = "/tmp/run-site_config_backup.json"
		generator.backup_path_files = "/tmp/run-files.tar"
		generator.backup_path_private_files = "/tmp/run-private-files.tar"
		result = _create_backup(include_files=False)
		generator.get_backup.assert_called_once_with(older_than=6, ignore_files=True, force=True)
		self.assertEqual(result["backup_path_conf"], generator.backup_path_conf)

	@patch("frappe.desk.page.backups.backups.delete_downloadable_backups")
	def test_local_cleanup_uses_frappe_backup_limit_logic(self, delete_downloadable_backups):
		from erpnext_s3_integration.backup_hooks import _cleanup_local_backups

		self.assertTrue(_cleanup_local_backups())
		delete_downloadable_backups.assert_called_once_with()

	def test_frappe_local_cleanup_deletes_only_the_oldest_complete_group(self):
		from frappe.desk.page.backups import backups

		site_slug = frappe.local.site.replace(".", "_")
		with tempfile.TemporaryDirectory() as backup_dir:
			for day in range(1, 5):
				prefix = f"202608{day:02d}_020000-{site_slug}"
				Path(backup_dir, f"{prefix}-database.sql.gz").touch()
				Path(backup_dir, f"{prefix}-site_config_backup.json").touch()
			with (
				patch.object(backups, "get_site_path", return_value=backup_dir),
				patch.object(backups.frappe, "get_system_settings", return_value=3),
			):
				backups.delete_downloadable_backups()
			remaining = {path.name for path in Path(backup_dir).iterdir()}
			self.assertEqual(len(remaining), 6)
			self.assertFalse(any(name.startswith("20260801_020000") for name in remaining))
			for day in range(2, 5):
				self.assertEqual(
					len([name for name in remaining if name.startswith(f"202608{day:02d}_020000")]),
					2,
				)

	@patch("erpnext_s3_integration.backup_hooks.log_storage_sync")
	@patch("erpnext_s3_integration.backup_hooks._cleanup_remote_backups")
	@patch("erpnext_s3_integration.backup_hooks._upload_backup", side_effect=[True, False])
	@patch("erpnext_s3_integration.backup_hooks.ObjectStorageService")
	@patch("erpnext_s3_integration.backup_hooks.frappe.get_single")
	def test_upload_failure_skips_oss_cleanup(
		self, get_single, _service_class, _upload, cleanup, _log
	):
		from erpnext_s3_integration.backup_hooks import sync_backup_files

		get_single.return_value = self._backup_settings()
		self.assertFalse(sync_backup_files(self._complete_backup_result()))
		cleanup.assert_not_called()

	@patch("erpnext_s3_integration.backup_hooks.log_storage_sync")
	@patch("erpnext_s3_integration.backup_hooks._cleanup_remote_backups", return_value=False)
	@patch("erpnext_s3_integration.backup_hooks._upload_completion_marker", return_value=True)
	@patch("erpnext_s3_integration.backup_hooks._upload_backup", return_value=True)
	@patch("erpnext_s3_integration.backup_hooks.ObjectStorageService")
	@patch("erpnext_s3_integration.backup_hooks.frappe.get_single")
	def test_oss_cleanup_failure_does_not_fail_new_restore_point(
		self, get_single, _service_class, _upload, _marker, cleanup, _log
	):
		from erpnext_s3_integration.backup_hooks import sync_backup_files

		get_single.return_value = self._backup_settings()
		self.assertTrue(sync_backup_files(self._complete_backup_result()))
		cleanup.assert_called_once()

	@staticmethod
	def _backup_settings():
		return frappe._dict(
			enable_backup_storage=1,
			backup_storage_profile="Backups",
			backup_retention_days=30,
			upload_database_backup=1,
			upload_public_files_backup=0,
			upload_private_files_backup=0,
		)

	@staticmethod
	def _complete_backup_result():
		return {
			"backup_path_db": "/tmp/20260811_020000-site-database.sql.gz",
			"backup_path_conf": "/tmp/20260811_020000-site-site_config_backup.json",
		}

	def test_private_folder_archive_is_separate_from_object_backed_attachments(self):
		from erpnext_s3_integration.backup_hooks import _paths_from_result

		settings = frappe._dict(
			upload_database_backup=0,
			upload_public_files_backup=0,
			upload_private_files_backup=1,
		)
		paths = _paths_from_result(
			{
				"backup_path_db": "/tmp/20260811-database.sql.gz",
				"backup_path_files": "/tmp/20260811-files.tar",
				"backup_path_private_files": "/tmp/20260811-private-files.tar",
			},
			settings,
		)
		self.assertEqual(paths, ["/tmp/20260811-private-files.tar"])

	@patch("erpnext_s3_integration.backup_hooks._local_backup_space_is_low", return_value=False)
	def test_backup_capacity_thresholds(self, _space_is_low):
		from erpnext_s3_integration.backup_hooks import _backup_health

		now = frappe.utils.now_datetime()
		self.assertEqual(_backup_health(now, 10 * 1024**3, 0)[0], "Warning")
		self.assertEqual(
			_backup_health(now - datetime.timedelta(hours=37), 1024, 0)[0], "Critical"
		)
