import hashlib
import json

import alibabacloud_oss_v2 as oss
import frappe
from frappe import _
from frappe.utils import now_datetime

from erpnext_s3_integration.object_storage.alibaba_oss import AlibabaOSSBackend
from erpnext_s3_integration.object_storage.errors import classify_storage_error

RECOMMENDED_LIFECYCLE_ID = "erpnext-business-archive"
BUSINESS_ARCHIVE_TAG = {"retention": "business-archive"}


@frappe.whitelist(methods=["GET"])
def get_bucket_setup_plan(profile_name: str) -> dict:
	frappe.only_for("System Manager")
	profile = frappe.get_doc("Object Storage Profile", profile_name)
	profile.check_permission("read")
	return build_bucket_setup_plan(profile, frappe.get_single("Object Storage Settings"))


@frappe.whitelist(methods=["POST"])
def verify_bucket_configuration(profile_name: str) -> dict:
	frappe.only_for("System Manager")
	profile = frappe.get_doc("Object Storage Profile", profile_name)
	profile.check_permission("write")
	return inspect_bucket_configuration(profile, frappe.get_single("Object Storage Settings"), persist=True)


def build_bucket_setup_plan(profile, settings) -> dict:
	prefix = attachment_object_prefix(profile)
	transitions = _expected_transitions(settings)
	expiration_days = _positive_int(settings.attachment_lifecycle_delete_days)
	return {
		"rule_id": RECOMMENDED_LIFECYCLE_ID,
		"prefix": prefix,
		"tag_key": "retention",
		"tag_value": "business-archive",
		"transitions": transitions,
		"expiration_days": expiration_days,
		"console_url": f"https://oss.console.aliyun.com/bucket/oss-{profile.region}/{profile.bucket}/lifecycle",
		"summary": _lifecycle_summary(transitions, expiration_days),
	}


def inspect_bucket_configuration(profile, settings, *, persist: bool = False) -> dict:
	if profile.provider != "Alibaba Cloud OSS":
		result = {
			"status": "Warning",
			"checked_at": now_datetime(),
			"fingerprint": bucket_verification_fingerprint(profile, settings),
			"checks": [
				_check(
					"PROVIDER_MANUAL",
					_("Bucket security"),
					"Warning",
					_("Verify the bucket security settings in the provider console."),
				)
			],
		}
		if persist:
			_persist_verification(profile, result)
		return result

	try:
		backend = AlibabaOSSBackend(profile)
		info_result = backend.client.get_bucket_info(oss.GetBucketInfoRequest(bucket=profile.bucket))
		bucket_info = info_result.bucket_info
	except Exception as exc:
		result = _inspection_error(profile, settings, exc, "BUCKET_INFO_FAILED")
		if persist:
			_persist_verification(profile, result)
		return result

	checks = _bucket_info_checks(profile, bucket_info)
	if profile.purpose == "Attachments":
		checks.extend(_lifecycle_checks(backend, profile, settings))
	result = {
		"status": _overall_status(checks),
		"checked_at": now_datetime(),
		"fingerprint": bucket_verification_fingerprint(profile, settings),
		"checks": checks,
		"setup_plan": build_bucket_setup_plan(profile, settings)
		if profile.purpose == "Attachments"
		else None,
	}
	if persist:
		profile.db_set(
			{
				"confirm_private_bucket": int((bucket_info.acl or "").lower() == "private"),
				"confirm_block_public_access": int(bool(bucket_info.block_public_access)),
			},
			update_modified=False,
		)
		_persist_verification(profile, result)
		if profile.purpose == "Attachments":
			lifecycle_ready = any(
				item["code"] == "LIFECYCLE" and item["status"] == "Passed"
				for item in checks
			) and not any(item["code"] == "LIFECYCLE_SCOPE" for item in checks)
			frappe.db.set_single_value(
				"Object Storage Settings",
				"attachment_lifecycle_reviewed",
				int(lifecycle_ready),
			)
	return result


def bucket_verification_fingerprint(profile, settings=None) -> str:
	values = [
		profile.provider,
		profile.purpose,
		profile.environment,
		profile.region,
		profile.endpoint_url,
		profile.bucket,
		profile.prefix,
	]
	return hashlib.sha256("\x1f".join(value or "" for value in values).encode()).hexdigest()


