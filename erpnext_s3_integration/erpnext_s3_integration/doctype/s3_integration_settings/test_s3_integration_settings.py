import frappe
from frappe.tests import IntegrationTestCase


class IntegrationTestS3IntegrationSettings(IntegrationTestCase):
	def setUp(self):
		self.settings = frappe.get_single("S3 Integration Settings")
		self.settings.enable_attachments_s3 = 0
		self.settings.enable_backups_s3 = 0

	def test_alibaba_provider_normalizes_endpoint_and_addressing(self):
		self.settings.provider = "Alibaba Cloud OSS"
		self.settings.region_name = "ap-southeast-5"
		self.settings.endpoint_url = "https://oss-ap-southeast-5.aliyuncs.com/"
		self.settings.addressing_style = "path"
		self.settings.use_path_style = 1

		self.settings.before_validate()
		self.settings.validate()

		self.assertEqual(
			self.settings.endpoint_url,
			"https://s3.oss-ap-southeast-5.aliyuncs.com",
		)
		self.assertEqual(self.settings.addressing_style, "virtual")
		self.assertEqual(self.settings.use_path_style, 0)

	def test_alibaba_jakarta_internal_endpoint_is_accepted(self):
		self.settings.provider = "Alibaba Cloud OSS"
		self.settings.region_name = "ap-southeast-5"
		self.settings.endpoint_url = "https://oss-ap-southeast-5-internal.aliyuncs.com"
		self.settings.before_validate()

		self.settings.validate_endpoint_url()
		self.assertEqual(
			self.settings.endpoint_url,
			"https://s3.oss-ap-southeast-5-internal.aliyuncs.com",
		)

	def test_alibaba_endpoint_region_must_match(self):
		self.settings.provider = "Alibaba Cloud OSS"
		self.settings.region_name = "ap-southeast-1"
		self.settings.endpoint_url = "https://s3.oss-ap-southeast-5.aliyuncs.com"
		self.settings.before_validate()

		with self.assertRaises(frappe.ValidationError):
			self.settings.validate_endpoint_url()

	def test_alibaba_cname_endpoint_is_rejected(self):
		self.settings.provider = "Alibaba Cloud OSS"
		self.settings.region_name = "ap-southeast-5"
		self.settings.endpoint_url = "https://files.example.com"
		self.settings.before_validate()

		with self.assertRaises(frappe.ValidationError):
			self.settings.validate_endpoint_url()

	def test_backup_requires_an_upload_type(self):
		self.settings.enable_backups_s3 = 1
		self.settings.upload_db_backup = 0
		self.settings.upload_files_backup = 0

		with self.assertRaises(frappe.ValidationError):
			self.settings.validate_backup_settings()

	def test_backup_schedule_must_be_valid(self):
		self.settings.enable_backups_s3 = 1
		self.settings.upload_db_backup = 1
		self.settings.backup_cron_expression = "not a cron expression"

		with self.assertRaises(frappe.ValidationError):
			self.settings.validate_backup_settings()
