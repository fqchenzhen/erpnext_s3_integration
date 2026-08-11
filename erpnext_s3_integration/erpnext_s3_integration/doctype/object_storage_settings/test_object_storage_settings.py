from unittest.mock import patch

import frappe
from frappe.tests import IntegrationTestCase


class IntegrationTestObjectStorageSettings(IntegrationTestCase):
	SETTINGS_STATE_FIELDS = (
		"attachment_storage_profile",
		"enable_attachment_storage",
		"backup_storage_profile",
		"enable_backup_storage",
		"backup_retention_days",
		"ram_policy_reviewed",
		"proxy_downloads",
	)

	def test_system_manager_navigation_is_installed(self):
		from frappe.boot import get_sidebar_items

		sidebar = frappe.get_doc("Workspace Sidebar", "ERPNext S3 Integration")
		self.assertEqual(sidebar.app, "erpnext_s3_integration")
		self.assertEqual(sidebar.items[0].label, "Object Storage Settings")
		self.assertEqual(sidebar.items[0].link_type, "DocType")
		self.assertEqual(sidebar.items[0].link_to, "Object Storage Settings")
		self.assertIn("erpnext s3 integration", get_sidebar_items([]))

		desktop_icon = frappe.get_doc("Desktop Icon", "ERPNext S3 Integration")
		self.assertEqual(desktop_icon.link_type, "Workspace Sidebar")
		self.assertEqual(desktop_icon.link_to, sidebar.name)
		self.assertEqual([row.role for row in desktop_icon.roles], ["System Manager"])

	def test_object_storage_navigation_is_hidden_from_guest(self):
		from frappe.boot import get_sidebar_items

		previous_user = frappe.session.user
		try:
			frappe.set_user("Guest")
			self.assertNotIn("erpnext s3 integration", get_sidebar_items([]))
		finally:
			frappe.set_user(previous_user)

	def test_required_doctypes_and_file_fields_exist(self):
		for doctype in (
			"Object Storage Settings",
			"Object Storage Profile",
			"Object Storage Retention Rule",
			"Object Storage Restore Role",
			"Object Restore Request",
		):
			self.assertTrue(frappe.db.exists("DocType", doctype))
		file_meta = frappe.get_meta("File")
		self.assertTrue(file_meta.has_field("object_storage_profile"))
		self.assertTrue(file_meta.has_field("retention_override"))

	def test_assistant_has_two_core_and_two_optional_features(self):
		from erpnext_s3_integration.object_storage.setup_assistant import build_setup_assistant

		assistant = build_setup_assistant(frappe.get_single("Object Storage Settings"))
		self.assertEqual(
			[item["key"] for item in assistant["features"]],
			["attachments", "backups", "attachment_archive", "archive_restore"],
		)
		self.assertIn(assistant["features"][2]["status"], {"Available", "Needs Attention", "Not Configured", "Optional"})
		self.assertIn(assistant["features"][3]["status"], {"Enabled", "Optional"})

	def test_settings_layout_has_four_tabs_and_no_bucket_field(self):
		meta = frappe.get_meta("Object Storage Settings")
		tabs = [field.label for field in meta.fields if field.fieldtype == "Tab Break"]
		self.assertEqual(tabs, ["Setup Assistant", "Attachments", "Backups", "Archive Restore"])
		self.assertFalse(meta.has_field("attachment_bucket"))
		self.assertFalse(meta.has_field("backup_bucket"))
		self.assertEqual(meta.get_field("backup_retention_days").default, "30")
		self.assertEqual(meta.get_field("upload_database_backup").default, "1")

	def test_classification_and_lifecycle_are_sections_inside_attachments(self):
		meta = frappe.get_meta("Object Storage Settings")
		self.assertEqual(meta.get_field("classification_tab").fieldtype, "Section Break")
		self.assertEqual(meta.get_field("attachment_lifecycle_tab").fieldtype, "Section Break")

	def test_lifecycle_days_must_be_strictly_increasing(self):
		settings = frappe.get_single("Object Storage Settings")
		settings.enable_attachment_storage = 0
		settings.enable_backup_storage = 0
		settings.lifecycle_reviewed = 1
		settings.lifecycle_ia_days = 30
		settings.lifecycle_archive_days = 7
		with self.assertRaises(frappe.ValidationError):
			settings.validate()

	def test_attachment_lifecycle_delete_is_disabled_by_default(self):
		settings = frappe.get_single("Object Storage Settings")
		settings.enable_attachment_storage = 0
		settings.enable_backup_storage = 0
		settings.attachment_lifecycle_ia_days = 30
		settings.attachment_lifecycle_archive_days = 90
		settings.attachment_lifecycle_cold_archive_days = 365
		settings.attachment_lifecycle_delete_days = 0
		settings.validate()

	def test_unreviewed_attachment_lifecycle_does_not_block_regular_attachments(self):
		settings = frappe.get_single("Object Storage Settings")
		settings.enable_attachment_storage = 0
		settings.attachment_lifecycle_reviewed = 0
		settings.attachment_lifecycle_ia_days = 0
		settings.attachment_lifecycle_archive_days = 0
		settings.attachment_lifecycle_cold_archive_days = 0
		settings.validate()

	def test_attachment_lifecycle_delete_must_follow_cold_archive(self):
		settings = frappe.get_single("Object Storage Settings")
		settings.enable_attachment_storage = 0
		settings.attachment_lifecycle_reviewed = 1
		settings.attachment_lifecycle_delete_days = 365
		with self.assertRaises(frappe.ValidationError):
			settings.validate()

	def test_unknown_attachments_are_unclassified_and_old_unsafe_field_is_removed(self):
		settings = frappe.get_single("Object Storage Settings")
		self.assertEqual(settings.default_retention_policy, "unclassified")
		self.assertFalse(settings.meta.has_field("attachment_default_retention_policy"))

	def test_proxy_download_security_invariant_is_forced_on(self):
		settings = frappe.get_single("Object Storage Settings")
		settings.enable_attachment_storage = 0
		settings.enable_backup_storage = 0
		settings.proxy_downloads = 0
		settings.validate()
		self.assertTrue(settings.proxy_downloads)

	def test_saved_unlinked_profile_is_offered_by_assistant(self):
		profile = frappe.get_doc(
			{
				"doctype": "Object Storage Profile",
				"profile_name": f"Guide Profile {frappe.generate_hash(length=8)}",
				"provider": "Alibaba Cloud OSS",
				"purpose": "Attachments",
				"environment": "Development",
				"credential_mode": "ECS Instance RAM Role",
				"region": "ap-southeast-5",
				"use_internal_endpoint": 1,
				"bucket": "guide-test-bucket",
			}
		).insert()
		from erpnext_s3_integration.object_storage.setup_assistant import build_setup_assistant

		settings = frappe._dict(frappe.get_single("Object Storage Settings").as_dict())
		settings.attachment_storage_profile = None
		assistant = build_setup_assistant(settings)
		feature = next(item for item in assistant["features"] if item["key"] == "attachments")
		self.assertIn(feature["status"], {"Needs Attention", "Check Failed"})
		self.assertEqual(feature["profile"], profile.name)

	@patch("erpnext_s3_integration.object_storage.healthcheck.run_full_test")
	@patch("erpnext_s3_integration.object_storage.bucket_setup.inspect_bucket_configuration")
	def test_one_click_flow_tests_links_and_enables_attachment_profile(
		self, inspect_bucket, run_full_test
	):
		from frappe.utils import now_datetime

		from erpnext_s3_integration.erpnext_s3_integration.doctype.object_storage_settings.object_storage_settings import (
			verify_and_enable_profile,
		)

		self._restore_settings_after_test()
		frappe.db.set_single_value("Object Storage Settings", "enable_attachment_storage", 0)
		frappe.db.set_single_value("Object Storage Settings", "enable_backup_storage", 0)
		profile = self._new_guided_profile("Attachments")

		def pass_test(profile_name):
			tested_profile = frappe.get_doc("Object Storage Profile", profile_name)
			tested_profile.db_set(
				{
					"last_test_status": "Passed",
					"last_tested_at": now_datetime(),
					"last_test_fingerprint": tested_profile.config_fingerprint(),
					"last_test_details": "[]",
				}
			)
			return {"status": "Passed", "steps": [], "message": "Passed"}

		run_full_test.side_effect = pass_test
		inspect_bucket.return_value = {
			"status": "Warning",
			"checks": [{"code": "LIFECYCLE", "status": "Warning", "summary": "Not configured"}],
		}
		result = verify_and_enable_profile("Attachments", profile.name)
		settings = frappe.get_single("Object Storage Settings")
		profile.reload()
		self.assertTrue(result["enabled"])
		self.assertTrue(settings.enable_attachment_storage)
		self.assertEqual(settings.attachment_storage_profile, profile.name)
		self.assertTrue(profile.enabled)

	@patch("erpnext_s3_integration.object_storage.healthcheck.run_full_test")
	@patch("erpnext_s3_integration.object_storage.bucket_setup.inspect_bucket_configuration")
	def test_one_click_failure_keeps_backup_disabled(self, inspect_bucket, run_full_test):
		from erpnext_s3_integration.erpnext_s3_integration.doctype.object_storage_settings.object_storage_settings import (
			verify_and_enable_profile,
		)

		self._restore_settings_after_test()
		frappe.db.set_single_value("Object Storage Settings", "enable_attachment_storage", 0)
		frappe.db.set_single_value("Object Storage Settings", "enable_backup_storage", 0)
		profile = self._new_guided_profile("Backups")
		inspect_bucket.return_value = {"status": "Passed", "checks": []}
		run_full_test.return_value = {
			"status": "Failed",
			"steps": [{"status": "Failed", "title": "Delete object", "detail": "AccessDenied"}],
			"message": "Check RAM permission",
		}

		result = verify_and_enable_profile("Backups", profile.name)
		settings = frappe.get_single("Object Storage Settings")
		profile.reload()
		self.assertFalse(result["enabled"])
		self.assertFalse(settings.enable_backup_storage)
		self.assertFalse(profile.enabled)

	@patch("erpnext_s3_integration.object_storage.bucket_setup.inspect_bucket_configuration")
	def test_read_only_bucket_permission_failure_keeps_existing_attachment_storage_enabled(
		self, inspect_bucket
	):
		from erpnext_s3_integration.erpnext_s3_integration.doctype.object_storage_settings.object_storage_settings import (
			verify_and_enable_profile,
		)

		self._restore_settings_after_test()
		profile = self._new_guided_profile("Attachments")
		frappe.db.set_single_value(
			"Object Storage Settings",
			{
				"attachment_storage_profile": profile.name,
				"enable_attachment_storage": 1,
				"enable_backup_storage": 0,
			},
		)
		inspect_bucket.return_value = {
			"status": "Failed",
			"checks": [
				{
					"code": "BUCKET_INFO_FAILED",
					"status": "Failed",
					"summary": "Add oss:GetBucketInfo",
				}
			],
		}
		result = verify_and_enable_profile("Attachments", profile.name)
		self.assertTrue(result["kept_existing_feature"])
		self.assertTrue(
			frappe.db.get_single_value("Object Storage Settings", "enable_attachment_storage")
		)

	def _new_guided_profile(self, purpose):
		return frappe.get_doc(
			{
				"doctype": "Object Storage Profile",
				"profile_name": f"Guided {purpose} {frappe.generate_hash(length=8)}",
				"provider": "Alibaba Cloud OSS",
				"purpose": purpose,
				"environment": "Development",
				"credential_mode": "ECS Instance RAM Role",
				"region": "ap-southeast-5",
				"bucket": f"guided-{purpose.lower()}-bucket",
				"confirm_private_bucket": 1,
				"confirm_block_public_access": 1,
			}
		).insert()

	def test_retention_rule_rejects_unknown_reference_field(self):
		settings = frappe.get_single("Object Storage Settings")
		settings.append(
			"retention_rules",
			{
				"rule_name": "Invalid field",
				"enabled": 1,
				"priority": 999,
				"scope": "DocType and Field",
				"reference_doctype": "Item",
				"reference_field": "not_a_real_field",
				"retention_policy": "business-online",
				"category": "test",
			},
		)
		with self.assertRaises(frappe.ValidationError):
			settings.validate()

	def test_runtime_capability_change_requires_ram_policy_review_again(self):
		self._restore_settings_after_test()
		frappe.db.set_single_value("Object Storage Settings", "ram_policy_reviewed", 1)
		settings = frappe.get_single("Object Storage Settings")
		settings.delete_on_last_reference = not settings.delete_on_last_reference
		settings.validate()
		self.assertFalse(settings.ram_policy_reviewed)

	def _restore_settings_after_test(self):
		values = {
			fieldname: frappe.db.get_single_value("Object Storage Settings", fieldname)
			for fieldname in self.SETTINGS_STATE_FIELDS
		}
		self.addCleanup(
			frappe.db.set_single_value,
			"Object Storage Settings",
			values,
			update_modified=False,
		)