def attachment_object_prefix(profile) -> str:
	profile_prefix = (profile.prefix or "").strip("/")
	return "/".join(part for part in (profile_prefix, "attachments/") if part)


def _bucket_info_checks(profile, info) -> list[dict]:
	location = (info.location or "").removeprefix("oss-")
	checks = [
		_check(
			"REGION",
			_("Jakarta region"),
			"Passed" if location == "ap-southeast-5" else "Failed",
			_("Bucket region: {0}").format(info.location or _("Unknown")),
			"ap-southeast-5",
			info.location,
		),
		_check(
			"PRIVATE_BUCKET",
			_("Private bucket"),
			"Passed" if (info.acl or "").lower() == "private" else "Failed",
			_("Bucket ACL: {0}").format(info.acl or _("Unknown")),
			"private",
			info.acl,
		),
		_check(
			"BLOCK_PUBLIC_ACCESS",
			_("Block Public Access"),
			"Passed" if info.block_public_access else "Failed",
			_("Public access is blocked.")
			if info.block_public_access
			else _("Enable Block Public Access for this bucket."),
			True,
			bool(info.block_public_access),
		),
	]
	redundancy = info.data_redundancy_type or "Unknown"
	if redundancy == "ZRS":
		status = "Passed"
	elif profile.environment == "Production":
		status = "Failed"
	else:
		status = "Warning"
	checks.append(
		_check(
			"REDUNDANCY",
			_("Zone-redundant storage"),
			status,
			_("Bucket redundancy: {0}").format(redundancy),
			"ZRS",
			redundancy,
		)
	)
	return checks


def _lifecycle_checks(backend, profile, settings) -> list[dict]:
	try:
		result = backend.client.get_bucket_lifecycle(
			oss.GetBucketLifecycleRequest(bucket=profile.bucket)
		)
		rules = result.lifecycle_configuration.rules or [] if result.lifecycle_configuration else []
	except Exception as exc:
		details = classify_storage_error(exc)
		if details.code in {"NoSuchLifecycle", "NoSuchLifecycleConfiguration"} or details.category == "Not Found":
			rules = []
		else:
			return [
				_check(
					"LIFECYCLE_READ_FAILED",
					_("Attachment archive rule"),
					"Warning",
					_("The app cannot read lifecycle rules. Add oss:GetBucketLifecycle and check again."),
					code_detail=details.code,
					ram_action="oss:GetBucketLifecycle",
					request_id=_request_id(exc),
				)
			]

	plan = build_bucket_setup_plan(profile, settings)
	exact = next((rule for rule in rules if _rule_matches(rule, plan)), None)
	checks = [
		_check(
			"LIFECYCLE",
			_("Attachment archive rule"),
			"Passed" if exact else "Warning",
			_("The recommended 30-day IA and 365-day Archive rule is configured.")
			if exact
			else _("Create the recommended tag-filtered lifecycle rule."),
			plan["summary"],
			_exact_rule_summary(exact) if exact else None,
		)
	]
	dangerous = [rule.id or _("Unnamed rule") for rule in rules if _is_dangerous_rule(rule, plan["prefix"])]
	if dangerous:
		checks.append(
			_check(
				"LIFECYCLE_SCOPE",
				_("Lifecycle rule scope"),
				"Warning",
				_("Rules without the business-archive tag may affect online attachments: {0}").format(
					", ".join(dangerous)
				),
			)
		)
	return checks


def _rule_matches(rule, plan: dict) -> bool:
	if rule.status != "Enabled" or (rule.prefix or "") != plan["prefix"]:
		return False
	tags = {tag.key: tag.value for tag in (rule.tags or [])}
	if tags != BUSINESS_ARCHIVE_TAG:
		return False
	transitions = sorted(
		(item.days, _storage_class_value(item.storage_class)) for item in (rule.transitions or [])
	)
	expected = sorted((item["days"], item["storage_class"]) for item in plan["transitions"])
	if transitions != expected:
		return False
	if plan["expiration_days"] is None:
		return rule.expiration is None
	return bool(rule.expiration) and getattr(rule.expiration, "days", None) == plan["expiration_days"]


