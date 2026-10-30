from unittest.mock import patch

import frappe
from frappe.tests import IntegrationTestCase

from erpnext_s3_integration.object_storage.types import ObjectInfo


def _deny_file_read_for_test(doc, ptype=None, user=None, debug=False):
	return False


class IntegrationTestObjectRestoreRequest(IntegrationTestCase):
	def setUp(self):
		self.profile_name = f"Test Restore {frappe.generate_hash(length=8)}"
		settings = frappe.get_single("Object Storage Settings")
		settings.enable_attachment_storage = 0
		settings.enable_backup_storage = 0
		settings.enable_archive_restore = 1
		settings.restore_settings_reviewed = 1
		settings.enable_auto_classification = 0
		settings.save()
		frappe.get_doc(
			{
				"doctype": "Object Storage Profile",
				"profile_name": self.profile_name,
				"provider": "Alibaba Cloud OSS",
				"purpose": "Attachments",
				"environment": "Development",
				"credential_mode": "ECS Instance RAM Role",
				"region": "ap-southeast-5",
				"use_internal_endpoint": 1,
				"bucket": "restore-test-bucket",
			}
		).insert()
		self.file = frappe.get_doc(
			{
				"doctype": "File",
				"file_name": "archived-test.txt",
				"file_url": f"/s3/test/{frappe.generate_hash(length=12)}",
				"is_private": 0,
				"object_storage_profile": self.profile_name,
				"object_storage_key": f"test/{frappe.generate_hash(length=20)}",
			}
		)
		self.file.flags.copy_from_existing_file = True
		self.file.insert(ignore_permissions=True)

	def _set_restore_access(self, roles, allow_self_service=True):
		settings = frappe.get_single("Object Storage Settings")
		settings.allow_self_service_restore = int(allow_self_service)
		settings.set("restore_roles", [])
		for role in roles:
			settings.append("restore_roles", {"role": role})
		settings.save()

	def _new_user(self, role):
		user = frappe.get_doc(
			{
				"doctype": "User",
				"email": f"restore-{frappe.generate_hash(length=10)}@example.test",
				"first_name": "Restore Request Test",
				"user_type": "System User",
				"send_welcome_email": 0,
				"roles": [{"role": role}],
			}
		).insert(ignore_permissions=True)
		return user.name

	def _insert_restore_request(self, file_name):
		with patch("erpnext_s3_integration.object_storage.service.ObjectStorageService") as service_class:
			service_class.return_value.backend.head.return_value = ObjectInfo(
				self.file.object_storage_key, storage_class="Archive"
			)
			return frappe.get_doc({"doctype": "Object Restore Request", "file": file_name}).insert()

	def _new_private_object_file(self):
		file_doc = frappe.get_doc(
			{
				"doctype": "File",
				"file_name": f"private-{frappe.generate_hash(length=8)}.txt",
				"file_url": f"/s3/private-test/{frappe.generate_hash(length=12)}.txt",
				"is_private": 1,
				"object_storage_profile": self.profile_name,
				"object_storage_key": f"test/{frappe.generate_hash(length=20)}",
			}
		)
		file_doc.flags.copy_from_existing_file = True
		file_doc.insert(ignore_permissions=True)
		return file_doc

	def test_allowed_business_role_creates_request_through_document_insert(self):
		self._set_restore_access(["Stock Manager"])
		user = self._new_user("Stock Manager")
		previous_user = frappe.session.user
		frappe.set_user(user)
		try:
			doc = self._insert_restore_request(self.file.name)
		finally:
			frappe.set_user(previous_user)

		self.assertEqual(doc.requested_by, user)
		self.assertEqual(doc.status, "Requested")

	def test_business_role_requires_allowlist_and_self_service(self):
		user = self._new_user("Stock Manager")
		self._set_restore_access([], allow_self_service=True)
		previous_user = frappe.session.user
		frappe.set_user(user)
		try:
			with self.assertRaises(frappe.PermissionError):
				frappe.get_doc({"doctype": "Object Restore Request", "file": self.file.name}).insert()
		finally:
			frappe.set_user(previous_user)

		self._set_restore_access(["Stock Manager"], allow_self_service=False)
		frappe.set_user(user)
		try:
			with self.assertRaises(frappe.PermissionError):
				frappe.get_doc({"doctype": "Object Restore Request", "file": self.file.name}).insert()
		finally:
			frappe.set_user(previous_user)

	def test_business_role_cannot_restore_file_without_read_permission(self):
		self._set_restore_access(["Stock Manager"])
		private_file = frappe.get_doc(
			{
				"doctype": "File",
				"file_name": "private-archived-test.txt",
				"file_url": f"/s3/private-test/{frappe.generate_hash(length=12)}.txt",
				"is_private": 1,
				"object_storage_profile": self.profile_name,
				"object_storage_key": f"test/{frappe.generate_hash(length=20)}",
			}
		)
		private_file.flags.copy_from_existing_file = True
		private_file.insert(ignore_permissions=True)
		user = self._new_user("Stock Manager")
		previous_user = frappe.session.user
		frappe.set_user(user)
		try:
			with patch("erpnext_s3_integration.object_storage.service.ObjectStorageService") as service_class:
				with self.assertRaises(frappe.PermissionError):
					frappe.get_doc({"doctype": "Object Restore Request", "file": private_file.name}).insert()
			service_class.assert_not_called()
		finally:
			frappe.set_user(previous_user)

	def test_system_manager_can_create_without_self_service_configuration(self):
		self._set_restore_access([], allow_self_service=False)
		user = self._new_user("System Manager")
		previous_user = frappe.session.user
		frappe.set_user(user)
		try:
			doc = self._insert_restore_request(self.file.name)
		finally:
			frappe.set_user(previous_user)

		self.assertEqual(doc.requested_by, user)

	def _get_hooks_with_external_file_denial(self, hook=None, *args, **kwargs):
		hooks = self._original_get_hooks(hook, *args, **kwargs)
		if hook != "has_permission":
			return hooks

		hooks = {**hooks}
		hooks["File"] = [
			*hooks.get("File", []),
			"erpnext_s3_integration.tests.test_restore_integration._deny_file_read_for_test",
		]
		return hooks

	def test_guest_can_download_public_object_through_permission_hooks(self):
		from erpnext_s3_integration.api import _authorized_file

		previous_user = frappe.session.user
		frappe.set_user("Guest")
		try:
			self.assertTrue(self.file.is_downloadable())
			self.assertEqual(_authorized_file(self.file.object_storage_key).name, self.file.name)
		finally:
			frappe.set_user(previous_user)

	def test_owner_can_download_private_object_through_full_permission_chain(self):
		from erpnext_s3_integration.api import _authorized_file

		user = self._new_user("Stock Manager")
		previous_user = frappe.session.user
		frappe.set_user(user)
		try:
			private_file = self._new_private_object_file()
			self.assertTrue(private_file.is_downloadable())
			self.assertEqual(_authorized_file(private_file.object_storage_key).name, private_file.name)
		finally:
			frappe.set_user(previous_user)

	def test_guest_cannot_download_private_object_through_file_controller(self):
		from erpnext_s3_integration.api import _authorized_file

		private_file = self._new_private_object_file()
		previous_user = frappe.session.user
		frappe.set_user("Guest")
		try:
			self.assertFalse(private_file.is_downloadable())
			with self.assertRaises(frappe.PermissionError):
				_authorized_file(private_file.object_storage_key)
		finally:
			frappe.set_user(previous_user)

	def test_guest_public_download_runs_external_file_permission_hooks(self):
		from erpnext_s3_integration.api import _authorized_file

		previous_user = frappe.session.user
		frappe.set_user("Guest")
		self._original_get_hooks = frappe.get_hooks
		try:
			self.assertTrue(self.file.is_downloadable())
			with patch.object(frappe, "get_hooks", side_effect=self._get_hooks_with_external_file_denial):
				with self.assertRaises(frappe.PermissionError):
					_authorized_file(self.file.object_storage_key)
		finally:
			frappe.set_user(previous_user)
			del self._original_get_hooks

	def test_logged_in_download_runs_external_file_permission_hooks(self):
		from erpnext_s3_integration.api import _authorized_file

		user = self._new_user("Stock Manager")
		previous_user = frappe.session.user
		frappe.set_user(user)
		self._original_get_hooks = frappe.get_hooks
		try:
			self.assertTrue(self.file.is_downloadable())
			with patch.object(frappe, "get_hooks", side_effect=self._get_hooks_with_external_file_denial):
				with self.assertRaises(frappe.PermissionError):
					_authorized_file(self.file.object_storage_key)
		finally:
			frappe.set_user(previous_user)
			del self._original_get_hooks

	def test_request_initializes_and_can_cancel_before_submission(self):
		previous_user = frappe.session.user
		frappe.set_user("Administrator")
		try:
			with patch("erpnext_s3_integration.object_storage.service.ObjectStorageService") as service_class:
				service_class.return_value.backend.head.return_value = ObjectInfo(
					self.file.object_storage_key, storage_class="Archive"
				)
				doc = frappe.get_doc({"doctype": "Object Restore Request", "file": self.file.name}).insert()
			doc.status = "Cancelled"
			doc.save()
		finally:
			frappe.set_user(previous_user)

		self.assertEqual(doc.status, "Cancelled")
		self.assertEqual(doc.requested_by, "Administrator")

	def test_duplicate_active_request_is_rejected(self):
		with patch("erpnext_s3_integration.object_storage.service.ObjectStorageService") as service_class:
			service_class.return_value.backend.head.return_value = ObjectInfo(
				self.file.object_storage_key, storage_class="Archive"
			)
			frappe.get_doc({"doctype": "Object Restore Request", "file": self.file.name}).insert()
			with self.assertRaises(frappe.ValidationError):
				frappe.get_doc({"doctype": "Object Restore Request", "file": self.file.name}).insert()

	def test_online_attachment_does_not_create_restore_request(self):
		with patch("erpnext_s3_integration.object_storage.service.ObjectStorageService") as service_class:
			service_class.return_value.backend.head.return_value = ObjectInfo(
				self.file.object_storage_key, storage_class="Standard"
			)
			with self.assertRaises(frappe.ValidationError):
				frappe.get_doc({"doctype": "Object Restore Request", "file": self.file.name}).insert()

	def test_disabled_archive_restore_rejects_request_before_object_lookup(self):
		settings = frappe.get_single("Object Storage Settings")
		settings.enable_archive_restore = 0
		settings.save()

		with patch("erpnext_s3_integration.object_storage.service.ObjectStorageService") as service_class:
			with self.assertRaises(frappe.ValidationError):
				frappe.get_doc({"doctype": "Object Restore Request", "file": self.file.name}).insert()

		service_class.assert_not_called()

	def test_request_requires_file_read_permission(self):
		with (
			patch.object(frappe, "has_permission", return_value=False) as has_permission,
			patch("erpnext_s3_integration.object_storage.service.ObjectStorageService") as service_class,
		):
			with self.assertRaises(frappe.PermissionError):
				frappe.get_doc({"doctype": "Object Restore Request", "file": self.file.name}).insert()

		has_permission.assert_called_once()
		doctype, permission_type = has_permission.call_args.args
		self.assertEqual((doctype, permission_type), ("File", "read"))
		self.assertEqual(has_permission.call_args.kwargs["doc"].name, self.file.name)
		self.assertEqual(has_permission.call_args.kwargs["user"], frappe.session.user)
		service_class.assert_not_called()
