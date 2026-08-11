import frappe
from frappe import _
from frappe.model.document import Document
from frappe.utils import now_datetime

ACTIVE_STATUSES = {"Requested", "Restoring", "Ready"}
ARCHIVE_CLASSES = {"Archive", "ColdArchive", "DeepColdArchive", "GLACIER", "DEEP_ARCHIVE"}
TRANSITIONS = {
	"Requested": {"Restoring", "Failed", "Cancelled"},
	"Restoring": {"Ready", "Failed"},
	"Ready": {"Expired"},
	"Failed": {"Requested"},
	"Expired": set(),
	"Cancelled": set(),
}


class ObjectRestoreRequest(Document):
	def before_insert(self):
		self._initialize_request()

	def validate(self):
		self._validate_transition()
		self._validate_active_request()

	def on_update(self):
		previous = self.get_doc_before_save()
		if previous and previous.status != self.status:
			from erpnext_s3_integration.object_storage.restore import notify_status_change

			notify_status_change(self)

	def after_insert(self):
		from erpnext_s3_integration.object_storage.restore import notify_status_change

		notify_status_change(self)

	def _initialize_request(self):
		self.flags.ignore_permlevel_for_fields = ["bucket", "object_key"]
		settings = frappe.get_single("Object Storage Settings")
		if not settings.enable_archive_restore:
			frappe.throw(_("Archive restore requests are disabled."))
		if not can_request_restore(frappe.session.user, settings):
			frappe.throw(
				_("You do not have a role allowed to request archive restores."), frappe.PermissionError
			)

		file_doc = frappe.get_doc("File", self.file)
		if not frappe.has_permission("File", "read", doc=file_doc, user=frappe.session.user):
			frappe.throw(_("You do not have permission to read this File."), frappe.PermissionError)
		if not file_doc.get("object_storage_profile") or not file_doc.get("object_storage_key"):
			frappe.throw(_("This File is not stored in configured object storage."))

		self.status = "Requested"
		profile = frappe.get_doc("Object Storage Profile", file_doc.object_storage_profile)
		from erpnext_s3_integration.object_storage.service import ObjectStorageService

		info = ObjectStorageService(profile.name).backend.head(file_doc.object_storage_key)
		if not info:
			frappe.throw(_("The attachment object no longer exists."))
		if info.storage_class not in ARCHIVE_CLASSES or (
			info.restore_status and 'ongoing-request="false"' in info.restore_status
		):
			frappe.throw(_("This attachment is currently available and does not need a restore request."))
		self.requested_by = frappe.session.user
		self.requested_at = now_datetime()
		self.file_name_snapshot = file_doc.file_name
		self.object_storage_profile = file_doc.object_storage_profile
		self.object_key = file_doc.object_storage_key
		self.provider = profile.provider
		self.bucket = profile.bucket
		self.storage_class = info.storage_class
		self.active_object_identity = _object_identity(self.object_storage_profile, self.object_key)
		self.restore_tier = settings.restore_tier
		self.restore_days = settings.restore_days
		self.provider_submitted = 0
		self.attempts = 0

	def _validate_transition(self):
		previous = self.get_doc_before_save()
		if not previous:
			return
		immutable_fields = (
			"file",
			"file_name_snapshot",
			"requested_by",
			"object_storage_profile",
			"provider",
			"bucket",
			"object_key",
			"restore_tier",
			"restore_days",
			"storage_class",
			"provider_submitted",
			"attempts",
			"submitted_at",
			"ready_at",
			"expires_at",
			"last_checked_at",
			"error_category",
			"error_code",
			"error_message",
		)
		if not self.flags.get("restore_worker") and any(
			self.get(field) != previous.get(field) for field in immutable_fields
		):
			frappe.throw(
				_("Restore request object and provider fields cannot be changed."), frappe.PermissionError
			)
		if previous.status == self.status:
			return
		if self.status not in TRANSITIONS.get(previous.status, set()):
			frappe.throw(
				_("Invalid restore transition from {0} to {1}.").format(previous.status, self.status)
			)
		if previous.status == "Requested" and self.status == "Cancelled" and previous.provider_submitted:
			frappe.throw(_("A restore cannot be cancelled after provider submission."))
		if not self.flags.get("restore_worker") and self.status not in {"Requested", "Cancelled"}:
			frappe.throw(
				_("Only the restore worker may perform this status transition."), frappe.PermissionError
			)

	def _validate_active_request(self):
		if self.status in ACTIVE_STATUSES:
			self.active_object_identity = _object_identity(self.object_storage_profile, self.object_key)
			if frappe.db.exists(
				"Object Restore Request",
				{
					"active_object_identity": self.active_object_identity,
					"name": ["!=", self.name or ""],
				},
			):
				frappe.throw(_("An active restore request already exists for this object."))
		else:
			self.active_object_identity = None


def _object_identity(profile: str, key: str) -> str:
	import hashlib

	return hashlib.sha256(f"{profile}\x1f{key}".encode()).hexdigest()


def can_request_restore(user: str, settings=None) -> bool:
	if "System Manager" in frappe.get_roles(user):
		return True
	settings = settings or frappe.get_single("Object Storage Settings")
	if not settings.allow_self_service_restore:
		return False
	allowed_roles = {row.role for row in settings.restore_roles}
	return bool(allowed_roles.intersection(frappe.get_roles(user)))


def get_permission_query_conditions(user: str | None = None) -> str:
	user = user or frappe.session.user
	if "System Manager" in frappe.get_roles(user):
		return ""
	return "`tabObject Restore Request`.`requested_by` = {0}".format(frappe.db.escape(user))


def has_permission(doc, user: str | None = None, permission_type: str | None = None) -> bool:
	user = user or frappe.session.user
	if "System Manager" in frappe.get_roles(user):
		return True
	if doc.requested_by and doc.requested_by != user:
		return False
	if not can_request_restore(user):
		return False
	if not doc.file:
		return False
	try:
		file_doc = frappe.get_doc("File", doc.file)
	except frappe.DoesNotExistError:
		return False
	return bool(frappe.has_permission("File", "read", doc=file_doc, user=user))


@frappe.whitelist(methods=["POST"])
def create_restore_request(file_name: str) -> str:
	doc = frappe.get_doc({"doctype": "Object Restore Request", "file": file_name})
	doc.insert()
	return doc.name


@frappe.whitelist(methods=["POST"])
def retry_restore_request(request_name: str) -> None:
	frappe.only_for("System Manager")
	doc = frappe.get_doc("Object Restore Request", request_name)
	doc.check_permission("write")
	if doc.status != "Failed":
		frappe.throw(_("Only a Failed restore request can be retried."))
	settings = frappe.get_single("Object Storage Settings")
	if doc.attempts >= settings.restore_retry_limit:
		frappe.throw(_("This restore request has reached the retry limit."))
	doc.flags.restore_worker = True
	doc.status = "Requested"
	doc.error_category = None
	doc.error_code = None
	doc.error_message = None
	doc.save()


@frappe.whitelist(methods=["POST"])
def cancel_restore_request(request_name: str) -> None:
	doc = frappe.get_doc("Object Restore Request", request_name)
	doc.check_permission("write")
	doc.status = "Cancelled"
	doc.save()


@frappe.whitelist(methods=["POST"])
def refresh_restore_request(request_name: str) -> None:
	frappe.only_for("System Manager")
	from erpnext_s3_integration.object_storage.restore import process_restore_request

	process_restore_request(request_name, force=True)