def _is_dangerous_rule(rule, target_prefix: str) -> bool:
	if rule.status != "Enabled" or not _prefixes_overlap(rule.prefix or "", target_prefix):
		return False
	tags = {tag.key: tag.value for tag in (rule.tags or [])}
	return tags.get("retention") != "business-archive" and bool(rule.transitions or rule.expiration)


def _expected_transitions(settings) -> list[dict]:
	configured = (
		("IA", _positive_int(settings.attachment_lifecycle_ia_days)),
		("Archive", _positive_int(settings.attachment_lifecycle_archive_days)),
		("ColdArchive", _positive_int(settings.attachment_lifecycle_cold_archive_days)),
	)
	return [
		{"days": days, "storage_class": storage_class}
		for storage_class, days in configured
		if days is not None
	]


def _positive_int(value) -> int | None:
	value = int(value or 0)
	return value if value > 0 else None


def _lifecycle_summary(transitions: list[dict], expiration_days: int | None = None) -> str:
	parts = [f"{item['days']}d {item['storage_class']}" for item in transitions]
	parts.append(f"{expiration_days}d Delete" if expiration_days else "Never delete")
	return " → ".join(parts)


def _exact_rule_summary(rule) -> str | None:
	if not rule:
		return None
	return _lifecycle_summary(
		[
			{"days": item.days, "storage_class": _storage_class_value(item.storage_class)}
			for item in (rule.transitions or [])
		],
		getattr(rule.expiration, "days", None) if rule.expiration else None,
	)


def _storage_class_value(value) -> str:
	return str(getattr(value, "value", value))


def _prefixes_overlap(first: str, second: str) -> bool:
	return first.startswith(second) or second.startswith(first)


def _overall_status(checks: list[dict]) -> str:
	if any(item["status"] == "Failed" for item in checks):
		return "Failed"
	if any(item["status"] == "Warning" for item in checks):
		return "Warning"
	return "Passed"


def _inspection_error(profile, settings, exc: Exception, code: str) -> dict:
	details = classify_storage_error(exc)
	summaries = {
		"Authorization": _(
			"The app cannot check bucket security. Add oss:GetBucketInfo to the generated RAM policy."
		),
		"Not Found": _("The bucket was not found. Check its name, Jakarta region, and endpoint."),
		"Network": _("The OSS endpoint could not be reached. Check the endpoint and server network."),
	}
	return {
		"status": "Failed",
		"checked_at": now_datetime(),
		"fingerprint": bucket_verification_fingerprint(profile, settings),
		"checks": [
			_check(
				code,
				_("Bucket configuration"),
				"Failed",
				summaries.get(
					details.category,
					_("The bucket configuration could not be checked ({0}).").format(details.category),
				),
				code_detail=details.code,
				ram_action="oss:GetBucketInfo",
				request_id=_request_id(exc),
			)
		],
	}


def _persist_verification(profile, result: dict) -> None:
	profile.db_set(
		{
			"last_bucket_verification_status": result["status"],
			"last_bucket_verified_at": result["checked_at"],
			"last_bucket_verification_fingerprint": result["fingerprint"],
			"last_bucket_verification_details": json.dumps(result["checks"], ensure_ascii=False),
		},
		update_modified=False,
	)


def _check(
	code: str,
	title: str,
	status: str,
	summary: str,
	expected=None,
	actual=None,
	*,
	code_detail: str | None = None,
	**details,
) -> dict:
	provider_code = code_detail or details.get("code")
	return {
		"code": code,
		"title": title,
		"status": status,
		"summary": summary,
		"expected": expected,
		"actual": actual,
		"provider_code": provider_code,
		**details,
	}


def _request_id(exc: Exception) -> str | None:
	current = exc
	seen = set()
	while isinstance(current, Exception) and id(current) not in seen:
		seen.add(id(current))
		if request_id := getattr(current, "request_id", None):
			return str(request_id)
		unwrap = getattr(current, "unwrap", None)
		if not callable(unwrap):
			break
		try:
			current = unwrap()
		except Exception:
			break
	return None
