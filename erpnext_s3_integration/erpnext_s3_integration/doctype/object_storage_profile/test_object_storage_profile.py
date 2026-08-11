import frappe
from frappe.tests import IntegrationTestCase


class IntegrationTestObjectStorageProfile(IntegrationTestCase):
	def test_jakarta_internal_endpoint_is_applied(self):
		doc = frappe.get_doc(
			{
				"doctype": "Object Storage Profile",
				"profile_name": f"Test Jakarta {frappe.generate_hash(length=8)}",
				"provider": "Alibaba Cloud OSS",
				"purpose": "Attachments",
				"environment": "Production",
				"credential_mode": "ECS Instance RAM Role",
				"region": "ap-southeast-5",
				"use_internal_endpoint": 1,
				"bucket": "private-test-bucket",
				"confirm_private_bucket": 1,
				"confirm_block_public_access": 1,
			}
		)
		doc.insert()
		self.assertEqual(doc.endpoint_url, "https://oss-ap-southeast-5-internal.aliyuncs.com")

	def test_jakarta_public_endpoint_is_applied_for_development(self):
		doc = frappe.get_doc(
			{
				"doctype": "Object Storage Profile",
				"profile_name": f"Test Public Jakarta {frappe.generate_hash(length=8)}",
				"provider": "Alibaba Cloud OSS",
				"purpose": "Attachments",
				"environment": "Development",
				"credential_mode": "ECS Instance RAM Role",
				"region": "ap-southeast-5",
				"bucket": "private-test-bucket",
			}
		).insert()
		self.assertFalse(doc.use_internal_endpoint)
		self.assertEqual(doc.endpoint_url, "https://oss-ap-southeast-5.aliyuncs.com")

	def test_profile_cannot_enable_without_full_test(self):
		doc = frappe.get_doc(
			{
				"doctype": "Object Storage Profile",
				"profile_name": f"Test Gate {frappe.generate_hash(length=8)}",
				"provider": "Alibaba Cloud OSS",
				"purpose": "Attachments",
				"environment": "Production",
				"credential_mode": "ECS Instance RAM Role",
				"region": "ap-southeast-5",
				"use_internal_endpoint": 1,
				"bucket": "private-test-bucket",
				"confirm_private_bucket": 1,
				"confirm_block_public_access": 1,
				"enabled": 1,
			}
		)
		with self.assertRaises(frappe.ValidationError):
			doc.insert()

	def test_production_profile_rejects_access_key(self):
		doc = frappe.get_doc(
			{
				"doctype": "Object Storage Profile",
				"profile_name": f"Test AccessKey {frappe.generate_hash(length=8)}",
				"provider": "Alibaba Cloud OSS",
				"purpose": "Attachments",
				"environment": "Production",
				"credential_mode": "AccessKey",
				"access_key_id": "test",
				"access_key_secret": "test",
				"region": "ap-southeast-5",
				"use_internal_endpoint": 1,
				"bucket": "private-test-bucket",
			}
		)
		with self.assertRaises(frappe.ValidationError):
			doc.insert()

	def test_configuration_change_invalidates_test_and_disables_profile(self):
		doc = frappe.get_doc(
			{
				"doctype": "Object Storage Profile",
				"profile_name": f"Test Fingerprint {frappe.generate_hash(length=8)}",
				"provider": "Alibaba Cloud OSS",
				"purpose": "Attachments",
				"environment": "Development",
				"credential_mode": "ECS Instance RAM Role",
				"region": "ap-southeast-5",
				"use_internal_endpoint": 1,
				"bucket": "fingerprint-test-bucket",
				"confirm_private_bucket": 1,
				"confirm_block_public_access": 1,
			}
		)
		doc.last_test_status = "Passed"
		doc.last_test_fingerprint = doc.config_fingerprint()
		doc.enabled = 1
		doc.insert()

		doc.bucket = "changed-test-bucket"
		doc.save()
		self.assertFalse(doc.enabled)
		self.assertEqual(doc.last_test_status, "Not Tested")
		self.assertFalse(doc.last_test_fingerprint)
