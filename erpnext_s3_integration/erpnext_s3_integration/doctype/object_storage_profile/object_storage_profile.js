frappe.ui.form.on("Object Storage Profile", {
	refresh(frm) {
		set_provider_fields(frm);
		render_profile_guidance(frm);
		frm.add_custom_button(__("Apply Recommended Jakarta Settings"), () => apply_recommended_settings(frm));

		if (!frm.is_new()) {
			frm.add_custom_button(__("Run Full Read/Write Test"), () => run_full_test(frm), __("Test"));
			if (frm.doc.provider === "Alibaba Cloud OSS") {
				frm.add_custom_button(__("Generate RAM Policy"), () => show_ram_policy(frm), __("Alibaba Cloud"));
			}
			frm.add_custom_button(__("Use This Profile in Settings"), () => use_in_settings(frm), __("Setup"));
		}
	},
	provider(frm) {
		set_provider_fields(frm);
		if (frm.doc.provider === "Alibaba Cloud OSS" && !frm.doc.region) {
			frm.set_value("region", "ap-southeast-5");
			apply_recommended_settings(frm, false);
		}
		if (frm.doc.provider === "MinIO") {
			frm.set_value("addressing_style", "Path");
			frm.set_value("credential_mode", "AccessKey");
		}
	},
	credential_mode(frm) {
		set_provider_fields(frm);
		render_profile_guidance(frm);
	},
	environment(frm) {
		if (frm.doc.provider === "Alibaba Cloud OSS") apply_recommended_settings(frm, false);
	},
	use_internal_endpoint(frm) {
		if (frm.doc.provider !== "Alibaba Cloud OSS" || frm.doc.region !== "ap-southeast-5") return;
		frm.set_value(
			"endpoint_url",
			frm.doc.use_internal_endpoint
				? "https://oss-ap-southeast-5-internal.aliyuncs.com"
				: "https://oss-ap-southeast-5.aliyuncs.com"
		);
		render_profile_guidance(frm);
	},
});

function apply_recommended_settings(frm, show_alert = true) {
	return frappe.call({
		method: "erpnext_s3_integration.erpnext_s3_integration.doctype.object_storage_profile.object_storage_profile.apply_recommended_jakarta_settings",
		type: "POST",
		args: { environment: frm.doc.environment || "Production" },
		callback: ({ message }) => {
			Object.entries(message || {}).forEach(([field, value]) => frm.set_value(field, value));
			render_profile_guidance(frm);
			if (show_alert) {
				frappe.show_alert({ message: __("Recommended Jakarta settings applied."), indicator: "green" });
			}
		},
	});
}

function set_provider_fields(frm) {
	const alibaba = frm.doc.provider === "Alibaba Cloud OSS";
	frm.toggle_display("use_internal_endpoint", alibaba);
	frm.toggle_display("ram_role_name", alibaba && frm.doc.credential_mode === "ECS Instance RAM Role");
	frm.toggle_display("addressing_style", !alibaba);
}

function render_profile_guidance(frm) {
	const wrapper = frm.get_field("profile_setup_help").$wrapper;
	if (frm.doc.provider !== "Alibaba Cloud OSS") {
		wrapper.empty();
		return;
	}
	const production = frm.doc.environment === "Production";
	const title = production ? __("Jakarta ECS production configuration") : __("Internet testing configuration");
	const detail = production
		? __("Use an ECS Instance RAM Role and the Jakarta internal endpoint. No AccessKey is stored.")
		: __("Use the Jakarta public endpoint. AccessKey is allowed only for isolated Development or Staging tests and public traffic charges may apply.");
	const endpoint = production
		? "https://oss-ap-southeast-5-internal.aliyuncs.com"
		: "https://oss-ap-southeast-5.aliyuncs.com";
	wrapper.html(`<div class="alert ${production ? "alert-info" : "alert-warning"}">
		<strong>${escape_html(title)}</strong><br>${escape_html(detail)}<br>
		<code>${endpoint}</code>
	</div>`);
}

function use_in_settings(frm) {
	frappe.call({
		method: "erpnext_s3_integration.erpnext_s3_integration.doctype.object_storage_profile.object_storage_profile.use_profile_in_settings",
		type: "POST",
		args: { profile_name: frm.doc.name },
		callback: () => {
			frappe.show_alert({ message: __("Profile linked to Object Storage Settings."), indicator: "green" });
			frappe.set_route("Form", "Object Storage Settings");
		},
	});
}

function run_full_test(frm) {
	frappe.call({
		method: "erpnext_s3_integration.object_storage.healthcheck.run_full_test",
		type: "POST",
		args: { profile_name: frm.doc.name },
		freeze: true,
		freeze_message: __("Running credential, upload, download, tag, and delete tests..."),
		callback: ({ message }) => {
			frm.reload_doc();
			const indicator = message.status === "Passed" ? "green" : "red";
			frappe.msgprint({ title: __("Full Read/Write Test"), message: __(message.message), indicator });
		},
	});
}

function show_ram_policy(frm) {
	frappe.call({
		method: "erpnext_s3_integration.object_storage.ram_policy.generate_ram_policy",
		type: "GET",
		args: { profile_name: frm.doc.name },
		callback: ({ message }) => {
			const dialog = new frappe.ui.Dialog({
				title: __("Least-privilege RAM Policy"),
				fields: [
					{ fieldname: "policy_name", fieldtype: "Data", label: __("Policy Name"), read_only: 1, default: message.policy_name },
					{ fieldname: "policy", fieldtype: "Code", label: __("Policy JSON"), options: "JSON", read_only: 1, default: message.policy },
				],
				primary_action_label: __("Copy Policy JSON"),
				primary_action() {
					frappe.utils.copy_to_clipboard(message.policy);
					frappe.show_alert({ message: __("Policy JSON copied."), indicator: "green" });
				},
			});
			dialog.show();
		},
	});
}

function escape_html(value) {
	return frappe.utils.escape_html(String(value || ""));
}
