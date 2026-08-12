import json
import re

import frappe
from croniter import croniter
from frappe import _
from frappe.model.document import Document
from frappe.utils import cint


class ObjectStorageSettings(Document):
	def validate(self):
		self.default_retention_policy = "unclassified"
		self.proxy_downloads = 1
		self._invalidate_attachment_lifecycle_review()
		self._disable_features_with_changed_capabilities()
		self._invalidate_ram_review()
		self._validate_profiles()
		self._validate_schedule()
		self._validate_attachment_lifecycle()
		self._validate_backup_lifecycle()
		self._validate_retention_rules()
		self._validate_restore()
		self._validate_reviews()

	def on_update(self):
		self._invalidate_changed_capabilities()

	def _validate_profiles(self):
		checks = (
			("enable_attachment_storage", "attachment_storage_profile", "Attachments"),
			("enable_backup_storage", "backup_storage_profile", "Backups"),
		)
		for enabled_field, profile_field, purpose in checks:
			if not self.get(enabled_field):
				continue
			profile_name = self.get(profile_field)
			if not profile_name:
				frappe.throw(_("Select an Object Storage Profile before enabling {0}.").format(purpose))
			profile = frappe.get_doc("Object Storage Profile", profile_name)
			blockers = _profile_blockers(profile, purpose, self)
			if blockers:
				items = "".join(f"<li>{frappe.utils.escape_html(item['message'])}</li>" for item in blockers)
				frappe.throw(
					_("Cannot enable {0} with profile {1}:").format(purpose, profile_name)
					+ f"<ul>{items}</ul>"
					+ _("Use Save, Test and Enable to fix the sequence automatically.")
				)

		if self.attachment_storage_profile and self.backup_storage_profile:
			attachment = frappe.get_doc("Object Storage Profile", self.attachment_storage_profile)
			backup = frappe.get_doc("Object Storage Profile", self.backup_storage_profile)
			if attachment.provider == backup.provider and attachment.bucket == backup.bucket:
				frappe.throw(_("Attachments and backups must use separate buckets."))

	def _validate_schedule(self):
		if self.backup_cron and not croniter.is_valid(self.backup_cron):
			frappe.throw(_("Backup Schedule must be a valid five-part cron expression."))
		if self.enable_backup_storage and cint(self.backup_retention_days) < 1:
			frappe.throw(_("Keep at least one successful daily OSS restore point."))

	def _validate_attachment_lifecycle(self):
		days = [
			cint(self.attachment_lifecycle_ia_days),
			cint(self.attachment_lifecycle_archive_days),
			cint(self.attachment_lifecycle_cold_archive_days),
		]
		delete_days = cint(self.attachment_lifecycle_delete_days)
		if any(day < 0 for day in days) or delete_days < 0:
			frappe.throw(_("Attachment lifecycle days cannot be negative. Use 0 to disable a stage."))
		enabled_days = [day for day in days if day]
		if enabled_days != sorted(enabled_days) or len(set(enabled_days)) != len(enabled_days):
			frappe.throw(_("Enabled attachment lifecycle stages must be strictly increasing."))
		if delete_days and enabled_days and delete_days <= enabled_days[-1]:
			frappe.throw(
				_("Attachment delete days must be 0 (never delete) or later than the last enabled transition.")
			)

	def _invalidate_attachment_lifecycle_review(self):
		if self.is_new():
			return
		if any(
			self.has_value_changed(fieldname)
			for fieldname in (
				"attachment_lifecycle_ia_days",
				"attachment_lifecycle_archive_days",
				"attachment_lifecycle_cold_archive_days",
				"attachment_lifecycle_delete_days",
			)
		):
			self.attachment_lifecycle_reviewed = 0

	def _validate_backup_lifecycle(self):
		if not self.lifecycle_reviewed:
			return
		days = [
			cint(self.lifecycle_ia_days),
			cint(self.lifecycle_archive_days),
			cint(self.lifecycle_cold_archive_days),
			cint(self.lifecycle_delete_days),
		]
		if any(day <= 0 for day in days) or days != sorted(days) or len(set(days)) != len(days):
			frappe.throw(_("Backup lifecycle days must be positive and strictly increasing."))

	def _validate_retention_rules(self):
		required_by_scope = {
			"DocType and Field": ("reference_doctype", "reference_field"),
			"DocType": ("reference_doctype",),
			"MIME Type": ("mime_type",),
			"File Extension": ("file_extension",),
		}
		seen = set()
		for row in self.retention_rules:
			self._normalize_rule(row)
			missing = [field for field in required_by_scope.get(row.scope, ()) if not row.get(field)]
			if missing:
				frappe.throw(_("Retention Rule row {0} is missing: {1}.").format(row.idx, ", ".join(missing)))
			self._validate_rule_values(row)
			identity = _rule_identity(row)
			if identity in seen:
				frappe.throw(_("Retention Rule row {0} conflicts with another rule at the same priority.").format(row.idx))
			seen.add(identity)

	def _normalize_rule(self, row):
		row.file_extension = (row.file_extension or "").strip().lower().lstrip(".")
		row.mime_type = (row.mime_type or "").strip().lower()
		if row.scope not in {"DocType and Field", "DocType"}:
			row.reference_doctype = None
			row.reference_field = None
		elif row.scope == "DocType":
			row.reference_field = None
		if row.scope != "MIME Type":
			row.mime_type = None
		if row.scope != "File Extension":
			row.file_extension = None

	def _validate_rule_values(self, row):
		if cint(row.priority) < 0:
			frappe.throw(_("Retention Rule row {0} Priority cannot be negative.").format(row.idx))
		if row.reference_field and not frappe.get_meta(row.reference_doctype).has_field(row.reference_field):
			frappe.throw(
				_("Retention Rule row {0} references a field that does not exist on {1}.").format(
					row.idx, row.reference_doctype
				)
			)
		if row.mime_type and not re.fullmatch(r"[a-z0-9!#$&^_.+-]+/(?:[a-z0-9!#$&^_.+-]+|\*)", row.mime_type):
			frappe.throw(_("Retention Rule row {0} has an invalid MIME Type.").format(row.idx))
		if row.category and not re.fullmatch(r"[A-Za-z0-9_.-]{1,64}", row.category):
			frappe.throw(
				_("Retention Rule row {0} Category must be a non-sensitive machine label.").format(row.idx)
			)

	def _validate_restore(self):
		interval = cint(self.restore_check_interval_minutes)
		if interval < 15 or interval % 15:
			frappe.throw(_("Restore Check Interval must be 15 minutes or a multiple of 15 minutes."))
		if cint(self.restore_days) < 1:
			frappe.throw(_("Restored Copy Availability must be at least 1 day."))
		if cint(self.restore_retry_limit) < 1:
			frappe.throw(_("Restore Retry Limit must be at least 1."))

	def _validate_reviews(self):
		if self.enable_archive_restore and not self.restore_settings_reviewed:
			frappe.throw(_("Review restore permissions and settings before enabling archive restore."))

	def _invalidate_ram_review(self):
		if self.is_new():
			return
		attachment_changed = any(
			self.has_value_changed(fieldname)
			for fieldname in (
				"delete_on_last_reference",
				"enable_auto_classification",
				"enable_archive_restore",
			)
		)
		backup_delete_capability_changed = bool(self.get_db_value("backup_retention_days")) != bool(
			self.backup_retention_days
		)
		if attachment_changed or backup_delete_capability_changed:
			self.ram_policy_reviewed = 0

	def _disable_features_with_changed_capabilities(self):
		if self.is_new():
			return
		if any(
			self.has_value_changed(fieldname)
			for fieldname in (
				"delete_on_last_reference",
				"enable_auto_classification",
				"enable_archive_restore",
			)
		):
			self.enable_attachment_storage = 0
		if bool(self.get_db_value("backup_retention_days")) != bool(self.backup_retention_days):
			self.enable_backup_storage = 0

	def _invalidate_changed_capabilities(self):
		before = self.get_doc_before_save()
		if not before:
			return
		attachment_changes = [
			fieldname
			for fieldname in ("delete_on_last_reference", "enable_auto_classification", "enable_archive_restore")
			if bool(before.get(fieldname)) != bool(self.get(fieldname))
		]
		backup_changes = []
		if bool(before.backup_retention_days) != bool(self.backup_retention_days):
			backup_changes.append("backup_retention_days")
		self._invalidate_profile("Attachments", self.attachment_storage_profile, attachment_changes)
		self._invalidate_profile("Backups", self.backup_storage_profile, backup_changes)

	@staticmethod
	def _invalidate_profile(purpose: str, profile_name: str | None, changed_fields: list[str]):
		if not profile_name or not changed_fields:
			return
		profile = frappe.get_doc("Object Storage Profile", profile_name)
		reason = _("{0} runtime settings changed ({1}). Run the Full Read/Write Test again.").format(
			purpose, ", ".join(changed_fields)
		)
		profile.invalidate_test(reason)


