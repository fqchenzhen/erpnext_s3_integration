import datetime
import mimetypes
import os
import re
import shutil
import time
from zoneinfo import ZoneInfo

import frappe
from frappe import _

from erpnext_s3_integration.object_storage.errors import classify_storage_error
from erpnext_s3_integration.object_storage.service import ObjectStorageService


def _safe_tag(value: str) -> str:
	return re.sub(r"[^A-Za-z0-9_.:/=+\-@]", "_", value or "")[:128] or "unknown"


def _is_database_backup(filename: str) -> bool:
	return "-database" in filename and filename.endswith(".sql.gz")


def _is_public_files_backup(filename: str) -> bool:
	return bool(re.search(r"-files(-enc)?\.(tar|tgz)$", filename)) and "-private-files" not in filename


def _is_private_files_backup(filename: str) -> bool:
	return bool(re.search(r"-private-files(-enc)?\.(tar|tgz)$", filename))


def _backup_enabled(file_path: str, settings) -> bool:
	filename = os.path.basename(file_path)
	return bool(
		(_is_database_backup(filename) and settings.upload_database_backup)
		or (_is_public_files_backup(filename) and settings.upload_public_files_backup)
		or (_is_private_files_backup(filename) and settings.upload_private_files_backup)
	)


def _backup_date(file_path: str) -> str:
	try:
		timezone = ZoneInfo(frappe.utils.get_system_timezone())
		return datetime.datetime.fromtimestamp(os.path.getmtime(file_path), tz=timezone).strftime("%Y-%m-%d")
	except OSError:
		return frappe.utils.now_datetime().strftime("%Y-%m-%d")


def backup_object_key(service: ObjectStorageService, site_name: str, file_path: str) -> str:
	relative = f"backups/{site_name}/{_backup_date(file_path)}/{os.path.basename(file_path)}"
	return service.key(relative)


def _paths_from_result(result: dict | None, settings) -> list[str]:
	if not result:
		return []
	paths = []
	for key in ("backup_path_db", "backup_path_files", "backup_path_private_files"):
		path = result.get(key)
		if path and _backup_enabled(path, settings):
			paths.append(os.path.abspath(path))
	return list(dict.fromkeys(paths))


def _today_candidates(settings) -> list[str]:
	from frappe.utils.backups import get_backup_path

	backup_path = get_backup_path()
	today = frappe.utils.now_datetime().strftime("%Y%m%d")
	return [
		os.path.join(backup_path, filename)
		for filename in os.listdir(backup_path)
		if filename.startswith(today) and _backup_enabled(filename, settings)
	]


def sync_backup_files(backup_files: dict | None = None) -> bool:
	settings = frappe.get_single("Object Storage Settings")
	if not settings.enable_backup_storage:
		return False
	paths = _paths_from_result(backup_files, settings) or _today_candidates(settings)
	if not paths:
		log_storage_sync("Failed", _("No eligible backup files were found."), operation="backup_sync")
		return False

	service = ObjectStorageService(settings.backup_storage_profile)
	results = [_upload_backup(service, settings, path) for path in paths]
	success = all(results)
	if success:
		cleanup_old_backups(
			service, service.key(f"backups/{frappe.local.site}/"), settings.backup_retention_days
		)
	log_storage_sync(
		"Success" if success else "Failed",
		_("Backup object storage sync completed for {0} file(s).").format(len(paths)),
		operation="backup_sync",
	)
	return success


def _upload_backup(service: ObjectStorageService, settings, file_path: str) -> bool:
	started = time.monotonic()
	key = backup_object_key(service, frappe.local.site, file_path)
	try:
		size = os.path.getsize(file_path)
		with open(file_path, "rb") as fileobj:  # nosemgrep
			service.backend.put(
				key,
				fileobj,
				content_type=mimetypes.guess_type(file_path)[0] or "application/octet-stream",
				content_length=size,
				metadata={"backup-file": os.path.basename(file_path)},
				tags={
					"retention": "business-archive",
					"category": "backup",
					"application": "erpnext",
					"environment": _safe_tag(service.profile.environment.lower()),
					"site": _safe_tag(frappe.local.site),
				},
			)
		log_storage_sync(
			"Success",
			_("Uploaded backup {0}.").format(os.path.basename(file_path)),
			operation="backup_upload",
			source_path=file_path,
			s3_key=key,
			file_size=size,
			duration_ms=int((time.monotonic() - started) * 1000),
		)
		if settings.delete_local_backup_after_upload:
			os.remove(file_path)
		return True
	except Exception as exc:
		details = classify_storage_error(exc)
		log_storage_sync(
			"Failed",
			_("Backup upload failed for {0}: {1}").format(os.path.basename(file_path), exc),
			operation="backup_upload",
			source_path=file_path,
			s3_key=key,
			duration_ms=int((time.monotonic() - started) * 1000),
			error_category=details.category,
			error_code=details.code,
			retryable=details.retryable,
		)
		frappe.log_error(title="Object Storage Backup", message=frappe.get_traceback())
		return False


