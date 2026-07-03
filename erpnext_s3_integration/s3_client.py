import frappe
from frappe import _
from frappe.utils.password import get_decrypted_password

AUTH_ERROR_CODES = {
	"AccessDenied",
	"AuthorizationHeaderMalformed",
	"CredentialRetrievalError",
	"ExpiredToken",
	"InvalidAccessKeyId",
	"InvalidClientTokenId",
	"InvalidToken",
	"NoCredentialsError",
	"SignatureDoesNotMatch",
}

BUCKET_OR_KEY_ERROR_CODES = {
	"IllegalLocationConstraintException",
	"InvalidBucketName",
	"NoSuchBucket",
	"NoSuchKey",
	"PermanentRedirect",
}

THROTTLING_ERROR_CODES = {
	"RequestLimitExceeded",
	"SlowDown",
	"ThrottledException",
	"Throttling",
	"ThrottlingException",
	"TooManyRequestsException",
}

NETWORK_TIMEOUT_CODES = {"RequestTimeout", "RequestTimeoutException", "RequestExpired"}


def _load_boto3():
	"""Import boto3 lazily so Desk boot is not blocked by optional S3 dependencies."""
	try:
		import boto3
		import botocore.exceptions as botocore_exceptions
	except Exception:
		frappe.throw(
			_(
				"S3 dependencies could not be loaded. Please verify the boto3/OpenSSL environment before using S3 features."
			),
			exc=frappe.ValidationError,
		)

	return boto3, botocore_exceptions


class S3OperationError(frappe.ValidationError):
	def __init__(
		self,
		message,
		category="unknown",
		error_code=None,
		retryable=False,
		original_exception=None,
	):
		super().__init__(message)
		self.category = category
		self.error_code = error_code
		self.retryable = retryable
		self.original_exception = original_exception


def classify_s3_exception(exc):
	"""Return structured diagnostics for S3, network, and local file errors."""
	try:
		_, botocore_exceptions = _load_boto3()
	except Exception:
		botocore_exceptions = None

	if isinstance(exc, S3OperationError):
		return {
			"category": exc.category,
			"error_code": exc.error_code,
			"retryable": exc.retryable,
		}

	if isinstance(exc, (FileNotFoundError, IsADirectoryError, PermissionError)):
		return {"category": "local_file", "error_code": exc.__class__.__name__, "retryable": False}

	if botocore_exceptions:
		timeout_errors = (
			botocore_exceptions.ConnectTimeoutError,
			botocore_exceptions.ReadTimeoutError,
		)
		connection_errors = (
			botocore_exceptions.ConnectionClosedError,
			botocore_exceptions.EndpointConnectionError,
		)
		credential_errors = (
			botocore_exceptions.CredentialRetrievalError,
			botocore_exceptions.NoCredentialsError,
			botocore_exceptions.PartialCredentialsError,
		)

		if isinstance(exc, timeout_errors):
			return {
				"category": "network_timeout",
				"error_code": exc.__class__.__name__,
				"retryable": True,
			}
		if isinstance(exc, connection_errors):
			return {
				"category": "network_connection",
				"error_code": exc.__class__.__name__,
				"retryable": True,
			}
		if isinstance(exc, credential_errors):
			return {
				"category": "auth_permission",
				"error_code": exc.__class__.__name__,
				"retryable": False,
			}

		if isinstance(exc, botocore_exceptions.ClientError):
			response = exc.response or {}
			error = response.get("Error") or {}
			code = str(error.get("Code") or "ClientError")
			status_code = (response.get("ResponseMetadata") or {}).get("HTTPStatusCode")

			if code in AUTH_ERROR_CODES or status_code in (401, 403):
				return {"category": "auth_permission", "error_code": code, "retryable": False}
			if code in BUCKET_OR_KEY_ERROR_CODES or status_code == 404:
				return {"category": "bucket_or_key", "error_code": code, "retryable": False}
			if code in THROTTLING_ERROR_CODES or status_code == 429:
				return {"category": "throttling", "error_code": code, "retryable": True}
			if code in NETWORK_TIMEOUT_CODES or status_code == 408:
				return {"category": "network_timeout", "error_code": code, "retryable": True}
			if status_code and status_code >= 500:
				return {"category": "s3_service", "error_code": code, "retryable": True}

			return {"category": "unknown", "error_code": code, "retryable": False}

	return {"category": "unknown", "error_code": exc.__class__.__name__, "retryable": False}


