import datetime
import os
import re
import time
from zoneinfo import ZoneInfo

import frappe


def _with_trailing_slash(value):
	value = (value or "").strip().strip("/")
	return f"{value}/" if value else ""


def _backup_prefix(settings):
	folder_prefix = _with_trailing_slash(settings.get("folder_prefix"))
	backup_folder_prefix = _with_trailing_slash(settings.get("backup_folder_prefix") or "backups")
	return f"{folder_prefix}{backup_folder_prefix}"


def _is_database_backup(filename):
	return "-database" in filename and filename.endswith(".sql.gz")


def _is_files_backup(filename):
	return bool(re.search(r"-(private-)?files(-enc)?\.(tar|tgz)$", filename))


def _backup_file_enabled(file_path, settings):
	filename = os.path.basename(file_path)
	if _is_database_backup(filename):
		return bool(settings.upload_db_backup)
	if _is_files_backup(filename):
		return bool(settings.upload_files_backup)
	return False


def _backup_date(file_path):
	try:
		return datetime.datetime.fromtimestamp(os.path.getmtime(file_path)).strftime("%Y-%m-%d")
	except OSError:
		return frappe.utils.now_datetime().strftime("%Y-%m-%d")


def _backup_s3_key(settings, site_name, file_path):
	return f"{_backup_prefix(settings)}{site_name}/{_backup_date(file_path)}/{os.path.basename(file_path)}"


def _paths_from_backup_result(backup_files, settings):
	paths = []
	if not backup_files:
		return paths

	if settings.upload_db_backup and backup_files.get("backup_path_db"):
		paths.append(backup_files.get("backup_path_db"))
	if settings.upload_files_backup:
		if backup_files.get("backup_path_files"):
			paths.append(backup_files.get("backup_path_files"))
		if backup_files.get("backup_path_private_files"):
			paths.append(backup_files.get("backup_path_private_files"))

	return paths


def _find_todays_backup_files(settings):
	from frappe.utils.backups import get_backup_path

	backup_path = get_backup_path()
	if not os.path.exists(backup_path):
		raise FileNotFoundError(f"Backup directory not found at {backup_path}")

	today_str = frappe.utils.now_datetime().strftime("%Y%m%d")
	paths = []
	for filename in os.listdir(backup_path):
		file_path = os.path.join(backup_path, filename)
		if filename.startswith(today_str) and _backup_file_enabled(file_path, settings):
			paths.append(file_path)

	return paths


def _dedupe_existing_paths(paths, settings):
	seen = set()
	candidates = []
	for file_path in paths:
		if not file_path:
			continue
		file_path = os.path.abspath(file_path)
		if file_path in seen or not _backup_file_enabled(file_path, settings):
			continue
		seen.add(file_path)
		candidates.append(file_path)
	return candidates


def get_backup_candidates(settings, backup_files=None):
	paths = _paths_from_backup_result(backup_files, settings)
	if not paths:
		paths = _find_todays_backup_files(settings)
	return _dedupe_existing_paths(paths, settings)


def _error_details(exc):
	from erpnext_s3_integration.s3_client import classify_s3_exception

	return classify_s3_exception(exc)


def _upload_backup_file(s3_client, settings, site_name, file_path):
	started = time.monotonic()
	s3_key = _backup_s3_key(settings, site_name, file_path)
	file_size = 0

	try:
		if not os.path.isfile(file_path):
			raise FileNotFoundError(f"Backup file not found at {file_path}")

		file_size = os.path.getsize(file_path)
		with open(file_path, "rb") as fileobj:  # nosemgrep
			s3_client.upload_fileobj(fileobj, s3_key, None, False)

		duration_ms = int((time.monotonic() - started) * 1000)
		log_s3_sync(
			"Success",
			f"Uploaded backup file {os.path.basename(file_path)} to S3.",
			operation="backup_upload",
			source_path=file_path,
			s3_key=s3_key,
			file_size=file_size,
			duration_ms=duration_ms,
		)

		if not settings.keep_local_backups:
			os.remove(file_path)

		return True
	except Exception as e:
		duration_ms = int((time.monotonic() - started) * 1000)
		details = _error_details(e)
		message = f"S3 backup sync failed for {os.path.basename(file_path)}: {e}"
		frappe.log_error(message=f"{message}\n\n{frappe.get_traceback()}", title="S3 Backup Sync")
		log_s3_sync(
			"Failed",
			message,
			operation="backup_upload",
			source_path=file_path,
			s3_key=s3_key,
			file_size=file_size,
			duration_ms=duration_ms,
			error_category=details.get("category"),
			error_code=details.get("error_code"),
			retryable=details.get("retryable"),
		)
		return False


