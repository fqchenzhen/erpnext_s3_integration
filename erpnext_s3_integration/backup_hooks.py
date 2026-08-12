import datetime
import io
import json
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

BACKUP_RESULT_FIELDS = (
	"backup_path_db",
	"backup_path_conf",
	"backup_path_files",
	"backup_path_private_files",
)
BACKUP_RUN_PATTERNS = (
	re.compile(r"^(?P<run>.+?)(?:-partial)?-database(?:-enc)?\.sql\.gz$"),
	re.compile(r"^(?P<run>.+?)-site_config_backup(?:-enc)?\.json$"),
	re.compile(r"^(?P<run>.+?)-private-files(?:-enc)?\.(?:tar|tgz)$"),
	re.compile(r"^(?P<run>.+?)-files(?:-enc)?\.(?:tar|tgz)$"),
	re.compile(r"^(?P<run>.+?)-backup_complete\.json$"),
)


def _safe_tag(value: str) -> str:
	return re.sub(r"[^A-Za-z0-9_.:/=+\-@]", "_", value or "")[:128] or "unknown"


def _is_database_backup(filename: str) -> bool:
	return "-database" in filename and filename.endswith(".sql.gz")


def _is_public_files_backup(filename: str) -> bool:
	return bool(re.search(r"-files(-enc)?\.(tar|tgz)$", filename)) and "-private-files" not in filename


def _is_private_files_backup(filename: str) -> bool:
	return bool(re.search(r"-private-files(-enc)?\.(tar|tgz)$", filename))


def _is_site_config_backup(filename: str) -> bool:
	return bool(re.search(r"-site_config_backup(-enc)?\.json$", filename))


def _backup_enabled(file_path: str, settings) -> bool:
	filename = os.path.basename(file_path)
	return bool(
		(_is_database_backup(filename) and settings.upload_database_backup)
		or (_is_site_config_backup(filename) and settings.upload_database_backup)
		or (_is_public_files_backup(filename) and settings.upload_public_files_backup)
		or (_is_private_files_backup(filename) and settings.upload_private_files_backup)
	)


def _backup_date(file_path: str) -> str:
	match = re.match(r"(?P<date>\d{8})_\d{6}", os.path.basename(file_path))
	if match:
		return datetime.datetime.strptime(match.group("date"), "%Y%m%d").strftime("%Y-%m-%d")
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
	for key in BACKUP_RESULT_FIELDS:
		path = result.get(key)
		if path and _backup_enabled(path, settings):
			paths.append(os.path.abspath(path))
	return list(dict.fromkeys(paths))


def _today_candidates(settings) -> list[str]:
	from frappe.utils.backups import get_backup_path

	backup_path = get_backup_path()
	today = frappe.utils.now_datetime().strftime("%Y%m%d")
	paths = [
		os.path.join(backup_path, filename)
		for filename in os.listdir(backup_path)
		if filename.startswith(today) and _backup_enabled(filename, settings)
	]
	return _latest_complete_backup_group(paths, settings)


def _latest_complete_backup_group(paths: list[str], settings) -> list[str]:
	required_kinds = _required_backup_kinds(settings)
	by_run: dict[str, list[str]] = {}
	for path in paths:
		if run_id := _backup_run_id(path):
			by_run.setdefault(run_id, []).append(path)
	complete = [
		run_paths
		for run_paths in by_run.values()
		if required_kinds <= {_backup_kind(path) for path in run_paths}
	]
	if not complete:
		return []
	return max(complete, key=_local_backup_group_sort_key)


def _required_backup_kinds(settings) -> set[str]:
	kinds = set()
	if settings.upload_database_backup:
		kinds.update(("database", "site-config"))
	if settings.upload_public_files_backup:
		kinds.add("public-files")
	if settings.upload_private_files_backup:
		kinds.add("private-files")
	return kinds


def _is_complete_backup_group(paths: list[str], settings) -> bool:
	required_kinds = _required_backup_kinds(settings)
	run_ids = {_backup_run_id(path) for path in paths}
	return bool(
		required_kinds
		and None not in run_ids
		and len(run_ids) == 1
		and required_kinds <= {_backup_kind(path) for path in paths}
	)


def _local_backup_group_sort_key(paths: list[str]) -> tuple:
	modified = max((os.path.getmtime(path) for path in paths), default=0)
	return _backup_run_id(paths[0]) or "", modified


