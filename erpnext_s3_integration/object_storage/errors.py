from dataclasses import dataclass


class ObjectStorageError(Exception):
	category = "Unexpected"
	retryable = False

	def __init__(self, message: str, *, code: str | None = None, operation: str | None = None):
		super().__init__(message)
		self.code = code
		self.operation = operation


class ObjectNotFoundError(ObjectStorageError):
	category = "Not Found"


class ObjectPermissionError(ObjectStorageError):
	category = "Authorization"


class ObjectConfigurationError(ObjectStorageError):
	category = "Configuration"


class ObjectArchivedError(ObjectStorageError):
	category = "Archived"


class ObjectRestoreInProgressError(ObjectStorageError):
	category = "Restore In Progress"


class ObjectRestoreFailedError(ObjectStorageError):
	category = "Restore Failed"


class ObjectNetworkError(ObjectStorageError):
	category = "Network"
	retryable = True


class ObjectThrottledError(ObjectStorageError):
	category = "Provider Temporary Error"
	retryable = True


@dataclass(frozen=True)
class StorageErrorDetails:
	category: str
	code: str | None
	retryable: bool


def classify_storage_error(exc: Exception) -> StorageErrorDetails:
	chain = _provider_error_chain(exc)
	for candidate in reversed(chain):
		details = _classify_storage_error(candidate)
		if details.category != "Unexpected":
			return details
	return _classify_storage_error(exc)


def _classify_storage_error(exc: Exception) -> StorageErrorDetails:
	if isinstance(exc, ObjectStorageError):
		return StorageErrorDetails(exc.category, exc.code, exc.retryable)
	exception_name = exc.__class__.__name__
	status = getattr(exc, "status_code", None) or getattr(exc, "status", None)
	code = getattr(exc, "code", None) or getattr(exc, "error_code", None)
	response = getattr(exc, "response", None)
	if isinstance(response, dict):
		status = status or response.get("ResponseMetadata", {}).get("HTTPStatusCode")
		code = code or response.get("Error", {}).get("Code")

	if status in {401, 403} or code in {
		"AccessDenied",
		"InvalidAccessKeyId",
		"InvalidAccessKeyId.NotFound",
		"SignatureDoesNotMatch",
	}:
		return StorageErrorDetails("Authorization", str(code) if code else None, False)
	if code in {"InvalidArgument", "InvalidBucketName", "AuthorizationHeaderMalformed"}:
		return StorageErrorDetails("Configuration", str(code), False)
	if status == 404 or code in {"NoSuchKey", "NotFound", "NoSuchBucket"}:
		return StorageErrorDetails("Not Found", str(code) if code else None, False)
	if code in {"InvalidObjectState", "ObjectNotAppendable"}:
		return StorageErrorDetails("Archived", str(code), False)
	if code in {"RestoreAlreadyInProgress", "RestoreAlreadyInProgressError"}:
		return StorageErrorDetails("Restore In Progress", str(code), False)
	if status == 429 or code in {"SlowDown", "Throttling", "RequestTimeout"} or (
		isinstance(status, int) and status >= 500
	):
		return StorageErrorDetails("Provider Temporary Error", str(code) if code else None, True)
	if isinstance(exc, (ConnectionError, TimeoutError)):
		return StorageErrorDetails("Network", str(code) if code else None, True)
	if exception_name in {
		"ConnectTimeoutError",
		"EndpointConnectionError",
		"OperationError",
		"ReadTimeoutError",
		"RequestError",
		"ResponseError",
	}:
		return StorageErrorDetails("Network", str(code) if code else exception_name, True)
	if "Credential" in exception_name:
		return StorageErrorDetails("Authorization", str(code) if code else exception_name, False)
	return StorageErrorDetails("Unexpected", str(code) if code else None, False)


def _provider_error_chain(exc: Exception) -> list[Exception]:
	chain = [exc]
	seen = {id(exc)}
	current = exc
	while callable(unwrap := getattr(current, "unwrap", None)):
		try:
			inner = unwrap()
		except Exception:
			break
		if not isinstance(inner, Exception) or id(inner) in seen:
			break
		chain.append(inner)
		seen.add(id(inner))
		current = inner
	return chain


def is_not_found(exc: Exception) -> bool:
	return classify_storage_error(exc).category == "Not Found"


def normalized_storage_error(exc: Exception, operation: str) -> ObjectStorageError:
	if isinstance(exc, ObjectStorageError):
		return exc
	details = classify_storage_error(exc)
	message = f"Object storage {operation} failed ({details.category})"
	error_types = {
		"Authorization": ObjectPermissionError,
		"Not Found": ObjectNotFoundError,
		"Archived": ObjectArchivedError,
		"Restore In Progress": ObjectRestoreInProgressError,
		"Configuration": ObjectConfigurationError,
		"Network": ObjectNetworkError,
		"Provider Temporary Error": ObjectThrottledError,
	}
	error_type = error_types.get(details.category, ObjectStorageError)
	return error_type(message, code=details.code, operation=operation)
