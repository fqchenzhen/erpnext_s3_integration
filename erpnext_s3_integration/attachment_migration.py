"""One-time migration of Frappe-managed local attachments to object storage.

The migration only changes File records after verifying the uploaded bytes.
Local copies are kept until a separate, administrator-requested cleanup.
"""

import hashlib
import mimetypes
from pathlib import Path
from urllib.parse import quote
from uuid import uuid4

import frappe
from frappe import _
from frappe.utils import add_days, cint, get_datetime, now_datetime

from erpnext_s3_integration.file_hooks import attachment_storage_enabled, generate_object_key
from erpnext_s3_integration.object_storage.service import ObjectStorageService
from erpnext_s3_integration.object_urls import build_object_url
from erpnext_s3_integration.storage_policy import StorageTarget, get_storage_target

BATCH_SIZE = 25
SCAN_LIMIT = 500
SCAN_SAMPLE_LIMIT = 30
CLEANUP_GRACE_DAYS = 7
TRANSFER_LOCK_SECONDS = 3600
TRANSFER_LOCK_NAME = "object-storage-legacy-attachment-transfer"
MIGRATION_JOB = "erpnext_s3_integration.attachment_migration.migrate_batch"
CLEANUP_JOB = "erpnext_s3_integration.attachment_migration.cleanup_batch"
CLEANUP_REF_FIELDS = [
	"name",
	"file_url",
	"object_storage_profile",
	"object_storage_key",
	"object_storage_migrated_at",
	"object_storage_local_cleaned_at",
	"attached_to_doctype",
	"attached_to_name",
]


def _require_manager():
	frappe.only_for("System Manager")


def _service():
	if not attachment_storage_enabled():
		frappe.throw(_("Enable attachment object storage before migrating local files."))
	settings = frappe.get_single("Object Storage Settings")
	return ObjectStorageService(settings.attachment_storage_profile)


def _is_local_url(url):
	return isinstance(url, str) and url.startswith(("/files/", "/private/files/"))


def _local_path(url):
	"""Resolve under this site's file directories; never follow symlinks."""
	if not _is_local_url(url):
		raise ValueError("Not a local attachment URL")
	private = url.startswith("/private/files/")
	prefix = "/private/files/" if private else "/files/"
	root = Path(frappe.get_site_path("private" if private else "public", "files")).resolve()
	relative = url.removeprefix(prefix)
	if not relative or relative.startswith("/") or ".." in Path(relative).parts:
		raise ValueError("Unsafe local attachment path")
	path = root / relative
	current = path
	while current != root:
		if current.is_symlink():
			raise ValueError("Symlinked attachment paths are not migrated")
		if root not in current.parents:
			raise ValueError("Attachment path escapes the site files directory")
		current = current.parent
	resolved = path.resolve()
	if not resolved.is_relative_to(root):
		raise ValueError("Attachment path escapes the site files directory")
	return resolved


def _md5(path):
	digest = hashlib.md5(usedforsecurity=False)
	size = 0
	with path.open("rb") as stream:
		while chunk := stream.read(1024 * 1024):
			digest.update(chunk)
			size += len(chunk)
	return digest.hexdigest(), size


def _verify_remote(service, key, expected_hash, expected_size):
	info = service.backend.head(key)
	if info is None or info.size != expected_size:
		raise ValueError("OSS object missing or size mismatch")
	digest = hashlib.md5(usedforsecurity=False)
	size = 0
	stream = service.backend.get(key)
	try:
		while chunk := stream.read(1024 * 1024):
			digest.update(chunk)
			size += len(chunk)
	finally:
		stream.close()
	if digest.hexdigest() != expected_hash or size != expected_size:
		raise ValueError("OSS object content verification failed")


def _ensure_remote_file(path, service, key, expected_hash, expected_size, file_name):
	try:
		_verify_remote(service, key, expected_hash, expected_size)
		return
	except ValueError:
		pass

	if _md5(path) != (expected_hash, expected_size):
		raise ValueError("Local file changed during migration")
	with path.open("rb") as body:
		service.put(
			key,
			body,
			content_length=expected_size,
			content_type=mimetypes.guess_type(file_name or "")[0],
			metadata={"content-hash": expected_hash},
		)
	_verify_remote(service, key, expected_hash, expected_size)


