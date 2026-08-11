import mimetypes
import re
from dataclasses import dataclass

import frappe
from frappe.utils import now_datetime

from erpnext_s3_integration.object_storage.service import ObjectStorageService

RETENTION_RANK = {
	"unclassified": 0,
	"business-archive": 1,
	"business-online": 2,
	"permanent-hot": 3,
}
SCOPE_RANK = {"DocType and Field": 0, "DocType": 1, "MIME Type": 2, "File Extension": 3}


@dataclass(frozen=True)
class Classification:
	policy: str
	category: str
	source: str


def enqueue_file_classification(file_doc, method=None) -> None:
	if not file_doc.get("object_storage_key"):
		return
	if not frappe.db.get_single_value(
		"Object Storage Settings", "enable_auto_classification"
	) and not file_doc.get("retention_override"):
		return
	frappe.db.set_value("File", file_doc.name, "object_tag_status", "Pending", update_modified=False)
	frappe.enqueue(
		"erpnext_s3_integration.object_storage.classification.tag_file_object",
		queue="short",
		enqueue_after_commit=True,
		deduplicate=True,
		job_id=f"object-tags-{file_doc.name}",
		file_name=file_doc.name,
	)


def enqueue_shared_object_reclassification(file_doc, method=None) -> None:
	if not file_doc.get("object_storage_key"):
		return
	frappe.enqueue(
		"erpnext_s3_integration.object_storage.classification.tag_shared_object",
		queue="short",
		enqueue_after_commit=True,
		deduplicate=True,
		job_id=f"object-retag-{frappe.generate_hash(file_doc.object_storage_key, 16)}",
		profile_name=file_doc.object_storage_profile,
		object_key=file_doc.object_storage_key,
	)


def tag_shared_object(profile_name: str, object_key: str) -> None:
	file_name = frappe.db.get_value(
		"File",
		{"object_storage_profile": profile_name, "object_storage_key": object_key},
		"name",
	)
	if file_name:
		tag_file_object(file_name)


def set_retention_override_audit(file_doc, method=None) -> None:
	if not file_doc.has_value_changed("retention_override"):
		return
	settings = frappe.get_single("Object Storage Settings")
	roles = set(frappe.get_roles())
	if not {"System Manager", "Object Storage Manager"}.intersection(roles):
		frappe.throw("Only an Object Storage Manager may change Retention Override.", frappe.PermissionError)
	if file_doc.retention_override and not settings.allow_manual_retention_override:
		frappe.throw("Manual Retention Override is disabled in Object Storage Settings.")
	if file_doc.retention_override:
		file_doc.retention_override_by = frappe.session.user
		file_doc.retention_override_at = now_datetime()
	else:
		file_doc.retention_override_by = None
		file_doc.retention_override_at = None


def tag_file_object(file_name: str, attempt: int = 0) -> None:
	file_doc = frappe.get_doc("File", file_name)
	if not file_doc.object_storage_key:
		return
	refs = frappe.get_all(
		"File",
		filters={
			"object_storage_profile": file_doc.object_storage_profile,
			"object_storage_key": file_doc.object_storage_key,
		},
		fields=["name", "file_name", "attached_to_doctype", "attached_to_field", "retention_override"],
	)
	settings = frappe.get_single("Object Storage Settings")
	profile = frappe.get_doc("Object Storage Profile", file_doc.object_storage_profile)
	default_policy = settings.default_retention_policy or "unclassified"
	classifications = [classify_file(ref, settings.retention_rules, default_policy) for ref in refs]
	effective = choose_hottest(classifications)
	tags = {
		"retention": effective.policy,
		"category": _safe_tag_value(effective.category),
		"application": "erpnext",
		"environment": _safe_tag_value(profile.environment.lower()),
		"site": _safe_tag_value(frappe.local.site),
	}
	try:
		ObjectStorageService(file_doc.object_storage_profile).backend.put_tags(
			file_doc.object_storage_key, tags
		)
		for ref in refs:
			classification = classify_file(ref, settings.retention_rules, default_policy)
			frappe.db.set_value(
				"File",
				ref.name,
				{
					"effective_retention_policy": effective.policy,
					"retention_category": classification.category,
					"retention_source": classification.source,
					"object_tag_status": "Passed",
					"object_tag_error": None,
					"object_tagged_at": now_datetime(),
				},
				update_modified=False,
			)
	except Exception as exc:
		for ref in refs:
			frappe.db.set_value(
				"File",
				ref.name,
				{"object_tag_status": "Failed", "object_tag_error": str(exc)[:500]},
				update_modified=False,
			)
		if attempt < 2:
			frappe.enqueue(
				"erpnext_s3_integration.object_storage.classification.tag_file_object",
				queue="short",
				file_name=file_name,
				attempt=attempt + 1,
			)
		else:
			frappe.log_error(title="Object Storage Tagging", message=frappe.get_traceback())


