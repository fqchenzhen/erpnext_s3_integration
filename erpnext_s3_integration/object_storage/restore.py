from datetime import datetime

import frappe
from frappe import _
from frappe.utils import add_days, now_datetime, time_diff_in_seconds

from erpnext_s3_integration.object_storage.errors import ObjectNotFoundError, classify_storage_error
from erpnext_s3_integration.object_storage.service import ObjectStorageService
from erpnext_s3_integration.object_storage.types import restore_expiry_from_header

ONLINE_CLASSES = {None, "", "STANDARD", "Standard", "IA", "STANDARD_IA", "OSS_IA"}


def process_restore_requests() -> None:
	if not frappe.db.get_single_value("Object Storage Settings", "enable_archive_restore"):
		return
	settings = frappe.get_single("Object Storage Settings")
	requests = frappe.get_all(
		"Object Restore Request",
		filters={"status": ["in", ["Requested", "Restoring", "Ready"]]},
		fields=["name", "last_checked_at"],
		limit_page_length=100,
	)
	interval_seconds = int(settings.restore_check_interval_minutes) * 60
	for request in requests:
		if (
			request.last_checked_at
			and time_diff_in_seconds(now_datetime(), request.last_checked_at) < interval_seconds
		):
			continue
		process_restore_request(request.name)



def process_restore_request(name: str, force: bool = False) -> None:
	_lock_request(name)
	doc = frappe.get_doc("Object Restore Request", name)
	if doc.status not in {"Requested", "Restoring", "Ready"}:
		return
	if not force and _checked_too_recent(doc):
		return
	doc.flags.restore_worker = True
	try:
		service = ObjectStorageService(doc.object_storage_profile)
		info = service.backend.head(doc.object_key)
		if not info:
			raise ObjectNotFoundError("The attachment object no longer exists.", operation="head")

		doc.last_checked_at = now_datetime()
		doc.storage_class = info.storage_class
		if doc.status == "Requested":
			_submit(doc, service, info)
		elif doc.status == "Restoring":
			_update_restoring(doc, info)
		elif doc.status == "Ready":
			provider_copy_expired = info.storage_class not in ONLINE_CLASSES and not (
				info.restore_status and 'ongoing-request="false"' in info.restore_status
			)
			if provider_copy_expired or (doc.expires_at and now_datetime() >= doc.expires_at):
				doc.status = "Expired"
		doc.save(ignore_permissions=True)
	except Exception as exc:
		_fail(doc, exc)


def _process_one(name: str) -> None:
	process_restore_request(name, force=True)


def _submit(doc, service: ObjectStorageService, info) -> None:
	if info.storage_class in ONLINE_CLASSES or (
		info.restore_status and 'ongoing-request="false"' in info.restore_status
	):
		doc.status = "Restoring"
		return
	settings = frappe.get_single("Object Storage Settings")
	if doc.attempts >= settings.restore_retry_limit:
		raise RuntimeError("Restore retry limit reached")
	try:
		service.backend.restore(doc.object_key, doc.restore_days, doc.restore_tier)
	except Exception as exc:
		if classify_storage_error(exc).code != "RestoreAlreadyInProgress":
			raise
	doc.provider_submitted = 1
	doc.attempts += 1
	doc.submitted_at = now_datetime()
	doc.status = "Restoring"


def _update_restoring(doc, info) -> None:
	restore_status = info.restore_status
	if info.storage_class in ONLINE_CLASSES:
		doc.status = "Ready"
		doc.ready_at = now_datetime()
		doc.expires_at = add_days(doc.ready_at, doc.restore_days)
		return
	if not restore_status or 'ongoing-request="true"' in restore_status:
		return
	if 'ongoing-request="false"' in restore_status:
		doc.status = "Ready"
		doc.ready_at = now_datetime()
		doc.expires_at = info.restore_expiry or _expiry_from_restore_header(restore_status) or add_days(
			doc.ready_at, doc.restore_days
		)


def _expiry_from_restore_header(value: str) -> datetime | None:
	return restore_expiry_from_header(value)


def _fail(doc, exc: Exception) -> None:
	if doc.status == "Ready":
		frappe.log_error(title="Object Restore Request Check", message=frappe.get_traceback())
		return
	details = classify_storage_error(exc)
	doc.flags.restore_worker = True
	doc.status = "Failed"
	doc.error_category = details.category
	doc.error_code = details.code
	doc.error_message = str(exc)[:500]
	doc.last_checked_at = now_datetime()
	doc.save(ignore_permissions=True)
	frappe.log_error(title="Object Restore Request", message=frappe.get_traceback())


def notify_status_change(doc) -> None:
	if doc.last_notified_status == doc.status:
		return
	settings = frappe.get_single("Object Storage Settings")
	if doc.status == "Expired":
		frappe.db.set_value(doc.doctype, doc.name, "last_notified_status", doc.status, update_modified=False)
		return
	message = _("Restore request {0} changed to {1}.").format(doc.name, doc.status)
	try:
		if settings.system_notifications:
			from frappe.desk.doctype.notification_log.notification_log import enqueue_create_notification

			enqueue_create_notification(
				users=[doc.requested_by],
				doc={
					"type": "Alert",
					"subject": message,
					"document_type": doc.doctype,
					"document_name": doc.name,
				},
			)
		if settings.email_notifications:
			frappe.sendmail(recipients=[doc.requested_by], subject=message, message=message)
		frappe.db.set_value(doc.doctype, doc.name, "last_notified_status", doc.status, update_modified=False)
	except Exception:
		frappe.log_error(title="Object Restore Notification", message=frappe.get_traceback())


def _lock_request(name: str) -> None:
	frappe.qb.get_query(
		"Object Restore Request", fields=["name"], filters={"name": name}, for_update=True
	).run()


def _checked_too_recent(doc) -> bool:
	if not doc.last_checked_at:
		return False
	interval = int(frappe.db.get_single_value("Object Storage Settings", "restore_check_interval_minutes"))
	return time_diff_in_seconds(now_datetime(), doc.last_checked_at) < interval * 60