def _transfer_lock_key():
	return frappe.cache().make_key(TRANSFER_LOCK_NAME)


def _acquire_transfer_lock():
	"""One owner across all migration/cleanup batches, scoped by the site's DB cache key."""
	token = uuid4().hex
	if frappe.cache().set(_transfer_lock_key(), token, nx=True, ex=TRANSFER_LOCK_SECONDS):
		frappe.db.after_rollback.add(lambda: _release_transfer_lock(token))
		return token
	return None


def _renew_transfer_lock(token):
	if not isinstance(token, str) or not token:
		return False
	return bool(
		frappe.cache().eval(
			"if redis.call('get', KEYS[1]) == ARGV[1] then "
			"return redis.call('expire', KEYS[1], ARGV[2]) else return 0 end",
			1,
			_transfer_lock_key(),
			token,
			TRANSFER_LOCK_SECONDS,
		)
	)


def _release_transfer_lock(token):
	if token:
		frappe.cache().eval(
			"if redis.call('get', KEYS[1]) == ARGV[1] then "
			"return redis.call('del', KEYS[1]) else return 0 end",
			1,
			_transfer_lock_key(),
			token,
		)


def _finish_transfer_lock(token):
	# Keep the lock until the final batch's DB writes commit.
	frappe.db.after_commit.add(lambda: _release_transfer_lock(token))
	frappe.db.after_rollback.add(lambda: _release_transfer_lock(token))


def _migration_ineligibility(ref):
	"""Never infer that an orphaned or unowned File is a safe business attachment."""
	if ref.is_folder:
		return "protected"
	if get_storage_target(ref) != StorageTarget.OBJECT_STORAGE:
		return "protected"
	doctype, name = ref.attached_to_doctype, ref.attached_to_name
	if not doctype or not name:
		return "unknown"
	if not frappe.db.exists("DocType", doctype) or not frappe.db.exists(doctype, name):
		return "unknown"
	return None


def _scan_local_files():
	"""Bounded administrator preview, not a promise about an incomplete large scan."""
	entries = frappe.get_all(
		"File",
		filters={"is_folder": 0},
		or_filters=[["file_url", "like", "/files/%"], ["file_url", "like", "/private/files/%"]],
		fields=[
			"name",
			"file_name",
			"file_url",
			"is_folder",
			"is_private",
			"attached_to_doctype",
			"attached_to_name",
		],
		order_by="name asc",
		limit=SCAN_LIMIT + 1,
	)
	truncated = len(entries) > SCAN_LIMIT
	entries = entries[:SCAN_LIMIT]
	by_url = {}
	for entry in entries:
		by_url.setdefault(entry.file_url, []).append(entry)
	status_counts = {"eligible": 0, "protected": 0, "unknown": 0, "missing": 0}
	preview = []
	for url, refs in by_url.items():
		reasons = {_migration_ineligibility(ref) for ref in refs}
		if "protected" in reasons:
			status = "protected"
		elif "unknown" in reasons:
			status = "unknown"
		elif len(url) > 1000:
			status = "missing"
		elif any(bool(ref.is_private) != url.startswith("/private/files/") for ref in refs):
			status = "protected"
		else:
			try:
				status = "eligible" if _local_path(url).is_file() else "missing"
			except ValueError, OSError:
				status = "missing"
		if truncated and status == "eligible":
			status = "unknown"
		# A full migration rechecks every File sharing the URL, including any outside the scan cap.
		for ref in refs:
			status_counts[status] += 1
			if len(preview) < SCAN_SAMPLE_LIMIT:
				preview.append(
					{
						"name": ref.name,
						"file_name": ref.file_name,
						"attached_to_doctype": ref.attached_to_doctype,
						"status": status,
					}
				)
	return {
		"scanned_records": len(entries),
		"truncated": truncated,
		"counts": status_counts,
		"sample": preview,
	}


def _local_files(start_after=""):
	return frappe.get_all(
		"File",
		filters={"is_folder": 0, **({"name": [">", start_after]} if start_after else {})},
		or_filters=[
			["file_url", "like", "/files/%"],
			["file_url", "like", "/private/files/%"],
		],
		fields=["name", "file_url"],
		order_by="name asc",
		limit=BATCH_SIZE,
	)