def classify_file(file_doc, rules, default_policy: str = "unclassified") -> Classification:
	if file_doc.get("retention_override"):
		return Classification(file_doc.retention_override, "manual", "Manual File Override")

	for rule in sorted(
		(rule for rule in rules if rule.enabled),
		key=lambda rule: (SCOPE_RANK.get(rule.scope, 99), rule.priority),
	):
		if _matches(file_doc, rule):
			return Classification(rule.retention_policy, rule.category or "general", rule.scope)
	return Classification(default_policy or "unclassified", "unclassified", "Default")


def get_default_retention_policy(settings, purpose: str | None = None) -> str:
	return settings.get("default_retention_policy") or "unclassified"


def choose_hottest(classifications: list[Classification]) -> Classification:
	return max(classifications, key=lambda item: RETENTION_RANK[item.policy])


def _matches(file_doc, rule) -> bool:
	if rule.scope == "DocType and Field":
		return (
			file_doc.attached_to_doctype == rule.reference_doctype
			and file_doc.attached_to_field == rule.reference_field
		)
	if rule.scope == "DocType":
		return file_doc.attached_to_doctype == rule.reference_doctype
	mime_type = mimetypes.guess_type(file_doc.file_name or "")[0] or "application/octet-stream"
	if rule.scope == "MIME Type":
		pattern = re.escape((rule.mime_type or "").lower()).replace(r"\*", ".*")
		return bool(re.fullmatch(pattern, mime_type.lower()))
	if rule.scope == "File Extension":
		extension = (file_doc.file_name or "").rsplit(".", 1)[-1].lower()
		return extension == (rule.file_extension or "").lower().lstrip(".")
	return False


def _safe_tag_value(value: str) -> str:
	value = re.sub(r"[^A-Za-z0-9_.:/=+\-@]", "_", value or "")
	return value[:128] or "unclassified"


@frappe.whitelist(methods=["GET"])
def preview_classification(file_name: str) -> dict:
	_require_storage_manager()
	file_doc = frappe.get_doc("File", file_name)
	file_doc.check_permission("read")
	settings = frappe.get_single("Object Storage Settings")
	classification = classify_file(file_doc, settings.retention_rules, "unclassified")
	shared = _shared_classifications(file_doc, settings)
	effective = choose_hottest(shared)
	return {
		"file": _classification_dict(classification),
		"object": _classification_dict(effective),
		"shared_references": len(shared),
	}


@frappe.whitelist(methods=["POST"])
def enqueue_recalculate_classification() -> dict:
	_require_storage_manager()
	count = frappe.db.count("File", {"object_storage_key": ["is", "set"]})
	frappe.enqueue(
		"erpnext_s3_integration.object_storage.classification.recalculate_classification",
		queue="long",
		timeout=1500,
		deduplicate=True,
		job_id=f"object-storage-recalculate-{frappe.local.site}",
	)
	return {"queued": count}


def recalculate_classification(start_after: str | None = None, batch_size: int = 250) -> None:
	filters = {"object_storage_key": ["is", "set"]}
	if start_after:
		filters["name"] = [">", start_after]
	files = frappe.get_all(
		"File", filters=filters, fields=["name"], order_by="name asc", limit_page_length=batch_size
	)
	for file_doc in files:
		tag_file_object(file_doc.name)
	if len(files) == batch_size:
		frappe.enqueue(
			"erpnext_s3_integration.object_storage.classification.recalculate_classification",
			queue="long",
			timeout=1500,
			enqueue_after_commit=True,
			start_after=files[-1].name,
			batch_size=batch_size,
		)


def _shared_classifications(file_doc, settings) -> list[Classification]:
	if not file_doc.object_storage_key:
		return [classify_file(file_doc, settings.retention_rules, "unclassified")]
	refs = frappe.get_all(
		"File",
		filters={
			"object_storage_profile": file_doc.object_storage_profile,
			"object_storage_key": file_doc.object_storage_key,
		},
		fields=["file_name", "attached_to_doctype", "attached_to_field", "retention_override"],
	)
	return [classify_file(ref, settings.retention_rules, "unclassified") for ref in refs]


def _classification_dict(classification: Classification) -> dict:
	return {
		"policy": classification.policy,
		"category": classification.category,
		"source": classification.source,
	}


def _require_storage_manager() -> None:
	if not {"System Manager", "Object Storage Manager"}.intersection(frappe.get_roles()):
		frappe.throw("Object Storage Manager role is required.", frappe.PermissionError)