def sync_backup_files(backup_files: dict | None = None) -> bool:
	settings = frappe.get_single("Object Storage Settings")
	if not settings.enable_backup_storage:
		return False
	paths = _paths_from_result(backup_files, settings) or _today_candidates(settings)
	if not paths or not _is_complete_backup_group(paths, settings):
		log_storage_sync(
			"Failed", _("No complete backup group was found."), operation="backup_sync"
		)
		return False

	service = ObjectStorageService(settings.backup_storage_profile)
	results = [_upload_backup(service, path) for path in paths]
	success = all(results)
	if success:
		success = _upload_completion_marker(service, paths)
	if success:
		_cleanup_remote_backups(
			service,
			service.key(f"backups/{frappe.local.site}/"),
			settings.backup_retention_days,
			_required_backup_kinds(settings),
		)
	log_storage_sync(
		"Success" if success else "Failed",
		_("Backup object storage sync completed for {0} file(s).").format(len(paths)),
		operation="backup_sync",
	)
	return success


def _upload_backup(service: ObjectStorageService, file_path: str) -> bool:
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


def _upload_completion_marker(service: ObjectStorageService, paths: list[str]) -> bool:
	started = time.monotonic()
	run_id = _backup_run_id(paths[0])
	if not run_id:
		return False
	filename = f"{run_id}-backup_complete.json"
	key = service.key(f"backups/{frappe.local.site}/{_backup_date(paths[0])}/{filename}")
	payload = json.dumps(
		{
			"run_id": run_id,
			"completed_at": frappe.utils.now_datetime().isoformat(),
			"files": [os.path.basename(path) for path in paths],
		},
		separators=(",", ":"),
	).encode()
	try:
		service.backend.put(
			key,
			io.BytesIO(payload),
			content_type="application/json",
			content_length=len(payload),
			metadata={"backup-run": run_id},
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
			_("Marked backup group {0} as complete.").format(run_id),
			operation="backup_complete",
			s3_key=key,
			file_size=len(payload),
			duration_ms=int((time.monotonic() - started) * 1000),
		)
		return True
	except Exception as exc:
		details = classify_storage_error(exc)
		log_storage_sync(
			"Failed",
			_("Could not mark backup group {0} as complete: {1}").format(run_id, exc),
			operation="backup_complete",
			s3_key=key,
			duration_ms=int((time.monotonic() - started) * 1000),
			error_category=details.category,
			error_code=details.code,
			retryable=details.retryable,
		)
		frappe.log_error(title="Object Storage Backup", message=frappe.get_traceback())
		return False


def _cleanup_remote_backups(
	service: ObjectStorageService,
	prefix: str,
	restore_points: int,
	required_kinds: set[str],
) -> bool:
	try:
		cleanup_old_backups(service, prefix, restore_points, required_kinds)
		return True
	except Exception as exc:
		details = classify_storage_error(exc)
		log_storage_sync(
			"Failed",
			_("Backup retention cleanup failed: {0}").format(exc),
			operation="backup_cleanup",
			error_category=details.category,
			error_code=details.code,
			retryable=details.retryable,
		)
		frappe.log_error(title="Object Storage Backup Cleanup", message=frappe.get_traceback())
		return False


def cleanup_old_backups(
	service: ObjectStorageService,
	prefix: str,
	restore_points: int,
	required_kinds: set[str] | None = None,
) -> int:
	if not restore_points:
		return 0
	objects = [item for item in service.backend.list(prefix) if _backup_run_id(item.key)]
	by_date_and_run: dict[str, dict[str, list]] = {}
	for item in objects:
		backup_date = _date_from_backup_object(item)
		run_id = _backup_run_id(item.key)
		if backup_date and run_id:
			by_date_and_run.setdefault(backup_date, {}).setdefault(run_id, []).append(item)

	first_marker_run = _first_completion_marker_run(by_date_and_run)
	complete_runs_by_date = {
		backup_date: complete_runs
		for backup_date, runs in by_date_and_run.items()
		if (
			complete_runs := _complete_remote_runs(
				runs,
				required_kinds,
				first_marker_run,
			)
		)
	}
	retained_dates = set(sorted(complete_runs_by_date, reverse=True)[:restore_points])
	deleted = 0
	for backup_date, runs in by_date_and_run.items():
		if backup_date not in retained_dates:
			for item in _flatten_remote_runs(runs):
				service.backend.delete(item.key)
				deleted += 1
			continue
		latest_run_id = max(
			complete_runs_by_date[backup_date],
			key=lambda run_id: _remote_backup_group_sort_key(runs[run_id]),
		)
		for run_id, run_objects in runs.items():
			if run_id == latest_run_id:
				continue
			for item in run_objects:
				service.backend.delete(item.key)
				deleted += 1
	return deleted


def _complete_remote_runs(
	runs: dict[str, list],
	required_kinds: set[str] | None,
	first_marker_run: str | None,
) -> list[str]:
	complete = []
	for run_id, items in runs.items():
		kinds = {_backup_kind(item.key) for item in items}
		if "completion-marker" in kinds:
			complete.append(run_id)
		elif first_marker_run and run_id < first_marker_run and "database" in kinds:
			complete.append(run_id)
		elif not first_marker_run and (not required_kinds or required_kinds <= kinds):
			complete.append(run_id)
	return complete