def _migration_refs(url):
	# Lock every File row sharing the original URL before updating references.
	return frappe.qb.get_query(
		"File",
		fields=[
			"name",
			"file_name",
			"is_private",
			"is_folder",
			"attached_to_doctype",
			"attached_to_name",
			"attached_to_field",
		],
		filters={"file_url": url},
		order_by="name asc",
		for_update=True,
	).run(as_dict=True)


def _linked_attach_fields(refs, old_url):
	"""Find ordinary Attach/Attach Image fields referencing the original URL."""
	result = []
	seen = set()
	for ref in refs:
		doctype, name = ref.attached_to_doctype, ref.attached_to_name
		if not doctype or not name or (doctype, name) in seen:
			continue
		seen.add((doctype, name))
		if not frappe.db.exists(doctype, name):
			continue
		doc = frappe.get_doc(doctype, name)
		for df in doc.meta.fields:
			if df.fieldtype not in ("Attach", "Attach Image"):
				continue
			if doc.get(df.fieldname) == old_url:
				result.append((doctype, name, df.fieldname))
		for table_field in doc.meta.get_table_fields():
			for row in doc.get(table_field.fieldname) or []:
				for df in row.meta.fields:
					if df.fieldtype in ("Attach", "Attach Image") and row.get(df.fieldname) == old_url:
						result.append((row.doctype, row.name, df.fieldname))
	return result


def _replace_linked_attach_fields(refs, old_url, new_url):
	for doctype, name, fieldname in _linked_attach_fields(refs, old_url):
		if frappe.get_meta(doctype).issingle:
			current = frappe.db.get_single_value(doctype, fieldname, cache=False, for_update=True)
			if current == old_url:
				frappe.db.set_single_value(doctype, fieldname, new_url, update_modified=False)
			continue
		# Compare the old value in the UPDATE so a concurrent user edit is not overwritten.
		frappe.db.set_value(
			doctype,
			{"name": name, fieldname: old_url},
			fieldname,
			new_url,
			update_modified=False,
		)


def _replace_attachment_comment_urls(refs, old_url, new_url):
	"""Keep Frappe's generated attachment history links working after migration."""
	old_urls = {old_url, quote(old_url, safe="/:")}
	new_urls = {old_url: new_url, quote(old_url, safe="/:"): quote(new_url, safe="/:")}
	seen = set()
	for ref in refs:
		doctype, name = ref.attached_to_doctype, ref.attached_to_name
		if not doctype or not name or doctype in {"Communication", "Comment"} or (doctype, name) in seen:
			continue
		seen.add((doctype, name))
		comments = frappe.get_all(
			"Comment",
			filters={
				"reference_doctype": doctype,
				"reference_name": name,
				"comment_type": "Attachment",
			},
			fields=["name", "content"],
		)
		for comment in comments:
			content = comment.content or ""
			updated = content
			for old in old_urls:
				updated = updated.replace(old, new_urls[old])
			if updated != content:
				frappe.db.set_value(
					"Comment",
					{"name": comment.name, "content": content},
					"content",
					updated,
					update_modified=False,
				)


def _has_related_content_reference(refs, url):
	"""Detect old URLs in related Comment or Communication bodies before local deletion."""
	seen = set()
	for ref in refs:
		doctype, name = ref.attached_to_doctype, ref.attached_to_name
		if not doctype or not name or (doctype, name) in seen:
			continue
		seen.add((doctype, name))
		for content_doctype in ("Comment", "Communication"):
			if frappe.db.exists(
				content_doctype,
				{
					"reference_doctype": doctype,
					"reference_name": name,
					"content": ["like", f"%{url}%"],
				},
			):
				return True
	return False


