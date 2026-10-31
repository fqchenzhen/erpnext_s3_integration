import frappe
from frappe.custom.doctype.custom_field.custom_field import create_custom_fields

RECOMMENDED_ATTACHMENT_LIFECYCLE = (30, 90, 0, 365)


def after_migrate() -> None:
	create_custom_fields(_custom_fields(), update=True)
	_ensure_file_field_permissions()
	_seed_missing_settings_defaults()
	_enforce_security_defaults()
	_seed_default_rules()


def _seed_missing_settings_defaults() -> None:
	stored_values = frappe.db.get_singles_dict("Object Storage Settings")
	# Existing Single DocTypes do not inherit defaults for fields added in later migrations.
	missing_defaults = {
		field.fieldname: field.default
		for field in frappe.get_meta("Object Storage Settings").fields
		if field.default is not None and field.fieldname not in stored_values
	}
	if missing_defaults:
		frappe.db.set_single_value(
			"Object Storage Settings",
			missing_defaults,
			update_modified=False,
		)


def _enforce_security_defaults() -> None:
	frappe.db.set_single_value("Object Storage Settings", "proxy_downloads", 1, update_modified=False)


def _seed_default_rules() -> None:
	from erpnext_s3_integration.object_storage.default_rules import sync_default_rules

	settings = frappe.get_single("Object Storage Settings")
	if sync_default_rules(settings):
		settings.retention_rules_reviewed = 0
		settings.save(ignore_permissions=True)


def _ensure_file_field_permissions() -> None:
	from frappe.permissions import add_permission, update_permission_property

	for role, permlevel, can_write in (
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
				"fieldname": "object_storage_source_url",
				"fieldtype": "Data",
				"length": 1000,
				"insert_after": "object_storage_key",
				"label": "Migrated Local URL",
				"read_only": 1,
				"no_copy": 1,
				"permlevel": 2,
			},
			{
				"fieldname": "object_storage_migrated_at",
				"fieldtype": "Datetime",
				"insert_after": "object_storage_source_url",
				"label": "Migrated to Object Storage At",
				"read_only": 1,
				"no_copy": 1,
				"permlevel": 2,
			},
			{
				"fieldname": "object_storage_local_cleaned_at",
				"fieldtype": "Datetime",
				"insert_after": "object_storage_migrated_at",
				"label": "Local Copy Cleaned At",
				"read_only": 1,
				"no_copy": 1,
				"permlevel": 2,
			},
			{
				"fieldname": "retention_override",
				"fieldtype": "Select",
				"options": "\npermanent-hot\nbusiness-online\narchive-keep\narchive-delete\nunclassified",
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
