import hashlib

import frappe
from frappe import _
from frappe.model.document import Document

JAKARTA_REGION = "ap-southeast-5"
JAKARTA_PUBLIC_ENDPOINT = "https://oss-ap-southeast-5.aliyuncs.com"
JAKARTA_INTERNAL_ENDPOINT = "https://oss-ap-southeast-5-internal.aliyuncs.com"


class ObjectStorageProfile(Document):
	def validate(self):
		self.prefix = (self.prefix or "").strip().strip("/")
		self.endpoint_url = (self.endpoint_url or "").strip().rstrip("/")
		self._set_alibaba_defaults()
		self._validate_production_alibaba()
		self._validate_credentials()
		self._validate_linked_purpose()
		self._invalidate_stale_test()
		self._invalidate_stale_bucket_verification()
		self._validate_enablement()

	def on_update(self):
		from erpnext_s3_integration.object_storage.service import clear_backend_cache

		clear_backend_cache(self.name)

	def _set_alibaba_defaults(self):
		if self.provider != "Alibaba Cloud OSS":
			return
		self.region = self.region or JAKARTA_REGION
		if self.region != JAKARTA_REGION:
			return
		if self.is_new() and self.environment != "Production" and not self.endpoint_url:
			self.use_internal_endpoint = 0
		known_endpoints = {"", JAKARTA_PUBLIC_ENDPOINT, JAKARTA_INTERNAL_ENDPOINT}
		if self.endpoint_url in known_endpoints:
			self.endpoint_url = (
				JAKARTA_INTERNAL_ENDPOINT if self.use_internal_endpoint else JAKARTA_PUBLIC_ENDPOINT
			)

	def _validate_credentials(self):
		if self.credential_mode == "AccessKey" and not (
			self.get_password("access_key_id") and self.get_password("access_key_secret")
		):
			frappe.throw(_("Access Key ID and Access Key Secret are required in AccessKey mode."))

	def _validate_production_alibaba(self):
		if self.provider != "Alibaba Cloud OSS" or self.environment != "Production":
			return
		if (
			self.region != JAKARTA_REGION
			or not self.use_internal_endpoint
			or self.endpoint_url != JAKARTA_INTERNAL_ENDPOINT
		):
			frappe.throw(_("Production Alibaba Cloud OSS must use the Jakarta region and internal endpoint."))
		if self.credential_mode == "AccessKey":
			frappe.throw(_("Use an ECS Instance RAM Role or Default Credential Chain in production."))

	def _validate_enablement(self):
		if not self.enabled:
			return
		if not self.confirm_private_bucket or not self.confirm_block_public_access:
			frappe.throw(_("Confirm that the bucket is private and Block Public Access is enabled."))
		if self.provider == "Alibaba Cloud OSS" and self.environment == "Production":
			from erpnext_s3_integration.object_storage.bucket_setup import (
				bucket_verification_fingerprint,
			)

			settings = frappe.get_single("Object Storage Settings")
			if (
				self.get("last_bucket_verification_fingerprint")
				!= bucket_verification_fingerprint(self, settings)
				or self.get("last_bucket_verification_status") not in {"Passed", "Warning"}
			):
				frappe.throw(_("Check the production bucket security settings before enabling this profile."))
		if self.last_test_status != "Passed" or self.last_test_fingerprint != self.config_fingerprint():
			frappe.throw(_("Run and pass the Full Read/Write Test after the latest configuration change."))

	def _validate_linked_purpose(self):
		if self.is_new() or not self.has_value_changed("purpose"):
			return
		settings = frappe.get_single("Object Storage Settings")
		if self.name in {settings.attachment_storage_profile, settings.backup_storage_profile}:
			frappe.throw(_("Unlink this profile from Object Storage Settings before changing Purpose."))

	def _invalidate_stale_test(self):
		if not self.last_test_fingerprint or self.last_test_fingerprint == self.config_fingerprint():
			return
		self.last_test_status = "Not Tested"
		self.last_tested_at = None
		self.last_test_fingerprint = None
		self.last_test_details = self.flags.get("test_invalidation_reason") or _(
			"Configuration changed. Run the Full Read/Write Test again."
		)
		self.enabled = 0

	def _invalidate_stale_bucket_verification(self):
		fingerprint = self.get("last_bucket_verification_fingerprint")
		if not fingerprint:
			return
		from erpnext_s3_integration.object_storage.bucket_setup import (
			bucket_verification_fingerprint,
		)

		settings = frappe.get_single("Object Storage Settings")
		if fingerprint == bucket_verification_fingerprint(self, settings):
			return
		self.last_bucket_verification_status = "Not Checked"
		self.last_bucket_verified_at = None
		self.last_bucket_verification_fingerprint = None
		self.last_bucket_verification_details = _(
			"Bucket, endpoint, environment, or prefix changed. Check again."
		)

	def invalidate_test(self, reason: str) -> None:
		if not self.last_test_fingerprint and self.last_test_status == "Not Tested" and not self.enabled:
			return
		self.flags.test_invalidation_reason = reason
		self.last_test_fingerprint = "stale"
		self.save(ignore_permissions=True)

	def config_fingerprint(self, settings=None) -> str:
		credential_fingerprint = ""
		if self.credential_mode == "AccessKey":
			credential_fingerprint = hashlib.sha256(
				f"{self.get_password('access_key_id')}\x1f{self.get_password('access_key_secret')}".encode()
			).hexdigest()
		values = (
			self.provider,
			self.purpose,
			self.environment,
			self.credential_mode,
			self.ram_role_name,
			credential_fingerprint,
			self.region,
			self.endpoint_url,
			self.bucket,
			self.prefix,
			str(self.use_internal_endpoint),
			self.addressing_style,
			self.server_side_encryption,
			*_capability_fingerprint_values(self.purpose, settings),
		)
		return hashlib.sha256("\x1f".join(value or "" for value in values).encode()).hexdigest()


