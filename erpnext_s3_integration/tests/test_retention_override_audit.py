from unittest.mock import patch

import frappe
from frappe.tests import UnitTestCase

from erpnext_s3_integration.object_storage import classification


class _FileStub:
	def __init__(self, *, retention_override=None, is_new=True, changed=True):
		self.retention_override = retention_override
		self.retention_override_by = None
		self.retention_override_at = None
		self._is_new = is_new
		self._changed = changed

	def get(self, fieldname):
		return getattr(self, fieldname, None)

	def is_new(self):
		return self._is_new

	def has_value_changed(self, fieldname):
		assert fieldname == "retention_override"
		return self._changed


class TestRetentionOverrideAudit(UnitTestCase):
	def test_new_file_without_override_does_not_require_storage_manager(self):
		file_doc = _FileStub(retention_override=None, is_new=True, changed=True)
		with (
			patch.object(classification.frappe, "get_roles") as get_roles,
			patch.object(classification.frappe, "get_single") as get_single,
		):
			classification.set_retention_override_audit(file_doc)

		get_roles.assert_not_called()
		get_single.assert_not_called()

	def test_unchanged_override_does_not_require_storage_manager(self):
		file_doc = _FileStub(retention_override="business-online", is_new=False, changed=False)
		with (
			patch.object(classification.frappe, "get_roles") as get_roles,
			patch.object(classification.frappe, "get_single") as get_single,
		):
			classification.set_retention_override_audit(file_doc)

		get_roles.assert_not_called()
		get_single.assert_not_called()

	def test_explicit_override_still_requires_privileged_role(self):
		file_doc = _FileStub(retention_override="business-online", is_new=True, changed=True)
		with patch.object(classification.frappe, "get_roles", return_value=["Sales User"]):
			with self.assertRaises(frappe.PermissionError):
				classification.set_retention_override_audit(file_doc)

	def test_storage_manager_can_set_explicit_override(self):
		file_doc = _FileStub(retention_override="business-online", is_new=True, changed=True)
		settings = frappe._dict(allow_manual_retention_override=1)
		with (
			patch.object(
				classification.frappe,
				"get_roles",
				return_value=["Object Storage Manager"],
			),
			patch.object(classification.frappe, "get_single", return_value=settings),
		):
			classification.set_retention_override_audit(file_doc)

		self.assertEqual(file_doc.retention_override_by, frappe.session.user)
		self.assertIsNotNone(file_doc.retention_override_at)

	def test_manual_override_setting_is_still_enforced(self):
		file_doc = _FileStub(retention_override="business-online", is_new=True, changed=True)
		settings = frappe._dict(allow_manual_retention_override=0)
		with (
			patch.object(classification.frappe, "get_roles", return_value=["System Manager"]),
			patch.object(classification.frappe, "get_single", return_value=settings),
		):
			with self.assertRaises(frappe.ValidationError):
				classification.set_retention_override_audit(file_doc)