def cleanup_old_backups(service: ObjectStorageService, prefix: str, restore_points: int) -> int:
	if not restore_points:
		return 0
	objects = list(service.backend.list(prefix))
	by_date: dict[str, list] = {}
	for item in objects:
		backup_date = _date_from_backup_object(item)
		if backup_date:
			by_date.setdefault(backup_date, []).append(item)
	retained_dates = set(sorted(by_date, reverse=True)[:restore_points])
	deleted = 0
	for backup_date, dated_objects in by_date.items():
		if backup_date not in retained_dates:
			for item in dated_objects:
				service.backend.delete(item.key)
				deleted += 1
			continue
		for duplicates in _duplicate_objects_by_kind(dated_objects):
			for item in duplicates:
				service.backend.delete(item.key)
				deleted += 1
	return deleted


def _date_from_backup_object(item) -> str | None:
	match = re.search(r"/(\d{4}-\d{2}-\d{2})/", item.key)
	if match:
		return match.group(1)
	if item.last_modified:
		value = item.last_modified
		if value.tzinfo is None:
			value = value.replace(tzinfo=datetime.UTC)
		return value.astimezone(datetime.UTC).strftime("%Y-%m-%d")
	return None


def _duplicate_objects_by_kind(items) -> list[list]:
	by_kind: dict[str, list] = {}
	for item in items:
		by_kind.setdefault(_backup_kind(item.key), []).append(item)
	duplicates = []
	for kind_items in by_kind.values():
		ordered = sorted(kind_items, key=_backup_object_sort_key, reverse=True)
		duplicates.append(ordered[1:])
	return duplicates


def _backup_kind(key: str) -> str:
	filename = os.path.basename(key)
	if _is_database_backup(filename):
		return "database"
	if _is_private_files_backup(filename):
		return "private-files"
	if _is_public_files_backup(filename):
		return "public-files"
	return filename


def _backup_object_sort_key(item) -> tuple:
	modified = item.last_modified or datetime.datetime.min.replace(tzinfo=datetime.UTC)
	if modified.tzinfo is None:
		modified = modified.replace(tzinfo=datetime.UTC)
	return modified.astimezone(datetime.UTC), item.key


def log_storage_sync(status: str, message: str, **details) -> None:
	if not frappe.db.exists("DocType", "S3 Sync Log"):
		return
	doc = frappe.new_doc("S3 Sync Log")
	doc.status = status
	doc.message = message
	for fieldname, value in details.items():
		if doc.meta.has_field(fieldname):
			doc.set(fieldname, value)
	doc.insert(ignore_permissions=True)


def run_backup_and_sync(update_last_sync: bool = True) -> bool:
	settings = frappe.get_single("Object Storage Settings")
	if not settings.enable_backup_storage:
		return False
	from frappe.utils.backups import backup
	from frappe.utils.synchronization import filelock

	with filelock("object_storage_backup_sync", timeout=1):
		include_files = bool(settings.upload_public_files_backup or settings.upload_private_files_backup)
		started = time.monotonic()
		try:
			result = backup(with_files=include_files)
			database_path = (result or {}).get("backup_path_db")
			log_storage_sync(
				"Success",
				_("Database backup file created."),
				operation="backup_create",
				source_path=database_path,
				file_size=os.path.getsize(database_path) if database_path else 0,
				duration_ms=int((time.monotonic() - started) * 1000),
			)
		except Exception:
			log_storage_sync(
				"Failed",
				_("Database backup file creation failed."),
				operation="backup_create",
				duration_ms=int((time.monotonic() - started) * 1000),
			)
			raise
		success = sync_backup_files(result)
		if success and update_last_sync:
			frappe.db.set_single_value(
				"Object Storage Settings", "last_backup_sync", frappe.utils.now_datetime()
			)
		return success


@frappe.whitelist(methods=["POST"])
def run_backup_now() -> bool:
	frappe.only_for("System Manager")
	return run_backup_and_sync()