class S3Client:
	def __init__(self):
		self.settings = frappe.get_single("S3 Integration Settings")
		self._client = None
		self.setup_client()

	@property
	def client(self):
		"""Expose the underlying boto3 client for internal callers like backup cleanup."""
		return self._client

	def get_password(self, fieldname):
		if not self.settings.get(fieldname):
			return None
		try:
			return get_decrypted_password("S3 Integration Settings", "S3 Integration Settings", fieldname)
		except frappe.exceptions.SecretNotFoundError:
			return self.settings.get(fieldname)
		except Exception:
			return self.settings.get(fieldname)

	def get_addressing_style(self):
		addressing_style = (self.settings.get("addressing_style") or "").strip().lower()
		if self.settings.get("use_path_style") and addressing_style in {"", "auto"}:
			return "path"
		if addressing_style in {"auto", "virtual", "path"}:
			return addressing_style

		return "auto"

	def setup_client(self):
		boto3, _ = _load_boto3()
		aws_access_key_id = self.settings.aws_access_key_id
		aws_secret_access_key = self.get_password("aws_secret_access_key")
		region_name = self.settings.region_name
		endpoint_url = self.settings.endpoint_url

		if not (aws_access_key_id and aws_secret_access_key):
			frappe.throw(_("AWS Credentials are required to initialize the S3 client."))

		self.bucket_name = self.settings.bucket_name
		if not self.bucket_name:
			frappe.throw(_("AWS Bucket Name is required."))

		config = boto3.session.Config(
			signature_version="s3v4",
			connect_timeout=10,
			read_timeout=60,
			retries={"mode": "standard", "total_max_attempts": 4},
			s3={"addressing_style": self.get_addressing_style()},
		)

		client_kwargs = {
			"service_name": "s3",
			"aws_access_key_id": aws_access_key_id,
			"aws_secret_access_key": aws_secret_access_key,
			"config": config,
		}

		if region_name:
			client_kwargs["region_name"] = region_name
		if endpoint_url:
			client_kwargs["endpoint_url"] = endpoint_url

		self._client = boto3.client(**client_kwargs)

	def _operation_error(self, exc, action, key=None):
		details = classify_s3_exception(exc)
		target = f" for {key}" if key else ""
		message = f"S3 {action} failed{target}: {exc}"
		return S3OperationError(
			message,
			category=details["category"],
			error_code=details["error_code"],
			retryable=details["retryable"],
			original_exception=exc,
		)

	def _raise_operation_error(self, exc, action, key=None):
		error = self._operation_error(exc, action, key)
		frappe.log_error(message=frappe.get_traceback(), title=f"S3 {action} Failed")
		raise error

	def test_connection(self):
		try:
			self._client.list_objects_v2(Bucket=self.bucket_name, MaxKeys=1)
			return True, "Connection successful! Bucket is accessible."
		except Exception as e:
			error = self._operation_error(e, "Test Connection")
			frappe.log_error(message=frappe.get_traceback(), title="S3 Test Connection Failed")
			return (
				False,
				f"Connection failed [{error.category}/{error.error_code}]: {e}",
			)

	def upload_fileobj(self, fileobj, key, content_type=None, is_public=False):
		_, botocore_exceptions = _load_boto3()
		extra_args = {}
		if content_type:
			extra_args["ContentType"] = content_type
		if is_public:
			extra_args["ACL"] = "public-read"

		try:
			self._client.upload_fileobj(fileobj, self.bucket_name, key, ExtraArgs=extra_args)
			return True
		except botocore_exceptions.ClientError as e:
			error_code = (e.response or {}).get("Error", {}).get("Code")
			if error_code == "AccessControlListNotSupported" and "ACL" in extra_args:
				try:
					fileobj.seek(0)
					extra_args.pop("ACL", None)
					self._client.upload_fileobj(fileobj, self.bucket_name, key, ExtraArgs=extra_args)
					return True
				except Exception as retry_exc:
					self._raise_operation_error(retry_exc, "Upload", key)

			self._raise_operation_error(e, "Upload", key)
		except Exception as e:
			self._raise_operation_error(e, "Upload", key)

	def delete_object(self, key, raise_on_error=False):
		try:
			self._client.delete_object(Bucket=self.bucket_name, Key=key)
			return True
		except Exception as e:
			error = self._operation_error(e, "Delete", key)
			frappe.log_error(message=frappe.get_traceback(), title=f"S3 Delete Failed for {key}")
			if raise_on_error:
				raise error
			return False

	def generate_presigned_url(self, key, expires_in=3600):
		try:
			url = self._client.generate_presigned_url(
				"get_object",
				Params={"Bucket": self.bucket_name, "Key": key},
				ExpiresIn=expires_in,
			)
			return url
		except Exception as e:
			error = self._operation_error(e, "Generate URL", key)
			frappe.log_error(
				message=f"{error}\n\n{frappe.get_traceback()}",
				title=f"S3 URL Generation Failed for {key}",
			)
			return None

	def download_as_stream(self, key):
		try:
			response = self._client.get_object(Bucket=self.bucket_name, Key=key)
			return response["Body"]
		except Exception as e:
			self._raise_operation_error(e, "Download", key)