def sync_backup_files(backup_files=None):
	"""Uploads selected database and file backups to S3."""
	settings = frappe.get_single("S3 Integration Settings")
	if not settings.enable_backups_s3:
		return False

	if not settings.upload_db_backup and not settings.upload_files_backup:
		log_s3_sync("Failed", "No backup upload type is enabled.", operation="backup_sync")
		return False

	site_name = frappe.local.site

	try:
		candidates = get_backup_candidates(settings, backup_files=backup_files)
	except Exception as e:
		details = _error_details(e)
		message = f"Could not collect backup files for S3 sync: {e}"
		frappe.log_error(message=f"{message}\n\n{frappe.get_traceback()}", title="S3 Backup Sync")
		log_s3_sync(
			"Failed",
			message,
			operation="backup_sync",
			error_category=details.get("category"),
			error_code=details.get("error_code"),
			retryable=details.get("retryable"),
		)
		return False

	if not candidates:
		log_s3_sync("Failed", "No eligible backup files were found for S3 sync.", operation="backup_sync")
		return False

	from erpnext_s3_integration.s3_client import S3Client

	try:
		s3_client = S3Client()
	except Exception as e:
		details = _error_details(e)
		message = f"Could not initialize S3 client for backup sync: {e}"
		frappe.log_error(message=f"{message}\n\n{frappe.get_traceback()}", title="S3 Backup Sync")
		log_s3_sync(
			"Failed",
			message,
			operation="backup_sync",
			error_category=details.get("category"),
			error_code=details.get("error_code"),
			retryable=details.get("retryable"),
		)
		return False

	results = [_upload_backup_file(s3_client, settings, site_name, file_path) for file_path in candidates]
	success = all(results)

	if success:
		msg = f"Successfully synced {len(candidates)} backup file(s) to S3 for site {site_name}."
		frappe.logger().info(msg)
		log_s3_sync("Success", msg, operation="backup_sync")

		retention_days = settings.get("delete_backups_older_than_days") or 0
		if retention_days > 0:
			cleanup_old_backups(s3_client, f"{_backup_prefix(settings)}{site_name}/", retention_days)
	else:
		log_s3_sync(
			"Failed",
			f"S3 backup sync completed with failures for site {site_name}.",
			operation="backup_sync",
		)

	return success


def after_backup(backup_files=None):
	"""Compatibility wrapper for callers that trigger S3 sync after a Frappe backup."""
	return sync_backup_files(backup_files=backup_files)


def _as_utc_datetime(value):
	value = frappe.utils.get_datetime(value)
	if value.tzinfo is None or value.tzinfo.utcoffset(value) is None:
		value = value.replace(tzinfo=ZoneInfo(frappe.utils.get_system_timezone()))
	return value.astimezone(datetime.UTC)


def cleanup_old_backups(s3_client, prefix, retention_days):
	cutoff_date = datetime.datetime.now(datetime.UTC) - datetime.timedelta(days=retention_days)
	deleted_count = 0

	try:
		paginator = s3_client.client.get_paginator("list_objects_v2")
		for page in paginator.paginate(Bucket=s3_client.bucket_name, Prefix=prefix):
			if "Contents" in page:
				for obj in page["Contents"]:
					if _as_utc_datetime(obj["LastModified"]) < cutoff_date:
						s3_client.delete_object(obj["Key"])
						deleted_count += 1

		if deleted_count > 0:
			log_s3_sync(
				"Success",
				f"Cleaned up {deleted_count} S3 backup(s) older than {retention_days} days.",
				operation="backup_cleanup",
			)
	except Exception as e:
		details = _error_details(e)
		frappe.log_error(f"S3 Backup Cleanup Failed: {e}", "S3 Backup Sync Error")
		log_s3_sync(
			"Failed",
			f"Backup cleanup failed: {e}",
			operation="backup_cleanup",
			error_category=details.get("category"),
			error_code=details.get("error_code"),
			retryable=details.get("retryable"),
		)


def log_s3_sync(status, message, **details):
	try:
		if not frappe.db.exists("DocType", "S3 Sync Log"):
			return

		log = frappe.new_doc("S3 Sync Log")
		log.status = status
		log.message = message

		for fieldname, value in details.items():
			if log.meta.has_field(fieldname):
				log.set(fieldname, value)

		log.insert(ignore_permissions=True)
		frappe.db.commit()  # nosemgrep
	except Exception as e:
		frappe.log_error(f"Failed to create S3 Sync Log: {e!s}", "S3 Sync Log Error")


def run_backup_and_sync(create_new_backup=None, update_last_sync=False):
	settings = frappe.get_single("S3 Integration Settings")
	if not settings.enable_backups_s3:
		return False

	if create_new_backup is None:
		create_new_backup = bool(settings.get("create_new_backup_before_sync", 1))

	from frappe.utils.synchronization import filelock

	with filelock("s3_backup_sync", timeout=1):
		backup_files = None
		if create_new_backup:
			from frappe.utils.backups import backup

			backup_files = backup(with_files=settings.upload_files_backup)

		success = sync_backup_files(backup_files=backup_files)
		if success and update_last_sync:
			frappe.db.set_single_value(
				"S3 Integration Settings",
				"last_backup_sync",
				frappe.utils.now_datetime(),
			)
			frappe.db.commit()  # nosemgrep

		return success


def scheduled_backup_and_sync():
	"""Triggered by Frappe scheduler 'all' event (runs every ~4 mins). Evaluates the frontend CRON."""
	settings = frappe.get_single("S3 Integration Settings")
	if not settings.enable_backups_s3 or not settings.backup_cron_expression:
		return

	from croniter import CroniterBadCronError, croniter
	from frappe.utils import get_datetime, now_datetime
	from frappe.utils.file_lock import LockTimeoutError

	try:
		now = now_datetime()
		last_run = get_datetime(settings.last_backup_sync or (now - datetime.timedelta(days=1)))
		next_run = croniter(settings.backup_cron_expression, last_run).get_next(datetime.datetime)

		if now >= next_run:
			run_backup_and_sync(
				create_new_backup=bool(settings.get("create_new_backup_before_sync", 1)),
				update_last_sync=True,
			)

	except LockTimeoutError:
		log_s3_sync(
			"Failed",
			"S3 backup sync skipped because another sync is already running.",
			operation="backup_sync",
		)
	except CroniterBadCronError:
		frappe.log_error(
			f"Invalid CRON expression in S3 Settings: {settings.backup_cron_expression}",
			"S3 Backup Sync Error",
		)
	except Exception as e:
		details = _error_details(e)
		log_s3_sync(
			"Failed",
			f"Scheduled S3 Backup Failed: {e!s}\n\n{frappe.get_traceback()}",
			operation="backup_sync",
			error_category=details.get("category"),
			error_code=details.get("error_code"),
			retryable=details.get("retryable"),
		)
