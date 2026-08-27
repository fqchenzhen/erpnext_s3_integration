from frappe.core.doctype.file.utils import get_safe_file_name

OBJECT_URL_PREFIX = "/s3/"


def build_object_url(object_key: str, file_name: str | None) -> str:
	key = _normalize_identifier(object_key)
	if not key:
		raise ValueError("An object key is required")
	return f"{OBJECT_URL_PREFIX}{key}/{get_safe_file_name(file_name or 'download')}"


def is_object_url(file_url: str | None) -> bool:
	return bool(file_url and file_url.startswith(OBJECT_URL_PREFIX))


def object_key_candidates(identifier: str | None) -> tuple[str, ...]:
	value = _normalize_identifier(identifier)
	if not value:
		return ()
	candidates = [value]
	parent, separator, _file_name = value.rpartition("/")
	if separator and parent:
		candidates.append(parent)
	return tuple(dict.fromkeys(candidates))


def object_url_from_identifier(identifier: str | None) -> str:
	return f"{OBJECT_URL_PREFIX}{_normalize_identifier(identifier)}"


def _normalize_identifier(identifier: str | None) -> str:
	value = (identifier or "").strip()
	if value.startswith(OBJECT_URL_PREFIX):
		value = value[len(OBJECT_URL_PREFIX) :]
	return value.strip("/")
