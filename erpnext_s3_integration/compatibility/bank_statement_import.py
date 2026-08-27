import frappe

from erpnext_s3_integration.storage_policy import storage_context


@frappe.whitelist(methods=["POST"])
def convert_mt940_to_csv(data_import, mt940_file_path):
	from erpnext.accounts.doctype.bank_statement_import.bank_statement_import import (
		convert_mt940_to_csv as standard_convert_mt940_to_csv,
	)

	with storage_context("Bank Statement Import"):
		return standard_convert_mt940_to_csv(data_import, mt940_file_path)
