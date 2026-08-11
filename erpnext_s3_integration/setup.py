import frappe
from frappe.custom.doctype.custom_field.custom_field import create_custom_fields


def after_migrate() -> None:
	_create_roles()
	create_custom_fields(_custom_fields(), update=True)
	_ensure_file_field_permissions()
	_enforce_security_defaults()
	_migrate_recommended_retention_defaults()
	_seed_default_rules()
	_seed_restore_role()


def _enforce_security_defaults() -> None:
	frappe.db.set_single_value(
		"Object Storage Settings", "proxy_downloads", 1, update_modified=False
	)


def _migrate_recommended_retention_defaults() -> None:
	legacy = tuple(
		int(frappe.db.get_single_value("Object Storage Settings", fieldname) or 0)
		for fieldname in (
			"attachment_lifecycle_ia_days",
			"attachment_lifecycle_archive_days",
			"attachment_lifecycle_cold_archive_days",
			"attachment_lifecycle_delete_days",
		)
	)
	if legacy != (30, 90, 365, 0):
		return
	frappe.db.set_single_value(
		"Object Storage Settings",
		{
			"attachment_lifecycle_ia_days": 30,
			"attachment_lifecycle_archive_days": 365,
			"attachment_lifecycle_cold_archive_days": 0,
			"attachment_lifecycle_delete_days": 0,
			"attachment_lifecycle_reviewed": 0,
		},
		update_modified=False,
	)


def _create_roles() -> None:
	for role_name in ("Object Storage Manager", "Object Storage Restore User"):
		if not frappe.db.exists("Role", role_name):
			frappe.get_doc({"doctype": "Role", "role_name": role_name}).insert(ignore_permissions=True)


def _seed_restore_role() -> None:
	settings = frappe.get_single("Object Storage Settings")
	if not settings.restore_roles:
		settings.append("restore_roles", {"role": "Object Storage Restore User"})
		settings.save(ignore_permissions=True)


def _seed_default_rules() -> None:
	from erpnext_s3_integration.object_storage.default_rules import sync_default_rules

	settings = frappe.get_single("Object Storage Settings")
	if sync_default_rules(settings):
		settings.retention_rules_reviewed = 0
		settings.save(ignore_permissions=True)


def _ensure_file_field_permissions() -> None:
	from frappe.permissions import add_permission, update_permission_property

	for role, permlevel, can_write in (
		("Object Storage Manager", 1, 1),
		("System Manager", 1, 1),
		("System Manager", 2, 0),
	):
		filters = {"parent": "File", "role": role, "permlevel": permlevel, "if_owner": 0}
		if not frappe.db.exists("Custom DocPerm", filters):
			add_permission("File", role, permlevel, "read")
		update_permission_property("File", role, permlevel, "read", 1, validate=False)
		update_permission_property("File", role, permlevel, "write", can_write, validate=False)
	frappe.clear_cache(doctype="File")


def _custom_fields() -> dict:
	return {
		"File": [
			{
				"fieldname": "object_storage_section",
				"fieldtype": "Section Break",
				"insert_after": "content_hash",
				"label": "Object Storage",
				"collapsible": 1,
			},
			{
				"fieldname": "object_storage_profile",
				"fieldtype": "Link",
				"options": "Object Storage Profile",
				"insert_after": "object_storage_section",
				"label": "Object Storage Profile",
				"read_only": 1,
				"no_copy": 1,
				"permlevel": 2,
			},
			{
				"fieldname": "object_storage_key",
				"fieldtype": "Data",
				"insert_after": "object_storage_profile",
				"label": "Object Storage Key",
				"read_only": 1,
				"no_copy": 1,
				"permlevel": 2,
			},
			{
				"fieldname": "retention_override",
				"fieldtype": "Select",
				"options": "\npermanent-hot\nbusiness-online\nbusiness-archive\nunclassified",
				"insert_after": "object_storage_key",
				"label": "Retention Override",
				"permlevel": 1,
			},
			{
				"fieldname": "retention_override_by",
				"fieldtype": "Link",
				"options": "User",
				"insert_after": "retention_override",
				"label": "Retention Override By",
				"read_only": 1,
				"permlevel": 1,
			},
			{
				"fieldname": "retention_override_at",
				"fieldtype": "Datetime",
				"insert_after": "retention_override_by",
				"label": "Retention Override At",
				"read_only": 1,
				"permlevel": 1,
			},
			{
				"fieldname": "effective_retention_policy",
				"fieldtype": "Data",
				"insert_after": "retention_override_at",
				"label": "Effective Retention Policy",
				"read_only": 1,
			},
			{
				"fieldname": "retention_category",
				"fieldtype": "Data",
				"insert_after": "effective_retention_policy",
				"label": "Retention Category",
				"read_only": 1,
			},
			{
				"fieldname": "retention_source",
				"fieldtype": "Data",
				"insert_after": "retention_category",
				"label": "Retention Source",
				"read_only": 1,
			},
			{
				"fieldname": "object_tag_status",
				"fieldtype": "Select",
				"options": "\nPending\nPassed\nFailed",
				"insert_after": "retention_source",
				"label": "Object Tag Status",
				"read_only": 1,
			},
			{
				"fieldname": "object_tag_error",
				"fieldtype": "Small Text",
				"insert_after": "object_tag_status",
				"label": "Object Tag Error",
				"read_only": 1,
			},
			{
				"fieldname": "object_tagged_at",
				"fieldtype": "Datetime",
				"insert_after": "object_tag_error",
				"label": "Object Tagged At",
				"read_only": 1,
			},
		],
	}
