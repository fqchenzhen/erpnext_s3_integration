import hashlib
import json

import alibabacloud_oss_v2 as oss
import frappe
from frappe import _
from frappe.utils import now_datetime

from erpnext_s3_integration.object_storage.alibaba_oss import AlibabaOSSBackend
from erpnext_s3_integration.object_storage.errors import classify_storage_error

ARCHIVE_DELETE_ID = "erpnext-archive-delete"
ARCHIVE_KEEP_ID = "erpnext-archive-keep"


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
	delete_rule = {
		"rule_id": ARCHIVE_DELETE_ID, "prefix": prefix,
		"tag_key": "retention", "tag_value": "archive-delete",
		"transitions": transitions, "expiration_days": 365,
	}
	keep_rule = {
		"rule_id": ARCHIVE_KEEP_ID, "prefix": prefix,
		"tag_key": "retention", "tag_value": "archive-keep",
		"transitions": transitions, "expiration_days": None,
	}
	return {
		**delete_rule, "rules": [delete_rule, keep_rule],
		"console_url": f"https://oss.console.aliyun.com/bucket/oss-{profile.region}/{profile.bucket}/lifecycle",
		"summary": _lifecycle_summary(transitions, 365) + "; archive-keep: Never delete",
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
			lifecycle_ready = all(
				any(item["code"] == code and item["status"] == "Passed" for item in checks)
				for code in ("LIFECYCLE", "LIFECYCLE_KEEP")
			) and not any(item["code"] in {"LIFECYCLE_SCOPE", "LIFECYCLE_TRANSITIONS"} for item in checks)
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
			return [_check(
				"LIFECYCLE_READ_FAILED", _("Attachment archive rules"), "Warning",
				_("The app cannot read lifecycle rules. Add oss:GetBucketLifecycle and check again."),
				code_detail=details.code, ram_action="oss:GetBucketLifecycle", request_id=_request_id(exc),
			)]

	plans = build_bucket_setup_plan(profile, settings)["rules"]
	checks = []
	matched = {}
	for plan in plans:
		code = "LIFECYCLE" if plan["tag_value"] == "archive-delete" else "LIFECYCLE_KEEP"
		rule = next((r for r in rules if _rule_matches(r, plan)), None)
		matched[plan["tag_value"]] = rule
		checks.append(_check(
			code,
			_("Archive-delete lifecycle") if code == "LIFECYCLE" else _("Archive-keep lifecycle"),
			"Passed" if rule else "Warning",
			_("Configured in OSS: {0}").format(_exact_rule_summary(rule))
			if rule else _("Configure the OSS tag-filtered rule: {0}").format(plan["rule_id"]),
			plan["tag_value"], _exact_rule_summary(rule),
		))
	if matched["archive-delete"] and matched["archive-keep"] and (
		_transition_pairs(matched["archive-delete"]) != _transition_pairs(matched["archive-keep"])
	):
		checks.append(_check(
			"LIFECYCLE_TRANSITIONS", _("Archive lifecycle consistency"), "Warning",
			_("The two archive rules must have the same IA and Archive transitions."),
		))
	dangerous = [r.id or _("Unnamed rule") for r in rules if _is_dangerous_rule(r, plans[0]["prefix"])]
	for policy in ("archive-delete", "archive-keep"):
		duplicates = [
			r for r in rules
			if r.status == "Enabled" and (r.prefix or "") == plans[0]["prefix"]
			and {t.key: t.value for t in (r.tags or [])} == {"retention": policy}
		]
		if len(duplicates) > 1:
			dangerous.extend(r.id or _("Unnamed rule") for r in duplicates)
	if dangerous:
		checks.append(_check(
			"LIFECYCLE_SCOPE", _("Lifecycle rule scope"), "Warning",
			_("Other lifecycle rules can affect protected attachments: {0}").format(", ".join(dangerous)),
		))
	return checks


def _transition_pairs(rule) -> list[tuple[int, str]]:
	return sorted(
		(int(item.days or 0), _storage_class_value(item.storage_class))
		for item in (rule.transitions or [])
	)


def _rule_matches(rule, plan: dict) -> bool:
	if rule.status != "Enabled" or (rule.prefix or "") != plan["prefix"]:
		return False
	if {t.key: t.value for t in (rule.tags or [])} != {"retention": plan["tag_value"]}:
		return False
	transitions = _transition_pairs(rule)
	if not (
		len(transitions) == 2
		and transitions[0][1] == "IA"
		and transitions[1][1] == "Archive"
		and 0 < transitions[0][0] < transitions[1][0]
	):
		return False
	if plan["tag_value"] == "archive-keep":
		return rule.expiration is None
	expiration = getattr(rule.expiration, "days", None) if rule.expiration else None
	return bool(expiration and int(expiration) > transitions[-1][0])


def _is_dangerous_rule(rule, target_prefix: str) -> bool:
	if rule.status != "Enabled" or not _prefixes_overlap(rule.prefix or "", target_prefix):
		return False
	tags = {t.key: t.value for t in (rule.tags or [])}
	if tags == {"retention": "archive-keep"}:
		return not _rule_matches(rule, {"prefix": target_prefix, "tag_value": "archive-keep"})
	if tags == {"retention": "archive-delete"}:
		return not _rule_matches(rule, {"prefix": target_prefix, "tag_value": "archive-delete"})
	return bool(rule.transitions or rule.expiration)


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
