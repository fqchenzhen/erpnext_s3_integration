frappe.ui.form.on("S3 Integration Settings", {
	refresh(frm) {
		frm.trigger("set_status_color");

		if (!frm.is_dirty()) {
			frm.add_custom_button(__("Test Bucket Access"), function () {
				frm.trigger("test_connection");
			});
		}
	},

	provider(frm) {
		const addressing_by_provider = {
			"AWS S3": "auto",
			"Alibaba Cloud OSS": "virtual",
			MinIO: "path",
		};
		const addressing_style = addressing_by_provider[frm.doc.provider];
		if (addressing_style) {
			frm.set_value("addressing_style", addressing_style);
		}
		if (frm.doc.provider === "Alibaba Cloud OSS") {
			frm.set_value("use_path_style", 0);
		}
		mark_connection_untested(frm);
	},

	aws_access_key_id: mark_connection_untested,
	aws_secret_access_key: mark_connection_untested,
	region_name: mark_connection_untested,
	bucket_name: mark_connection_untested,
	endpoint_url: mark_connection_untested,
	addressing_style: mark_connection_untested,
	use_path_style: mark_connection_untested,

	after_save(frm) {
		frm.trigger("set_status_color");
	},

	set_status_color(frm) {
		frm.page.clear_indicator();
		if (!frm.doc.status) return;

		if (["Configured & Connected", "Bucket Access Verified"].includes(frm.doc.status)) {
			frm.page.set_indicator(frm.doc.status, "green");
		} else if (frm.doc.status === "Not Tested") {
			frm.page.set_indicator(frm.doc.status, "orange");
		} else {
			frm.page.set_indicator(frm.doc.status, "red");
		}
	},

	test_connection(frm) {
		if (frm.is_dirty()) {
			frappe.msgprint(__("Please save the document before testing the connection."));
			return;
		}

		frappe.call({
			method: "erpnext_s3_integration.erpnext_s3_integration.doctype.s3_integration_settings.s3_integration_settings.test_s3_connection",
			freeze: true,
			freeze_message: __("Testing bucket access..."),
			callback: function (r) {
				if (r.message) {
					if (r.message.success) {
						frappe.msgprint({
							title: __("Success"),
							indicator: "green",
							message: r.message.message,
						});
						frm.set_value("status", "Bucket Access Verified");
					} else {
						frappe.msgprint({
							title: __("Connection Failed"),
							indicator: "red",
							message: r.message.message,
						});
						frm.set_value("status", "Misconfigured");
					}
					frm.save().then(() => {
						frm.trigger("set_status_color");
					});
				}
			},
		});
	},

	migrate_existing_files(frm) {
		frappe.confirm(
			__(
				"Are you sure you want to start migrating existing files to S3? This process will run in the background."
			),
			() => {
				frappe.call({
					method: "erpnext_s3_integration.migration.start_migration",
					args: {
						only_unmigrated: frm.doc.migrate_only_unmigrated,
					},
					callback: function (r) {
						if (!r.exc) {
							frappe.show_alert({
								message: __(r.message),
								indicator: "green",
							});
						}
					},
				});
			}
		);
	},

	take_backup_and_sync(frm) {
		frappe.confirm(
			__(
				"Are you sure you want to take a new backup and sync it to S3? This process will run in the background."
			),
			() => {
				frappe.call({
					method: "erpnext_s3_integration.erpnext_s3_integration.doctype.s3_integration_settings.s3_integration_settings.take_backup_and_sync",
					callback: function (r) {
						if (!r.exc) {
							frappe.show_alert({
								message: __(r.message),
								indicator: "green",
							});
						}
					},
				});
			}
		);
	},
});

function mark_connection_untested(frm) {
	if (!frm.is_new() && frm.doc.status !== "Not Tested") {
		frm.set_value("status", "Not Tested");
	}
}
