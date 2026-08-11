$(document).on("click", "a[href^='/s3/']", function (event) {
	if (event.isDefaultPrevented()) return;
	const href = $(this).attr("href");
	const key = decodeURIComponent(href.split("?")[0].slice(4));
	if (!key) return;
	event.preventDefault();
	show_object_storage_status(key, href);
});

window.show_object_storage_status = function (key, download_url = null) {
	frappe.call({
		method: "erpnext_s3_integration.api.get_file_status",
		type: "GET",
		args: { key },
		callback: ({ message }) => {
			if (message.download_ready) {
				window.location.assign(download_url || `/s3/${encodeURI(key)}`);
				return;
			}
			show_restore_dialog(message);
		},
	});
};

function show_restore_dialog(status) {
	const active = status.restore_request;
	const dialog = new frappe.ui.Dialog({
		title: __("Archived attachment requires restore"),
		fields: [{
			fieldname: "message",
			fieldtype: "HTML",
			options: `<div class="alert alert-warning">
				<p><strong>${escape_html(status.file_name)}</strong></p>
				<p>${__("Storage Class")}: ${escape_html(status.storage_class)}</p>
				<p>${escape_html(status.estimated_time)}</p>
				<p>${__("Restoring creates a temporary readable copy and may incur provider request and retrieval charges.")}</p>
				${active ? `<p>${__("Active request")}: <strong>${escape_html(active.name)}</strong> · ${__(active.status)}</p>` : ""}
			</div>`,
		}],
		primary_action_label: active ? __("Open Restore Request") : status.can_request_restore ? __("Request Restore") : __("Close"),
		primary_action() {
			if (active) {
				frappe.set_route("Form", "Object Restore Request", active.name);
				dialog.hide();
				return;
			}
			if (!status.can_request_restore) {
				dialog.hide();
				return;
			}
			frappe.call({
				method: "erpnext_s3_integration.erpnext_s3_integration.doctype.object_restore_request.object_restore_request.create_restore_request",
				type: "POST",
				args: { file_name: status.file },
				callback: ({ message }) => frappe.set_route("Form", "Object Restore Request", message),
			});
			dialog.hide();
		},
	});
	dialog.show();
}

function escape_html(value) {
	return frappe.utils.escape_html(String(value || ""));
}
