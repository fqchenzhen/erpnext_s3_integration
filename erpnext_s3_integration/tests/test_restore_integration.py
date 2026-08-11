from unittest.mock import patch

import frappe
from frappe.tests import IntegrationTestCase

from erpnext_s3_integration.object_storage.types import ObjectInfo


class IntegrationTestObjectRestoreRequest(IntegrationTestCase):
	def setUp(self):
		self.profile_name = f"Test Restore {frappe.generate_hash(length=8)}"
		settings = frappe.get_single("Object Storage Settings")
		settings.enable_attachment_storage = 0
		settings.enable_backup_storage = 0
		settings.enable_archive_restore = 1
		settings.restore_settings_reviewed = 1
		settings.enable_auto_classification = 0
		settings.save()
		frappe.get_doc(
			{
				"doctype": "Object Storage Profile",
				"profile_name": self.profile_name,
				"provider": "Alibaba Cloud OSS",
				"purpose": "Attachments",
				"environment": "Development",
				"credential_mode": "ECS Instance RAM Role",
				"region": "ap-southeast-5",
				"use_internal_endpoint": 1,
				"bucket": "restore-test-bucket",
			}
		).insert()
		self.file = frappe.get_doc(
			{
				"doctype": "File",
				"file_name": "archived-test.txt",
				"file_url": f"/s3/test/{frappe.generate_hash(length=12)}",
				"is_private": 0,
				"object_storage_profile": self.profile_name,
				"object_storage_key": f"test/{frappe.generate_hash(length=20)}",
			}
		)
		self.file.flags.copy_from_existing_file = True
		self.file.insert(ignore_permissions=True)

	def test_request_initializes_and_can_cancel_before_submission(self):
		with patch(
			"erpnext_s3_integration.object_storage.service.ObjectStorageService"
		) as service_class:
			service_class.return_value.backend.head.return_value = ObjectInfo(
				self.file.object_storage_key, storage_class="Archive"
			)
			doc = frappe.get_doc({"doctype": "Object Restore Request", "file": self.file.name}).insert()
		self.assertEqual(doc.status, "Requested")
		self.assertEqual(doc.requested_by, frappe.session.user)
		doc.status = "Cancelled"
		doc.save()
		self.assertEqual(doc.status, "Cancelled")

	def test_duplicate_active_request_is_rejected(self):
		with patch("erpnext_s3_integration.object_storage.service.ObjectStorageService") as service_class:
			service_class.return_value.backend.head.return_value = ObjectInfo(
				self.file.object_storage_key, storage_class="Archive"
			)
			frappe.get_doc({"doctype": "Object Restore Request", "file": self.file.name}).insert()
			with self.assertRaises(frappe.ValidationError):
				frappe.get_doc({"doctype": "Object Restore Request", "file": self.file.name}).insert()

	def test_online_attachment_does_not_create_restore_request(self):
		with patch("erpnext_s3_integration.object_storage.service.ObjectStorageService") as service_class:
			service_class.return_value.backend.head.return_value = ObjectInfo(
				self.file.object_storage_key, storage_class="Standard"
			)
			with self.assertRaises(frappe.ValidationError):
				frappe.get_doc({"doctype": "Object Restore Request", "file": self.file.name}).insert()
