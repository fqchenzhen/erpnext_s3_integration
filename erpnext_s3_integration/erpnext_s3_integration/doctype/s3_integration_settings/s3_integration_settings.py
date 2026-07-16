import re
from urllib.parse import urlsplit

import frappe
from frappe import _
from frappe.model.document import Document
from frappe.utils import cint

ALIBABA_CLOUD_OSS = "Alibaba Cloud OSS"
MINIO = "MinIO"


class S3IntegrationSettings(Document):
	def before_validate(self):
		self.endpoint_url = (self.endpoint_url or "").strip().rstrip("/")

		if self.provider == ALIBABA_CLOUD_OSS:
			self.endpoint_url = re.sub(
				r"^(https?://)oss-([a-z0-9-]+)\.aliyuncs\.com$",
				r"\1s3.oss-\2.aliyuncs.com",
				self.endpoint_url,
				flags=re.IGNORECASE,
			)
			self.addressing_style = "virtual"
			self.use_path_style = 0

	def validate(self):
		self.validate_addressing_style()
		self.validate_endpoint_url()
		self.validate_required_connection_fields()
		self.validate_backup_settings()

	def validate_addressing_style(self):
		if self.get("addressing_style") and self.addressing_style not in {"auto", "virtual", "path"}:
			frappe.throw(_("Addressing Style must be one of auto, virtual, or path."))

	def validate_endpoint_url(self):
		if not self.endpoint_url:
			return

		try:
			endpoint = urlsplit(self.endpoint_url)
			endpoint.port
		except ValueError:
			frappe.throw(_("Endpoint URL is invalid."))

		if endpoint.scheme not in {"http", "https"} or not endpoint.hostname:
			frappe.throw(_("Endpoint URL must be a complete HTTP or HTTPS URL."))
		if endpoint.username or endpoint.password:
			frappe.throw(_("Endpoint URL must not contain credentials."))
		if endpoint.path not in {"", "/"} or endpoint.query or endpoint.fragment:
			frappe.throw(_("Endpoint URL must not contain a bucket name, path, query, or fragment."))

		if self.bucket_name and endpoint.hostname.startswith(f"{self.bucket_name}."):
			frappe.throw(_("Endpoint URL must not include the bucket name."))

		if self.provider == ALIBABA_CLOUD_OSS:
			self.validate_alibaba_endpoint(endpoint)

	def validate_alibaba_endpoint(self, endpoint):
		if endpoint.scheme != "https":
			frappe.throw(_("Alibaba Cloud OSS endpoints must use HTTPS."))
		if not endpoint.hostname.endswith(".aliyuncs.com"):
			frappe.throw(
				_(
					"This boto3 integration does not support bucket-bound OSS CNAME endpoints. Use an official OSS S3-compatible endpoint."
				)
			)

		match = re.fullmatch(
			r"(?:s3\.)?oss-(?P<region>[a-z0-9-]+?)(?:-internal)?\.aliyuncs\.com",
			endpoint.hostname,
		)
		endpoint_region = match.group("region") if match else None
		if (
			endpoint_region
			and self.region_name
			and endpoint_region != "accelerate"
			and endpoint_region != self.region_name
		):
			frappe.throw(
				_("Region Name must match the Alibaba Cloud OSS endpoint region ({0}).").format(
					endpoint_region
				)
			)

	def validate_required_connection_fields(self):
		if not (self.enable_attachments_s3 or self.enable_backups_s3):
			return

		required_fields = ["aws_access_key_id", "region_name", "bucket_name"]
		if self.provider in {ALIBABA_CLOUD_OSS, MINIO}:
			required_fields.append("endpoint_url")

		missing = [
			_(self.meta.get_field(fieldname).label)
			for fieldname in required_fields
			if not self.get(fieldname)
		]
		if not self.get_password("aws_secret_access_key", raise_exception=False):
			missing.append(_(self.meta.get_field("aws_secret_access_key").label))

		if missing:
			frappe.throw(
				_("The following fields are required when S3 Integration features are enabled: {0}").format(
					", ".join(missing)
				)
			)

	def validate_backup_settings(self):
		if not self.enable_backups_s3:
			return
		if not (self.upload_db_backup or self.upload_files_backup):
			frappe.throw(_("Select at least one backup type to upload."))
		if cint(self.delete_backups_older_than_days) < 0:
			frappe.throw(_("Backup retention days cannot be negative."))
		if self.backup_cron_expression:
			from croniter import croniter

			if not croniter.is_valid(self.backup_cron_expression):
				frappe.throw(_("Backup Schedule must be a valid CRON expression."))


@frappe.whitelist()
def test_s3_connection():
	frappe.only_for("System Manager")
	try:
		from erpnext_s3_integration.s3_client import S3Client

		client = S3Client()
		success, msg = client.test_connection()

		return {"success": success, "message": msg}
	except Exception as e:
		return {"success": False, "message": str(e)}


@frappe.whitelist()
def take_backup_and_sync():
	frappe.only_for("System Manager")

	settings = frappe.get_single("S3 Integration Settings")
	if not settings.enable_backups_s3:
		frappe.throw(_("S3 Backups are currently disabled in Settings."))

	frappe.enqueue(
		"erpnext_s3_integration.erpnext_s3_integration.doctype.s3_integration_settings.s3_integration_settings.run_backup_and_sync",
		queue="long",
		timeout=1500,
	)
	return "Backup and Sync job enqueued successfully."


def run_backup_and_sync():
	from erpnext_s3_integration.backup_hooks import run_backup_and_sync as execute_backup_and_sync

	try:
		execute_backup_and_sync(create_new_backup=True, update_last_sync=False)

	except Exception:
		frappe.log_error(message=frappe.get_traceback(), title="Manual S3 Backup Sync Failed")