@frappe.whitelist(methods=["GET"])
def get_setup_assistant() -> dict:
	from erpnext_s3_integration.object_storage.setup_assistant import build_setup_assistant

	frappe.only_for("System Manager")
	return build_setup_assistant(frappe.get_single("Object Storage Settings"))


@frappe.whitelist(methods=["POST"])
def verify_and_enable_profile(purpose: str, profile_name: str | None = None) -> dict:
	frappe.only_for("System Manager")
	if purpose not in {"Attachments", "Backups"}:
		frappe.throw(_("Purpose must be Attachments or Backups."))

	settings = frappe.get_single("Object Storage Settings")
	profile_field, enabled_field = _feature_fields(purpose)
	previous_profile = settings.get(profile_field)
	was_enabled = bool(settings.get(enabled_field))
	profile_name = profile_name or settings.get(profile_field)
	if not profile_name:
		return _blocked_result([{"code": "PROFILE_MISSING", "message": _("Create or select a profile.")}])
	profile = frappe.get_doc("Object Storage Profile", profile_name)
	blockers = _profile_blockers(
		profile,
		purpose,
		settings,
		check_enabled=False,
		check_test=False,
		check_bucket_security=profile.provider != "Alibaba Cloud OSS",
	)
	if blockers:
		return _blocked_result(blockers)

	settings.set(profile_field, profile.name)
	settings.set(enabled_field, 0)
	settings.save()

	from erpnext_s3_integration.object_storage.bucket_setup import inspect_bucket_configuration

	bucket_result = inspect_bucket_configuration(profile, settings, persist=True)
	bucket_blockers = [
		{"code": item["code"], "message": item["summary"]}
		for item in bucket_result["checks"]
		if item["status"] == "Failed"
	]
	if bucket_blockers:
		if was_enabled:
			settings.db_set(
				{profile_field: previous_profile, enabled_field: 1},
				update_modified=False,
			)
		return {
			**_blocked_result(bucket_blockers),
			"enabled": was_enabled,
			"kept_existing_feature": was_enabled,
			"profile": profile.name,
			"bucket_verification": bucket_result,
		}
	if purpose == "Attachments":
		lifecycle_passed = any(
			item["code"] == "LIFECYCLE" and item["status"] == "Passed"
			for item in bucket_result["checks"]
		)
		settings.db_set("attachment_lifecycle_reviewed", int(lifecycle_passed), update_modified=False)

	from erpnext_s3_integration.object_storage.healthcheck import run_full_test

	test_result = run_full_test(profile.name)
	if test_result["status"] != "Passed":
		return {
			**test_result,
			"enabled": False,
			"profile": profile.name,
			"bucket_verification": bucket_result,
		}

	profile.reload()
	profile.enabled = 1
	profile.save()
	settings.reload()
	settings.set(profile_field, profile.name)
	settings.set(enabled_field, 1)
	settings.save()
	return {
		**test_result,
		"enabled": True,
		"profile": profile.name,
		"bucket_verification": bucket_result,
	}