def _first_completion_marker_run(by_date_and_run: dict[str, dict[str, list]]) -> str | None:
	marked_runs = [
		run_id
		for runs in by_date_and_run.values()
		for run_id, items in runs.items()
		if "completion-marker" in {_backup_kind(item.key) for item in items}
	]
	return min(marked_runs, default=None)


def _flatten_remote_runs(runs: dict[str, list]) -> list:
	return [item for items in runs.values() for item in items]


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


def _backup_kind(key: str) -> str:
	filename = os.path.basename(key)
	if _is_database_backup(filename):
		return "database"
	if _is_site_config_backup(filename):
		return "site-config"
	if _is_private_files_backup(filename):
		return "private-files"
	if _is_public_files_backup(filename):
		return "public-files"
	if filename.endswith("-backup_complete.json"):
		return "completion-marker"
	return filename


def _backup_run_id(key: str) -> str | None:
	filename = os.path.basename(key)
	for pattern in BACKUP_RUN_PATTERNS:
		if match := pattern.match(filename):
			return match.group("run")
	return None


def _remote_backup_group_sort_key(items) -> tuple:
	modified = max((_object_modified(item) for item in items), default=datetime.datetime.min.replace(tzinfo=datetime.UTC))
	return _backup_run_id(items[0].key) or "", modified


def _object_modified(item) -> datetime.datetime:
	modified = item.last_modified or datetime.datetime.min.replace(tzinfo=datetime.UTC)
	if modified.tzinfo is None:
		modified = modified.replace(tzinfo=datetime.UTC)
	return modified.astimezone(datetime.UTC)


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
	from frappe.utils.synchronization import filelock

	with filelock("object_storage_backup_sync", timeout=1):
		include_files = bool(settings.upload_public_files_backup or settings.upload_private_files_backup)
		started = time.monotonic()
		try:
			result = _create_backup(include_files)
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
		_cleanup_local_backups()
		success = sync_backup_files(result)
		if success and update_last_sync:
			frappe.db.set_single_value(
				"Object Storage Settings", "last_backup_sync", frappe.utils.now_datetime()
			)
		return success


def _create_backup(include_files: bool) -> dict[str, str | None]:
	from frappe.utils.backups import BackupGenerator

	# Skip Frappe's age-based pre-cleanup; complete local groups are pruned below by backup_limit.
	backup = BackupGenerator(
		frappe.conf.db_name,
		frappe.conf.db_user,
		frappe.conf.db_password,
		db_socket=frappe.conf.db_socket,
		db_host=frappe.conf.db_host,
		db_port=frappe.conf.db_port,
		db_type=frappe.conf.db_type,
	)
	backup.get_backup(older_than=6, ignore_files=not include_files, force=True)
	return {fieldname: getattr(backup, fieldname) for fieldname in BACKUP_RESULT_FIELDS}


def _cleanup_local_backups() -> bool:
	try:
		from frappe.desk.page.backups.backups import delete_downloadable_backups

		delete_downloadable_backups()
		return True
	except Exception as exc:
		log_storage_sync(
			"Failed",
			_("Local backup retention cleanup failed: {0}").format(exc),
			operation="backup_cleanup_local",
		)
		frappe.log_error(title="Local Backup Cleanup", message=frappe.get_traceback())
		return False


@frappe.whitelist(methods=["POST"])
def run_backup_now() -> bool:
	frappe.only_for("System Manager")
	return run_backup_and_sync()


@frappe.whitelist(methods=["GET"])
def get_backup_capacity_summary() -> dict:
	frappe.only_for("System Manager")
	settings = frappe.get_single("Object Storage Settings")
	local_backup_limit = _local_backup_limit()
	retention = max(int(settings.backup_retention_days or 0), 1)
	if not frappe.db.exists("DocType", "S3 Sync Log"):
		return _empty_capacity_summary(
			_("No backup history is available yet."), local_backup_limit, retention
		)
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
		return _empty_capacity_summary(
			_("Run the first database backup to collect capacity data."),
			local_backup_limit,
			retention,
		)

	latest = database_uploads[0]
	creation = frappe.utils.get_datetime(latest.creation)
	latest_size = int(latest.file_size or 0)
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
		"local_backup_limit": local_backup_limit,
		"sample_count": len(database_uploads),
		"duration_ms": duration_ms,
	}


def _local_backup_limit() -> int:
	return int(frappe.get_system_settings("backup_limit") or 0)


def _empty_capacity_summary(message: str, local_backup_limit: int, restore_points: int) -> dict:
	return {
		"status": "Warning",
		"message": message,
		"last_success": None,
		"latest_size": 0,
		"estimated_usage": 0,
		"restore_points": restore_points,
		"local_backup_limit": local_backup_limit,
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
