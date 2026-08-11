import frappe


def can_open_object_storage_app() -> bool:
	return "System Manager" in frappe.get_roles()