@frappe.whitelist(methods=["POST"])
def apply_recommended_storage_plan() -> dict:
	frappe.only_for("System Manager")
	from erpnext_s3_integration.object_storage.default_rules import sync_default_rules

	settings = frappe.get_single("Object Storage Settings")
	settings.attachment_lifecycle_ia_days = 30
	settings.attachment_lifecycle_archive_days = 90
	settings.attachment_lifecycle_cold_archive_days = 0
	settings.attachment_lifecycle_delete_days = 0
	settings.backup_cron = "0 2 * * *"
	settings.backup_retention_days = 30
	settings.lifecycle_reviewed = 0
	sync_default_rules(settings)
	settings.retention_rules_reviewed = 1
	settings.attachment_lifecycle_reviewed = 0
	settings.save()
	return {
		"attachment_lifecycle": {"ia_days": 30, "archive_days": 90, "cold_days": 0, "delete_days": 0},
		"backup_cron": settings.backup_cron,
		"backup_restore_points": settings.backup_retention_days,
	}


@frappe.whitelist(methods=["POST"])
def apply_recommended_backup_retention() -> dict:
	frappe.only_for("System Manager")
	settings = frappe.get_single("Object Storage Settings")
	settings.backup_retention_days = 30
	settings.save()
	return {"backup_retention_days": 30}


