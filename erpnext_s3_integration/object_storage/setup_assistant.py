import json

import frappe
from frappe import _

from erpnext_s3_integration.erpnext_s3_integration.doctype.object_storage_settings.object_storage_settings import (
	_profile_blockers,
)
from erpnext_s3_integration.object_storage.bucket_setup import (
	bucket_verification_fingerprint,
	build_bucket_setup_plan,
)


def build_setup_assistant(settings) -> dict:
	features = [
		_profile_feature(settings, "Attachments"),
		_profile_feature(settings, "Backups"),
		_attachment_archive(settings),
		_archive_restore(settings),
	]
	completed = sum(feature["status"] in {"Available", "Enabled"} for feature in features)
	return {
		"features": features,
		"completed": completed,
		"total": len(features),
		"next_action": next((feature["action"] for feature in features if feature.get("action")), None),
		"status": "Available" if completed == len(features) else "Needs Attention",
	}


def _profile_feature(settings, purpose: str) -> dict:
	profile_field, enabled_field = {
		"Attachments": ("attachment_storage_profile", "enable_attachment_storage"),
		"Backups": ("backup_storage_profile", "enable_backup_storage"),
	}[purpose]
	profile = _profile(settings.get(profile_field), purpose)
	title = _("Attachment Storage") if purpose == "Attachments" else _("Database Backups")
	if not profile:
		return _feature(
			purpose.lower(),
			title,
			"Not Configured",
			_("Create a {0} profile and enter its private OSS bucket.").format(purpose),
			{"type": "new_profile", "purpose": purpose, "label": _("Start Setup")},
		)

	blockers = _profile_blockers(profile, purpose, settings)
	if not blockers and settings.get(enabled_field):
		bucket_failure = _current_bucket_failure(profile, settings)
		if bucket_failure:
			return _feature(
				purpose.lower(),
				title,
				"Needs Attention",
				bucket_failure.get("summary") or _("Bucket security could not be checked."),
				{
					"type": "open_profile",
					"profile_name": profile.name,
					"label": _("Update RAM Policy"),
				},
				profile,
			)
		if purpose == "Backups" and not settings.last_backup_sync:
			return _feature(
				purpose.lower(),
				title,
				"Needs Attention",
				_("The Backup Bucket is ready. Create the first database restore point."),
				{"type": "run_backup", "label": _("Back Up Now")},
				profile,
			)
		summary = (
			_("Latest successful backup: {0}").format(settings.last_backup_sync)
			if purpose == "Backups"
			else _("Upload, download, Range, and permission checks passed.")
		)
		return _feature(purpose.lower(), title, "Available", summary, None, profile)
	if not blockers:
		return _feature(
			purpose.lower(),
			title,
			"Needs Attention",
			_("The profile passed its checks but the feature is disabled."),
			{"type": "verify_enable", "purpose": purpose, "profile_name": profile.name, "label": _("Enable")},
			profile,
		)

	static_codes = {
		"PURPOSE_MISMATCH",
		"BUCKET_MISSING",
		"ENDPOINT_MISMATCH",
		"BUCKET_CHECK_REQUIRED",
		"BUCKET_CHECK_FAILED",
	}
	action_type = "open_profile" if any(item["code"] in static_codes for item in blockers) else "verify_enable"
	return _feature(
		purpose.lower(),
		title,
		"Check Failed" if any(item["code"] in {"TEST_FAILED", "BUCKET_CHECK_FAILED"} for item in blockers) else "Needs Attention",
		blockers[0]["message"],
		{
			"type": action_type,
			"purpose": purpose,
			"profile_name": profile.name,
			"label": _("Open Profile") if action_type == "open_profile" else _("Save and Check"),
		},
		profile,
		blockers,
	)


def _attachment_archive(settings) -> dict:
	profile = _profile(settings.attachment_storage_profile, "Attachments")
	if not settings.enable_attachment_storage or not profile:
		return _feature(
			"attachment_archive",
			_("Attachment Archive"),
			"Not Configured",
			_("Configure attachment storage first. Archive settings do not block normal uploads."),
			None,
			profile,
		)

	if profile.provider != "Alibaba Cloud OSS":
		return _feature(
			"attachment_archive",
			_("Attachment Archive"),
			"Optional",
			_("Verify the tag-filtered lifecycle rule in the provider console."),
			{"type": "open_tab", "fieldname": "attachments_tab", "label": _("View")},
			profile,
		)

	current = profile.get("last_bucket_verification_fingerprint") == bucket_verification_fingerprint(
		profile, settings
	)
	checks = _verification_checks(profile) if current else []
	lifecycle = next((item for item in checks if item.get("code") == "LIFECYCLE"), None)
	dangerous = next((item for item in checks if item.get("code") == "LIFECYCLE_SCOPE"), None)
	configured_summary = build_bucket_setup_plan(profile, settings)["summary"]
	if (
		settings.attachment_lifecycle_reviewed
		and lifecycle
		and lifecycle.get("status") == "Passed"
		and not dangerous
	):
		return _feature(
			"attachment_archive",
			_("Attachment Archive"),
			"Available",
			_("Business archives use the configured lifecycle: {0}.").format(configured_summary),
			{"type": "verify_bucket", "profile_name": profile.name, "label": _("Check Again")},
			profile,
		)
	return _feature(
		"attachment_archive",
		_("Attachment Archive"),
		"Needs Attention",
		(lifecycle or dangerous or {}).get("summary")
		or _("Create the configured tag-filtered lifecycle rule: {0}.").format(
			configured_summary
		),
		{"type": "bucket_setup", "profile_name": profile.name, "label": _("Configure")},
		profile,
	)


def _archive_restore(settings) -> dict:
	if settings.enable_archive_restore and settings.restore_settings_reviewed:
		return _feature(
			"archive_restore",
			_("Archive Restore"),
			"Enabled",
			_("Self-service attachment restore is enabled."),
		)
	return _feature(
		"archive_restore",
		_("Archive Restore"),
		"Optional",
		_("Archived attachments can be restored when the first archived file is available."),
		{"type": "open_tab", "fieldname": "restore_tab", "label": _("View")},
	)


def _verification_checks(profile) -> list[dict]:
	try:
		return json.loads(profile.get("last_bucket_verification_details") or "[]")
	except (TypeError, ValueError):
		return []


def _current_bucket_failure(profile, settings) -> dict | None:
	if profile.provider != "Alibaba Cloud OSS":
		return None
	if profile.get("last_bucket_verification_fingerprint") != bucket_verification_fingerprint(
		profile, settings
	):
		return None
	return next(
		(item for item in _verification_checks(profile) if item.get("status") == "Failed"),
		None,
	)


def _profile(profile_name: str | None, purpose: str):
	name = profile_name or frappe.db.get_value(
		"Object Storage Profile", {"purpose": purpose}, "name", order_by="modified desc"
	)
	return frappe.get_doc("Object Storage Profile", name) if name else None


def _feature(key, title, status, summary, action=None, profile=None, blockers=None) -> dict:
	return {
		"key": key,
		"title": title,
		"status": status,
		"summary": summary,
		"profile": profile.name if profile else None,
		"blockers": blockers or [],
		"action": action,
	}