def _migrate_url(url, service):
	if len(url) > 1000:
		raise ValueError("Local attachment URL exceeds the source URL field limit")
	refs = _migration_refs(url)
	if not refs:
		return None
	if any(_migration_ineligibility(ref) for ref in refs):
		return None
	is_private = url.startswith("/private/files/")
	if any(bool(ref.is_private) != is_private for ref in refs):
		raise ValueError("Inconsistent privacy for the same local file URL")
	path = _local_path(url)
	if not path.is_file():
		raise FileNotFoundError(str(path))
	digest, size = _md5(path)
	key = generate_object_key(digest, is_private, service.profile.prefix)
	_ensure_remote_file(path, service, key, digest, size, refs[0].file_name)
	new_url = build_object_url(key, refs[0].file_name)
	_replace_linked_attach_fields(refs, url, new_url)
	_replace_attachment_comment_urls(refs, url, new_url)
	migrated_at = now_datetime()
	for ref in refs:
		frappe.db.set_value(
			"File",
			ref.name,
			{
				"file_url": new_url,
				"object_storage_profile": service.profile.name,
				"object_storage_key": key,
				"object_storage_source_url": url,
				"object_storage_migrated_at": migrated_at,
				"content_hash": None,
			},
			update_modified=False,
		)
		frappe.clear_document_cache("File", ref.name)
	frappe.enqueue(
		"erpnext_s3_integration.object_storage.classification.tag_file_object",
		file_name=refs[0].name,
		queue="short",
		enqueue_after_commit=True,
	)
	return key, size


def _log(status, operation, url, key=None, size=0, message=None):
	frappe.get_doc(
		{
			"doctype": "S3 Sync Log",
			"status": status,
			"operation": operation,
			"source_path": url[:140],
			"s3_key": key,
			"file_size": size,
			"message": (message or "")[:2000],
		}
	).insert(ignore_permissions=True)


@frappe.whitelist(methods=["GET"])
def get_local_migration_status():
	_require_manager()
	return {
		"local_file_records": frappe.db.count("File", {"file_url": ["like", "/files/%"]})
		+ frappe.db.count("File", {"file_url": ["like", "/private/files/%"]}),
		"pending_cleanup_records": frappe.db.count(
			"File",
			{
				"object_storage_source_url": ["is", "set"],
				"object_storage_local_cleaned_at": ["is", "not set"],
			},
		),
		"cleanup_grace_days": CLEANUP_GRACE_DAYS,
		"migration_scan": _scan_local_files(),
	}


@frappe.whitelist(methods=["POST"])
def start_local_migration():
	_require_manager()
	_service()
	token = _acquire_transfer_lock()
	if not token:
		return {"queued": False, "running": True}
	frappe.enqueue(
		MIGRATION_JOB,
		run_token=token,
		queue="long",
		timeout=1500,
		enqueue_after_commit=True,
	)
	return {"queued": True}


def migrate_batch(start_after="", run_token=None):
	if not _renew_transfer_lock(run_token):
		return
	try:
		service = _service()
		entries = _local_files(start_after)
	except Exception:
		_finish_transfer_lock(run_token)
		raise
	seen = set()
	for entry in entries:
		if entry.file_url in seen:
			continue
		seen.add(entry.file_url)
		frappe.db.savepoint("attachment_migration_entry")
		try:
			result = _migrate_url(entry.file_url, service)
		except Exception as exc:
			frappe.db.rollback(save_point="attachment_migration_entry")
			_log("Failed", "Attachment Migration", entry.file_url, message=str(exc))
		else:
			if result:
				_log("Success", "Attachment Migration", entry.file_url, key=result[0], size=result[1])
	if len(entries) == BATCH_SIZE:
		frappe.enqueue(
			MIGRATION_JOB,
			start_after=entries[-1].name,
			run_token=run_token,
			queue="long",
			timeout=1500,
			enqueue_after_commit=True,
		)
	else:
		_finish_transfer_lock(run_token)


def _cleanup_entries(start_after=""):
	return frappe.get_all(
		"File",
		filters={
			"object_storage_source_url": ["is", "set"],
			"object_storage_local_cleaned_at": ["is", "not set"],
			**({"name": [">", start_after]} if start_after else {}),
		},
		fields=["name", "object_storage_source_url"],
		order_by="name asc",
		limit=BATCH_SIZE,
	)


