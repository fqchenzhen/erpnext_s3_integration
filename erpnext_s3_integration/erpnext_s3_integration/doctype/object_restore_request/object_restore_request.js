frappe.ui.form.on("Object Restore Request", {
	refresh(frm) {
		if (frm.doc.status === "Failed" && frappe.user.has_role("System Manager")) {
			frm.add_custom_button(__("Retry"), () => call_action(frm, "retry_restore_request"));
		}
		if (["Requested", "Restoring", "Ready"].includes(frm.doc.status) && frappe.user.has_role("System Manager")) {
			frm.add_custom_button(__("Refresh Now"), () => call_action(frm, "refresh_restore_request"));
		}
		if (frm.doc.status === "Requested" && !frm.doc.provider_submitted) {
			frm.add_custom_button(__("Cancel"), () => call_action(frm, "cancel_restore_request"));
		}
	},
});

function call_action(frm, action) {
	frappe.call({
		method: `erpnext_s3_integration.erpnext_s3_integration.doctype.object_restore_request.object_restore_request.${action}`,
		type: "POST",
		args: { request_name: frm.doc.name },
		callback: () => frm.reload_doc(),
	});
}