@frappe.whitelist(methods=["GET"])
def get_backup_capacity_summary() -> dict:
	frappe.only_for("System Manager")
	settings = frappe.get_single("Object Storage Settings")
	if not frappe.db.exists("DocType", "S3 Sync Log"):
		return _empty_capacity_summary(_("No backup history is available yet."))
	uploads = frappe.get_all(
		"S3 Sync Log",
		filters={"operation": "backup_upload", "status": "Success"},
		fields=["creation", "file_size", "source_path"],
		order_by="creation desc",
		limit=90,
	)
	database_uploads = [
		row for row in uploads if _is_database_backup(os.path.basename(row.source_path or ""))
	]
	if not database_uploads:
		return _empty_capacity_summary(_("Run the first database backup to collect capacity data."))

	latest = database_uploads[0]
	creation = frappe.utils.get_datetime(latest.creation)
	latest_size = int(latest.file_size or 0)
	retention = max(int(settings.backup_retention_days or 0), 1)
	create_log = frappe.get_all(
		"S3 Sync Log",
		filters={"operation": "backup_create", "status": "Success"},
		fields=["duration_ms"],
		order_by="creation desc",
		limit=1,
	)
	duration_ms = int(create_log[0].duration_ms or 0) if create_log else 0
	status, message = _backup_health(creation, latest_size, duration_ms)
	return {
		"status": status,
		"message": message,
		"last_success": creation,
		"last_success_display": frappe.utils.format_datetime(creation),
		"latest_size": latest_size,
		"latest_size_display": _format_bytes(latest_size),
		"estimated_usage": latest_size * retention,
		"estimated_usage_display": _format_bytes(latest_size * retention),
		"restore_points": retention,
		"sample_count": len(database_uploads),
		"duration_ms": duration_ms,
	}


def _empty_capacity_summary(message: str) -> dict:
	return {
		"status": "Warning",
		"message": message,
		"last_success": None,
		"latest_size": 0,
		"estimated_usage": 0,
		"sample_count": 0,
	}


def _format_bytes(value: int) -> str:
	size = float(value or 0)
	for unit in ("B", "KB", "MB", "GB", "TB"):
		if size < 1024 or unit == "TB":
			return f"{size:.0f} {unit}" if unit == "B" else f"{size:.2f} {unit}"
		size /= 1024
	return "0 B"


def _backup_health(last_success, latest_size: int, duration_ms: int) -> tuple[str, str]:
	age = frappe.utils.now_datetime() - last_success
	if age > datetime.timedelta(hours=36):
		return "Critical", _("No successful database backup has completed in the last 36 hours.")
	if latest_size >= 25 * 1024**3:
		return "Critical", _("The latest database backup is at least 25 GB. Review the backup architecture.")
	if duration_ms >= 90 * 60 * 1000:
		return "Critical", _("Database backup generation took at least 90 minutes.")
	if _local_backup_space_is_low(latest_size):
		return "Critical", _("Local free space is less than twice the latest database backup size.")
	if latest_size >= 10 * 1024**3:
		return "Warning", _("The latest database backup is at least 10 GB. Monitor growth.")
	if duration_ms >= 45 * 60 * 1000:
		return "Warning", _("Database backup generation took at least 45 minutes.")
	return "Available", _("Database backup size, age, and generation time are within recommended limits.")


def _local_backup_space_is_low(latest_size: int) -> bool:
	if not latest_size:
		return False
	from frappe.utils.backups import get_backup_path

	try:
		return shutil.disk_usage(get_backup_path()).free < latest_size * 2
	except OSError:
		return False


def scheduled_backup_and_sync() -> None:
	if not frappe.db.exists("DocType", "Object Storage Settings"):
		return
	settings = frappe.get_single("Object Storage Settings")
	if not settings.enable_backup_storage or not settings.backup_cron:
		return
	from croniter import croniter
	from frappe.utils import get_datetime, now_datetime
	from frappe.utils.file_lock import LockTimeoutError

	now = now_datetime()
	last_run = get_datetime(settings.last_backup_sync or (now - datetime.timedelta(days=1)))
	if now < croniter(settings.backup_cron, last_run).get_next(datetime.datetime):
		return
	try:
		run_backup_and_sync()
	except LockTimeoutError:
		log_storage_sync(
			"Failed", _("Backup skipped because another backup is running."), operation="backup_sync"
		)
	except Exception:
		frappe.log_error(title="Scheduled Object Storage Backup", message=frappe.get_traceback())