def _cleanup_url(url):
	# Never delete when an ordinary File still points to the local URL.
	if frappe.db.exists("File", {"file_url": url}):
		return False
	refs = frappe.get_all(
		"File",
		filters={"object_storage_source_url": url},
		fields=CLEANUP_REF_FIELDS,
		limit=10000,
	)
	if not refs or len(refs) == 10000:
		return False
	# All File records for this source must have been migrated; never clean a
	# path while a new or legacy File still references it under the local URL.
	if any(ref.object_storage_local_cleaned_at for ref in refs):
		return False
	# HTML/email bodies may contain literal URLs beyond the Frappe Attach fields.
	# Keep those copies until the administrator has resolved such references separately.
	if any(ref.attached_to_doctype in {"Communication", "Comment"} for ref in refs):
		return False
	threshold = get_datetime(add_days(now_datetime(), -CLEANUP_GRACE_DAYS))
	if any(
		not ref.object_storage_migrated_at or get_datetime(ref.object_storage_migrated_at) > threshold
		for ref in refs
	):
		return False
	if any(
		not ref.object_storage_key or not ref.object_storage_profile or not ref.file_url.startswith("/s3/")
		for ref in refs
	):
		return False
	if len({(ref.object_storage_profile, ref.object_storage_key) for ref in refs}) != 1:
		return False
	if _linked_attach_fields(refs, url) or _has_related_content_reference(refs, url):
		return False
	path = _local_path(url)
	if not path.is_file():
		# Missing copy is not a reason to change OSS-backed File records.
		return False
	digest, size = _md5(path)
	key = refs[0].object_storage_key
	if key.rsplit("/", 1)[-1] != digest:
		raise ValueError("Local copy changed since migration")
	service = ObjectStorageService(refs[0].object_storage_profile, require_enabled=False)
	_verify_remote(service, key, digest, size)
	# Recheck after the network round trip; the local copy or its references can change meanwhile.
	if frappe.db.exists("File", {"file_url": url}):
		return False
	current_refs = frappe.get_all(
		"File",
		filters={"object_storage_source_url": url},
		fields=CLEANUP_REF_FIELDS,
		limit=10000,
	)
	if len(current_refs) != len(refs) or len(current_refs) == 10000:
		return False
	if sorted(tuple(ref.get(field) for field in CLEANUP_REF_FIELDS) for ref in current_refs) != sorted(
		tuple(ref.get(field) for field in CLEANUP_REF_FIELDS) for ref in refs
	):
		return False
	if any(ref.attached_to_doctype in {"Communication", "Comment"} for ref in current_refs):
		return False
	if _linked_attach_fields(current_refs, url) or _has_related_content_reference(current_refs, url):
		return False
	try:
		current_path = _local_path(url)
	except ValueError:
		return False
	if current_path != path or not current_path.is_file() or _md5(current_path) != (digest, size):
		return False
	current_path.unlink()
	cleaned_at = now_datetime()
	for ref in refs:
		frappe.db.set_value(
			"File", ref.name, "object_storage_local_cleaned_at", cleaned_at, update_modified=False
		)
	return True


@frappe.whitelist(methods=["POST"])
def start_local_cleanup(confirm=0):
	_require_manager()
	if not cint(confirm):
		frappe.throw(_("Explicit confirmation is required to delete local copies."))
	_service()
	token = _acquire_transfer_lock()
	if not token:
		return {"queued": False, "running": True}
	frappe.enqueue(
		CLEANUP_JOB,
		run_token=token,
		queue="long",
		timeout=1500,
		enqueue_after_commit=True,
	)
	return {"queued": True}


def cleanup_batch(start_after="", run_token=None):
	if not _renew_transfer_lock(run_token):
		return
	try:
		entries = _cleanup_entries(start_after)
	except Exception:
		_finish_transfer_lock(run_token)
		raise
	seen = set()
	for entry in entries:
		url = entry.object_storage_source_url
		if url in seen:
			continue
		seen.add(url)
		frappe.db.savepoint("attachment_cleanup_entry")
		try:
			cleaned = _cleanup_url(url)
		except Exception as exc:
			frappe.db.rollback(save_point="attachment_cleanup_entry")
			_log("Failed", "Local Attachment Cleanup", url, message=str(exc))
		else:
			if cleaned:
				_log("Success", "Local Attachment Cleanup", url)
	if len(entries) == BATCH_SIZE:
		frappe.enqueue(
			CLEANUP_JOB,
			start_after=entries[-1].name,
			run_token=run_token,
			queue="long",
			timeout=1500,
			enqueue_after_commit=True,
		)
	else:
		_finish_transfer_lock(run_token)