@frappe.whitelist(methods=["POST"])
def apply_recommended_jakarta_settings(environment: str = "Production") -> dict:
	frappe.only_for("System Manager")
	if environment not in {"Production", "Staging", "Development"}:
		frappe.throw(_("Select a valid Environment before applying Jakarta settings."))
	production = environment == "Production"
	return {
		"region": JAKARTA_REGION,
		"endpoint_url": JAKARTA_INTERNAL_ENDPOINT if production else JAKARTA_PUBLIC_ENDPOINT,
		"use_internal_endpoint": int(production),
		"credential_mode": "ECS Instance RAM Role" if production else "AccessKey",
	}


@frappe.whitelist(methods=["POST"])
def apply_jakarta_defaults(profile_name: str | None = None) -> dict:
	return apply_recommended_jakarta_settings("Production")


@frappe.whitelist(methods=["POST"])
def use_profile_in_settings(profile_name: str) -> dict:
	frappe.only_for("System Manager")
	profile = frappe.get_doc("Object Storage Profile", profile_name)
	profile.check_permission("read")
	fieldname = {
		"Attachments": "attachment_storage_profile",
		"Backups": "backup_storage_profile",
	}[profile.purpose]
	settings = frappe.get_single("Object Storage Settings")
	other_field = (
		"backup_storage_profile" if fieldname == "attachment_storage_profile" else "attachment_storage_profile"
	)
	other_name = settings.get(other_field)
	if other_name:
		other = frappe.get_doc("Object Storage Profile", other_name)
		if other.provider == profile.provider and other.bucket == profile.bucket:
			frappe.throw(_("Attachments and backups must use separate buckets."))
	settings.set(fieldname, profile.name)
	settings.save()
	return {"fieldname": fieldname, "settings_route": "/app/object-storage-settings"}


def _capability_fingerprint_values(purpose: str, settings=None) -> tuple[str, ...]:
	if not frappe.db.exists("DocType", "Object Storage Settings"):
		return ()
	settings = settings or frappe.get_single("Object Storage Settings")
	if purpose == "Attachments":
		return (
			str(bool(settings.delete_on_last_reference)),
			str(bool(settings.enable_auto_classification)),
			str(bool(settings.enable_archive_restore)),
		)
	return (
		str(bool(settings.backup_retention_days)),
	)