def _profile_is_ready(profile, purpose: str, settings=None) -> bool:
	return not _profile_blockers(profile, purpose, settings)


def _profile_blockers(
	profile,
	purpose: str,
	settings=None,
	*,
	check_enabled: bool = True,
	check_test: bool = True,
	check_bucket_security: bool = True,
) -> list[dict]:
	blockers = []
	if profile.purpose != purpose:
		blockers.append(
		{"code": "PURPOSE_MISMATCH", "message": _("Purpose must be {0}.").format(purpose)}
		)
	if not profile.bucket:
		blockers.append({"code": "BUCKET_MISSING", "message": _("Enter the OSS bucket name.")})
	if profile.provider == "Alibaba Cloud OSS" and profile.environment == "Production":
		from erpnext_s3_integration.erpnext_s3_integration.doctype.object_storage_profile.object_storage_profile import (
			JAKARTA_INTERNAL_ENDPOINT,
			JAKARTA_REGION,
		)

		if (
			profile.region != JAKARTA_REGION
			or not profile.use_internal_endpoint
			or profile.endpoint_url != JAKARTA_INTERNAL_ENDPOINT
		):
			blockers.append(
				{
					"code": "ENDPOINT_MISMATCH",
					"message": _(
						"Production Alibaba Cloud profiles must use the Jakarta internal endpoint."
					),
				}
			)
	if check_bucket_security and not profile.confirm_private_bucket:
		blockers.append({"code": "PRIVATE_UNCONFIRMED", "message": _("Confirm that the bucket is private.")})
	if check_bucket_security and not profile.confirm_block_public_access:
		blockers.append(
		{"code": "BPA_UNCONFIRMED", "message": _("Confirm that Block Public Access is enabled.")}
		)
	if check_bucket_security and profile.provider == "Alibaba Cloud OSS" and profile.environment == "Production":
		from erpnext_s3_integration.object_storage.bucket_setup import bucket_verification_fingerprint

		current_fingerprint = bucket_verification_fingerprint(profile, settings or frappe.get_single("Object Storage Settings"))
		if profile.get("last_bucket_verification_fingerprint") != current_fingerprint:
			blockers.append(
				{"code": "BUCKET_CHECK_REQUIRED", "message": _("Check the production bucket security settings.")}
			)
		elif profile.get("last_bucket_verification_status") == "Failed":
			blockers.append(
				{"code": "BUCKET_CHECK_FAILED", "message": _("Fix the failed production bucket security check.")}
			)
	if check_test:
		if profile.last_test_status == "Failed":
			blockers.append({"code": "TEST_FAILED", "message": _test_failure_message(profile)})
		elif profile.last_test_status != "Passed":
			blockers.append({"code": "TEST_REQUIRED", "message": _("Run the Full Read/Write Test.")})
		elif profile.last_test_fingerprint != profile.config_fingerprint(settings):
			blockers.append(
				{"code": "TEST_STALE", "message": _("Settings changed after the last Full Test.")}
			)
	if check_enabled and not profile.enabled:
		blockers.append({"code": "PROFILE_DISABLED", "message": _("Enable the profile after its test passes.")})
	return blockers


def _test_failure_message(profile) -> str:
	fallback = _("The last Full Test failed. Run it again.")
	try:
		steps = json.loads(profile.last_test_details or "[]")
	except (TypeError, ValueError):
		return fallback
	failed = next((step for step in reversed(steps) if step.get("status") == "Failed"), None)
	if not failed:
		return fallback
	title = failed.get("title") or _("Full Test")
	detail = failed.get("detail")
	return _("{0} failed: {1}").format(title, detail) if detail else _("{0} failed.").format(title)


def _feature_fields(purpose: str) -> tuple[str, str]:
	return {
		"Attachments": ("attachment_storage_profile", "enable_attachment_storage"),
		"Backups": ("backup_storage_profile", "enable_backup_storage"),
	}[purpose]


def _blocked_result(blockers: list[dict]) -> dict:
	return {
		"status": "Blocked",
		"enabled": False,
		"blockers": blockers,
		"message": _("Complete the required profile fields, then try again."),
	}


def _rule_identity(row) -> tuple:
	condition = {
		"DocType and Field": (row.reference_doctype, row.reference_field),
		"DocType": (row.reference_doctype,),
		"MIME Type": (row.mime_type,),
		"File Extension": (row.file_extension,),
	}.get(row.scope, ())
	return (row.scope, *condition, cint(row.priority))
