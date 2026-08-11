import io
import json
import secrets

import frappe
from frappe import _
from frappe.utils import now_datetime

from erpnext_s3_integration.object_storage.errors import classify_storage_error
from erpnext_s3_integration.object_storage.service import ObjectStorageService


@frappe.whitelist(methods=["POST"])
def run_full_test(profile_name: str) -> dict:
	frappe.only_for("System Manager")
	profile = frappe.get_doc("Object Storage Profile", profile_name)
	settings = frappe.get_single("Object Storage Settings")
	capabilities = _capabilities(profile, settings)
	service = ObjectStorageService(profile_name, require_enabled=False)
	key = service.key(f".erpnext-storage-test/{frappe.generate_hash(length=24)}.txt")
	payload = f"ERPNext object storage test {secrets.token_hex(16)}".encode()
	steps = []

	try:
		steps.append(_passed("Credential and client initialization"))
		service.backend.put(
			key,
			io.BytesIO(payload),
			content_type="text/plain; charset=utf-8",
			content_length=len(payload),
			metadata={"test-purpose": "erpnext-object-storage"},
		)
		steps.append(_passed("Put object"))

		info = service.backend.head(key)
		if not info or info.size != len(payload) or not (info.content_type or "").startswith("text/plain"):
			raise ValueError("Head object did not return the expected size and content type")
		steps.append(_passed("Head object and verify metadata"))

		stream = service.backend.get(key)
		try:
			if stream.read() != payload:
				raise ValueError("Downloaded content does not match uploaded content")
		finally:
			stream.close()
		steps.append(_passed("Get object and compare content"))

		partial = service.backend.get(key, (0, min(7, len(payload) - 1)))
		try:
			if partial.read() != payload[:8]:
				raise ValueError("Range download did not return the expected bytes")
		finally:
			partial.close()
		steps.append(_passed("Range download for HTTP 206 support"))

		tags = {"application": "erpnext", "environment": profile.environment.lower(), "test": "true"}
		if capabilities["tags"]:
			service.backend.put_tags(key, tags)
			if service.backend.get_tags(key) != tags:
				raise ValueError("Object tags read back with different values")
			steps.append(_passed("Put and get object tags"))
		else:
			steps.append(_skipped("Put and get object tags", "Automatic tagging is disabled"))

		if capabilities["list"]:
			if not any(item.key == key for item in service.backend.list(service.key(".erpnext-storage-test/"))):
				raise ValueError("List did not return the test object within the configured prefix")
			steps.append(_passed("List objects within the backup prefix"))
		else:
			steps.append(_skipped("List objects", "Attachment runtime does not require ListObjects"))

		if capabilities["delete"]:
			service.backend.delete(key)
			if service.backend.exists(key):
				raise ValueError("Object still exists after delete")
			steps.append(_passed("Delete object and confirm absence"))
		else:
			steps.append(_skipped("Delete object", "Remote deletion is disabled; remove the test object manually"))
		steps.append(_skipped("Restore object", "Use Restore Drill with an archived non-sensitive object"))
		_set_result(profile, "Passed", steps, settings)
		return {"status": "Passed", "steps": steps, "message": _("Full Read/Write Test passed.")}
	except Exception as exc:
		_try_cleanup(service, key)
		details = classify_storage_error(exc)
		steps.append(
			{
				"status": "Failed",
				"title": _("Test stopped"),
				"detail": str(exc),
				"category": details.category,
				"retryable": details.retryable,
			}
		)
		_set_result(profile, "Failed", steps, settings)
		frappe.log_error(title="Object Storage Full Test", message=frappe.get_traceback())
		return {"status": "Failed", "steps": steps, "message": _diagnosis(details.category)}


def _passed(title: str) -> dict:
	return {"status": "Passed", "title": _(title)}


def _skipped(title: str, detail: str) -> dict:
	return {"status": "Skipped", "title": _(title), "detail": _(detail)}


def _set_result(profile, status: str, steps: list[dict], settings=None) -> None:
	values = {
		"last_test_status": status,
		"last_tested_at": now_datetime(),
		"last_test_fingerprint": profile.config_fingerprint(settings),
		"last_test_details": json.dumps(steps, ensure_ascii=False, indent=2),
	}
	if status == "Failed":
		values["enabled"] = 0
	profile.db_set(values, update_modified=True)


def _try_cleanup(service: ObjectStorageService, key: str) -> None:
	try:
		service.backend.delete(key)
	except Exception:
		pass


def _diagnosis(category: str) -> str:
	messages = {
		"Authorization": _("Check the ECS RAM Role binding, RAM policy resources, and bucket permissions."),
		"Not Found": _("Check the bucket name, region, and endpoint."),
		"Network": _("Check ECS VPC connectivity and the Jakarta internal endpoint."),
		"Provider Temporary Error": _(
			"The provider returned a temporary error. Retry after checking OSS service health."
		),
	}
	return messages.get(category, _("Review the failed step and server error log, then retry."))


def _capabilities(profile, settings) -> dict[str, bool]:
	if profile.purpose == "Attachments":
		return {
			"tags": bool(settings.enable_auto_classification),
			"delete": bool(settings.delete_on_last_reference),
			"list": False,
		}
	return {
		"tags": True,
		"delete": bool(settings.backup_retention_days),
		"list": True,
	}
