frappe.ui.form.on("File", {
	refresh(frm) {
		if (!frm.doc.object_storage_key) return;
		frm.remove_custom_button(__("Optimize"));
		frm.add_custom_button(__("Check Download / Restore"), () => {
			window.show_object_storage_status(frm.doc.object_storage_key, frm.doc.file_url);
		}, __("Object Storage"));
	},
});
