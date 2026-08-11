from dataclasses import asdict, dataclass

import frappe
from frappe import _


@dataclass(frozen=True)
class DefaultRule:
	key: str
	name: str
	policy: str
	category: str
	doctype: str
	fieldname: str | None = None
	priority: int = 100


DEFAULT_RULES = (
	DefaultRule("item-main-image", "Item main image stays hot", "permanent-hot", "master-image", "Item", "image", 10),
	DefaultRule("user-image", "User image stays hot", "permanent-hot", "identity-image", "User", "user_image", 10),
	DefaultRule("employee-image", "Employee image stays hot", "permanent-hot", "identity-image", "Employee", "image", 10),
	DefaultRule("company-logo", "Company logo stays hot", "permanent-hot", "brand-image", "Company", "company_logo", 10),
	DefaultRule("item-documents", "Item documents stay online", "business-online", "master-data", "Item", priority=100),
	DefaultRule("bom-documents", "BOM documents stay online", "business-online", "manufacturing", "BOM", priority=100),
	DefaultRule("sales-invoice", "Sales Invoice archive", "business-archive", "transaction", "Sales Invoice"),
	DefaultRule("purchase-invoice", "Purchase Invoice archive", "business-archive", "transaction", "Purchase Invoice"),
	DefaultRule("sales-order", "Sales Order archive", "business-archive", "transaction", "Sales Order"),
	DefaultRule("purchase-order", "Purchase Order archive", "business-archive", "transaction", "Purchase Order"),
	DefaultRule("delivery-note", "Delivery Note archive", "business-archive", "transaction", "Delivery Note"),
	DefaultRule("purchase-receipt", "Purchase Receipt archive", "business-archive", "transaction", "Purchase Receipt"),
	DefaultRule("payment-entry", "Payment Entry archive", "business-archive", "accounting", "Payment Entry"),
	DefaultRule("journal-entry", "Journal Entry archive", "business-archive", "accounting", "Journal Entry"),
	DefaultRule("stock-entry", "Stock Entry archive", "business-archive", "stock", "Stock Entry"),
	DefaultRule("quality-inspection", "Quality Inspection archive", "business-archive", "quality", "Quality Inspection"),
	DefaultRule("expense-claim", "Expense Claim archive", "business-archive", "expense", "Expense Claim"),
)


def sync_default_rules(settings=None) -> bool:
	settings = settings or frappe.get_single("Object Storage Settings")
	existing = {row.default_rule_key: row for row in settings.retention_rules if row.default_rule_key}
	changed = False
	for rule in DEFAULT_RULES:
		if not _rule_target_exists(rule):
			continue
		row = existing.get(rule.key)
		if not row:
			row = settings.append("retention_rules", {})
			changed = True
		values = _rule_values(rule)
		for fieldname, value in values.items():
			if row.get(fieldname) != value:
				row.set(fieldname, value)
				changed = True
	return changed


@frappe.whitelist(methods=["POST"])
def reset_default_rules() -> dict:
	frappe.only_for("System Manager")
	settings = frappe.get_single("Object Storage Settings")
	changed = sync_default_rules(settings)
	settings.retention_rules_reviewed = 0
	settings.save()
	return {
		"changed": changed,
		"default_rules": sum(bool(row.default_rule_key) for row in settings.retention_rules),
		"custom_rules": sum(not row.default_rule_key for row in settings.retention_rules),
		"message": _("Built-in rules were updated. Custom rules were kept; review the result before enabling classification."),
	}


def _rule_target_exists(rule: DefaultRule) -> bool:
	if not frappe.db.exists("DocType", rule.doctype):
		return False
	return not rule.fieldname or frappe.get_meta(rule.doctype).has_field(rule.fieldname)


def _rule_values(rule: DefaultRule) -> dict:
	values = asdict(rule)
	return {
		"enabled": 1,
		"rule_name": values["name"],
		"priority": values["priority"],
		"scope": "DocType and Field" if values["fieldname"] else "DocType",
		"reference_doctype": values["doctype"],
		"reference_field": values["fieldname"],
		"retention_policy": values["policy"],
		"category": values["category"],
		"description": _("Built-in deterministic retention rule."),
		"is_default": 1,
		"default_rule_key": values["key"],
	}
