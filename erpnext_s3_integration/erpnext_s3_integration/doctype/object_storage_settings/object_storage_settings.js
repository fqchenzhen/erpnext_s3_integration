frappe.ui.form.on("Object Storage Settings", {
	refresh(frm) {
		frm.set_query("attachment_storage_profile", () => ({ filters: { purpose: "Attachments" } }));
		frm.set_query("backup_storage_profile", () => ({ filters: { purpose: "Backups" } }));
		render_setup_assistant(frm);
		render_attachment_status(frm);
		render_classification(frm);
		render_attachment_lifecycle(frm);
		render_backup_status(frm);
		render_restore_help(frm);
		render_more_menu(frm);
	},
	attachment_storage_profile(frm) {
		render_attachment_status(frm);
		render_attachment_lifecycle(frm);
	},
	backup_storage_profile(frm) {
		render_backup_status(frm);
	},
	enable_attachment_storage(frm) {
		render_attachment_status(frm);
		render_backup_status(frm);
	},
	enable_backup_storage(frm) {
		render_backup_status(frm);
	},
	attachment_lifecycle_ia_days: render_attachment_lifecycle,
	attachment_lifecycle_archive_days: render_attachment_lifecycle,
	attachment_lifecycle_cold_archive_days: render_attachment_lifecycle,
	attachment_lifecycle_delete_days: render_attachment_lifecycle,
});

function render_more_menu(frm) {
	frm.clear_custom_buttons();
	frm.add_custom_button(__("Open Profiles"), () => frappe.set_route("List", "Object Storage Profile"), __("More"));
	frm.add_custom_button(__("Apply Recommended Storage Plan"), () => apply_recommended_plan(frm), __("More"));
	frm.add_custom_button(__("Open OSS Console"), () => open_url("https://oss.console.aliyun.com/"), __("More"));
	frm.add_custom_button(__("Open RAM Console"), () => open_url("https://ram.console.aliyun.com/"), __("More"));
}

async function render_setup_assistant(frm) {
	const { message } = await frappe.call({
		method: settings_method("get_setup_assistant"),
		type: "GET",
	});
	if (!message || !frm.get_field("setup_progress_html")) return;
	frm.__object_storage_assistant = message;
	render_attachment_lifecycle(frm);
	const attention = message.features.filter((feature) => feature.status === "Needs Attention" || feature.status === "Check Failed" || feature.status === "Not Configured").length;
	const rows = message.features.map((feature, index) => assistant_row(feature, index)).join("");
	const next_action = message.next_action;
	const primary = next_action
		? `<button class="btn btn-primary btn-sm" data-assistant-primary>${escape_html(primary_action_label(message))}</button>`
		: `<button class="btn btn-primary btn-sm" data-health-check>${escape_html(__("Run Health Check"))}</button>`;
	frm.get_field("setup_progress_html").$wrapper.html(`
		<div class="object-storage-assistant">
			<div class="object-storage-summary">
				<div><div class="object-storage-title">${escape_html(__("Object Storage"))}</div>
				<div class="object-storage-heading">${escape_html(__('{0} items available, {1} item(s) need attention', [message.completed, attention]))}</div>
				<div class="text-muted">${escape_html(assistant_summary(message))}</div></div>
				${primary}
			</div>
			<div class="object-storage-list">${rows}</div>
		</div>`);
	bind_assistant_actions(frm);
}

function assistant_summary(message) {
	const archive = message.features.find((feature) => feature.key === "attachment_archive");
	if (archive?.status === "Needs Attention" && message.completed >= 2) {
		return __("Attachment upload, download, and database backup can continue. Archive rules still need attention.");
	}
	if (message.status === "Available") return __("Object storage is configured and healthy.");
	return __("Follow the next highlighted action. The assistant handles save, verification, and enablement in order.");
}

function primary_action_label(message) {
	if (message.features.some((feature) => feature.status === "Check Failed")) return __("Fix Problem");
	if (message.completed === 0) return __("Start Setup");
	return __("Continue Setup");
}

function assistant_row(feature, index) {
	const presentation = {
		Available: ["green", "✓", __("Available")],
		Enabled: ["green", "✓", __("Enabled")],
		Optional: ["gray", "○", __("Not Drilled")],
		"Needs Attention": ["orange", "!", __("Needs Attention")],
		"Check Failed": ["red", "!", __("Check Failed")],
		"Not Configured": ["gray", "○", __("Not Configured")],
	};
	const [indicator, icon, label] = presentation[feature.status] || ["blue", "•", __(feature.status)];
	const action = feature.action
		? `<button class="btn btn-link btn-sm" data-feature-index="${index}">${escape_html(feature.action.label)}</button>`
		: "";
	return `<div class="object-storage-row">
		<div class="object-storage-row-icon text-${indicator}">${icon}</div>
		<div class="object-storage-row-content"><div><strong>${escape_html(feature.title)}</strong> <span class="indicator-pill ${indicator}">${escape_html(label)}</span></div>
			<div class="text-muted">${escape_html(feature.summary)}</div></div>
		<div class="object-storage-row-action">${action}</div>
	</div>`;
}

function bind_assistant_actions(frm) {
	const wrapper = frm.get_field("setup_progress_html").$wrapper;
	wrapper.find("[data-feature-index]").on("click", function () {
		const feature = frm.__object_storage_assistant.features[Number($(this).attr("data-feature-index"))];
		run_assistant_action(frm, feature.action);
	});
	wrapper.find("[data-assistant-primary]").on("click", () => run_assistant_action(frm, frm.__object_storage_assistant.next_action));
	wrapper.find("[data-health-check]").on("click", () => run_health_check(frm));
}

function run_assistant_action(frm, action) {
	if (!action) return;
	if (action.type === "new_profile") show_profile_setup_dialog(frm, action.purpose);
	if (action.type === "open_profile") frappe.set_route("Form", "Object Storage Profile", action.profile_name);
	if (action.type === "verify_enable") verify_and_enable(frm, action.purpose, action.profile_name);
	if (action.type === "verify_bucket") verify_bucket(frm, action.profile_name);
	if (action.type === "bucket_setup") show_bucket_setup_dialog(frm, action.profile_name);
	if (action.type === "run_backup") run_backup_now(frm);
	if (action.type === "open_tab") open_form_tab(frm, action.fieldname);
}

function show_profile_setup_dialog(frm, purpose) {
	const dialog = new frappe.ui.Dialog({
		title: purpose === "Attachments" ? __("Set Up Attachment Storage") : __("Set Up Database Backups"),
		fields: [
			{ fieldname: "environment", fieldtype: "Select", label: __("Environment"), options: "Development\nStaging\nProduction", default: "Development", reqd: 1 },
			{ fieldname: "bucket", fieldtype: "Data", label: __("Bucket Name"), reqd: 1 },
			{ fieldname: "prefix", fieldtype: "Data", label: __("Object Prefix (Optional)"), description: __("Leave blank for a dedicated bucket. Use it only to isolate a site or environment inside a shared bucket.") },
			{ fieldname: "access_key_id", fieldtype: "Password", label: __("Access Key ID"), depends_on: "eval:doc.environment!='Production'", mandatory_depends_on: "eval:doc.environment!='Production'" },
			{ fieldname: "access_key_secret", fieldtype: "Password", label: __("Access Key Secret"), depends_on: "eval:doc.environment!='Production'", mandatory_depends_on: "eval:doc.environment!='Production'" },
			{ fieldname: "ram_role_name", fieldtype: "Data", label: __("RAM Role Name"), depends_on: "eval:doc.environment=='Production'", description: __("The role must be attached to the Jakarta ECS instance. Docker obtains temporary credentials from ECS metadata.") },
		],
		primary_action_label: __("Save and Check"),
		primary_action: async (values) => {
			dialog.disable_primary_action();
			try {
				const production = values.environment === "Production";
				const profile_name = `ERPNext-${purpose}`;
				const doc = {
					doctype: "Object Storage Profile",
					profile_name,
					provider: "Alibaba Cloud OSS",
					purpose,
					environment: values.environment,
					credential_mode: production ? "ECS Instance RAM Role" : "AccessKey",
					access_key_id: values.access_key_id,
					access_key_secret: values.access_key_secret,
					ram_role_name: values.ram_role_name,
					region: "ap-southeast-5",
					use_internal_endpoint: production ? 1 : 0,
					endpoint_url: production ? "https://oss-ap-southeast-5-internal.aliyuncs.com" : "https://oss-ap-southeast-5.aliyuncs.com",
					bucket: values.bucket,
					prefix: values.prefix || "",
				};
				const { message } = await frappe.call({ method: "frappe.client.insert", type: "POST", args: { doc } });
				const fieldname = purpose === "Attachments" ? "attachment_storage_profile" : "backup_storage_profile";
				await frm.set_value(fieldname, message.name);
				await frm.save();
				dialog.hide();
				await verify_and_enable(frm, purpose, message.name);
			} finally {
				dialog.enable_primary_action();
			}
		},
	});
	dialog.show();
}

async function render_attachment_status(frm) {
	const wrapper = frm.get_field("attachment_setup_help").$wrapper;
	const profile = await profile_values(frm.doc.attachment_storage_profile);
	if (!profile) {
		wrapper.html(status_surface(__("Attachment Storage"), __("Not configured"), __("Create a dedicated private Attachment Bucket in Jakarta OSS."), "gray"));
	} else {
		const endpoint = profile.use_internal_endpoint ? __("Internal Endpoint") : __("Public Endpoint");
		const checked = profile.last_bucket_verified_at ? frappe.datetime.prettyDate(profile.last_bucket_verified_at) : __("Not checked");
		wrapper.html(status_surface(__("Attachment Storage"), frm.doc.enable_attachment_storage ? __("Available") : __("Needs check"),
			`${profile.bucket} · ${profile.environment} · ${endpoint}<br>${escape_html(__("Last checked: {0}", [checked]))}`,
			frm.doc.enable_attachment_storage ? "green" : "orange"));
	}
	render_attachment_action(frm);
}

function render_attachment_action(frm) {
	const wrapper = frm.get_field("attachment_actions_html").$wrapper;
	const profile_name = frm.doc.attachment_storage_profile;
	const label = profile_name ? __("Save and Check") : __("Start Attachment Setup");
	wrapper.html(`<div class="object-storage-primary-action"><div class="text-muted">${escape_html(__("Uploads, downloads, Range, security, and current permissions are checked together."))}</div><button class="btn btn-primary btn-sm" data-attachment-check>${escape_html(label)}</button></div>`);
	wrapper.find("[data-attachment-check]").on("click", () => profile_name ? verify_and_enable(frm, "Attachments", profile_name) : show_profile_setup_dialog(frm, "Attachments"));
}

function render_classification(frm) {
	frm.get_field("classification_help").$wrapper.html(`
		<div class="object-storage-subsection">
			<div class="object-storage-heading">${escape_html(__("Attachment Classification"))}</div>
			<div class="text-muted">${escape_html(__("Classification assigns retention tags. It never moves a file to another bucket."))}</div>
			<table class="table table-bordered table-sm object-storage-policy-table"><tbody>
				<tr><td>${escape_html(__("Item main images, logos, and avatars"))}</td><td>${escape_html(__("Always Standard"))}</td></tr>
				<tr><td>${escape_html(__("Item and BOM technical documents"))}</td><td>${escape_html(__("Keep online"))}</td></tr>
				<tr><td>${escape_html(__("Orders, invoices, receipts, Stock Entries, and quality attachments"))}</td><td>${escape_html(__("IA after 30 days; Archive after 365 days"))}</td></tr>
				<tr><td>${escape_html(__("Unmatched attachments"))}</td><td>${escape_html(__("Keep Standard"))}</td></tr>
			</tbody></table>
		</div>`);
	const actions = frm.get_field("classification_actions_html").$wrapper;
	actions.html(`<div class="object-storage-secondary-actions">
		<button class="btn btn-default btn-sm" data-classification="preview">${escape_html(__("Preview an Attachment"))}</button>
		<button class="btn btn-default btn-sm" data-classification="recalculate">${escape_html(__("Recalculate Existing Attachments"))}</button>
		<button class="btn btn-default btn-sm" data-classification="edit">${escape_html(__("Edit Classification Rules"))}</button>
	</div>`);
	actions.find("[data-classification=preview]").on("click", preview_classification);
	actions.find("[data-classification=recalculate]").on("click", recalculate_classification);
	actions.find("[data-classification=edit]").on("click", () => expand_section(frm, "classification_rules_section"));
}

function render_attachment_lifecycle(frm) {
	const ia = Number(frm.doc.attachment_lifecycle_ia_days || 0);
	const archive = Number(frm.doc.attachment_lifecycle_archive_days || 0);
	const cold = Number(frm.doc.attachment_lifecycle_cold_archive_days || 0);
	const remove = Number(frm.doc.attachment_lifecycle_delete_days || 0);
	frm.get_field("attachment_lifecycle_intro").$wrapper.html(`
		<div class="object-storage-subsection">
			<div class="object-storage-heading">${escape_html(__("Long-term Attachment Storage"))}</div>
			<div class="object-storage-timeline">
				<div><strong>${escape_html(__("Recent"))}</strong><span>${escape_html(__("Standard ZRS"))}</span></div>
				<div><strong>${escape_html(ia ? __("After {0} days", [ia]) : __("IA disabled"))}</strong><span>${escape_html(ia ? __("IA ZRS, immediate download") : __("Remain Standard"))}</span></div>
				<div><strong>${escape_html(archive ? __("After {0} days", [archive]) : __("Archive disabled"))}</strong><span>${escape_html(archive ? __("Archive ZRS, restore before download") : __("Remain online"))}</span></div>
				<div><strong>${escape_html(__("Cold Archive"))}</strong><span>${escape_html(cold ? __("After {0} days", [cold]) : __("Not used"))}</span></div>
				<div><strong>${escape_html(__("Automatic deletion"))}</strong><span>${escape_html(remove ? __("After {0} days", [remove]) : __("Never"))}</span></div>
			</div>
			<div class="text-muted">${escape_html(__("Only objects tagged retention=business-archive use this OSS rule. Missing lifecycle rules do not block normal attachment storage."))}</div>
		</div>`);
	const wrapper = frm.get_field("attachment_lifecycle_actions_html").$wrapper;
	if (!frm.doc.attachment_storage_profile) {
		wrapper.html(`<div class="text-muted">${escape_html(__("Configure attachment storage before checking the OSS lifecycle rule."))}</div>`);
		return;
	}
	const archive_feature = frm.__object_storage_assistant?.features?.find((feature) => feature.key === "attachment_archive");
	const archive_ready = archive_feature?.status === "Available";
	const label = archive_ready ? __("Check Again") : __("Configure OSS Archive Rule");
	wrapper.html(`<div class="object-storage-secondary-actions"><button class="btn btn-default btn-sm" data-lifecycle-action>${escape_html(label)}</button><button class="btn btn-link btn-sm" data-lifecycle-custom>${escape_html(__("Custom Lifecycle"))}</button></div>`);
	wrapper.find("[data-lifecycle-action]").on("click", () => archive_ready ? verify_bucket(frm, frm.doc.attachment_storage_profile) : show_bucket_setup_dialog(frm, frm.doc.attachment_storage_profile));
	wrapper.find("[data-lifecycle-custom]").on("click", () => expand_section(frm, "attachment_lifecycle_settings_section"));
}

async function render_backup_status(frm) {
	const object_backed = Boolean(frm.doc.enable_attachment_storage);
	const folder_help = object_backed
		? __("Local folder backups include only files still stored on this server; they do not duplicate Attachment Bucket objects.")
		: __("Local files folders may contain ordinary attachments and can produce larger archives.");
	frm.get_field("backup_setup_help").$wrapper.html(status_surface(__("Daily Full Database Backup"), frm.doc.enable_backup_storage ? __("Available") : __("Not configured"),
		`${escape_html(__("Schedule: daily at 02:00 · Keep 30 successful daily restore points"))}<br>${escape_html(folder_help)}`,
		frm.doc.enable_backup_storage ? "green" : "gray"));
	const wrapper = frm.get_field("backup_actions_html").$wrapper;
	const label = frm.doc.enable_backup_storage ? __("Back Up Now and Verify") : frm.doc.backup_storage_profile ? __("Save and Check") : __("Start Backup Setup");
	wrapper.html(`<div class="object-storage-primary-action"><div class="text-muted">${escape_html(__("A failed backup never removes an existing successful restore point."))}</div><button class="btn btn-primary btn-sm" data-backup-primary>${escape_html(label)}</button></div>`);
	wrapper.find("[data-backup-primary]").on("click", () => {
		if (frm.doc.enable_backup_storage) return run_backup_now(frm);
		if (frm.doc.backup_storage_profile) return verify_and_enable(frm, "Backups", frm.doc.backup_storage_profile);
		return show_profile_setup_dialog(frm, "Backups");
	});
	render_backup_capacity(frm);
}

async function render_backup_capacity(frm) {
	const wrapper = frm.get_field("backup_capacity_html").$wrapper;
	try {
		const { message } = await frappe.call({ method: "erpnext_s3_integration.backup_hooks.get_backup_capacity_summary", type: "GET" });
		if (!message) return;
		const indicator = message.status === "Critical" ? "red" : message.status === "Warning" ? "orange" : "green";
		wrapper.html(`<div class="object-storage-capacity-grid">
			${metric(__("Latest success"), message.last_success_display || __("No successful backup yet"), indicator)}
			${metric(__("Latest size"), message.latest_size_display || "—")}
			${metric(__("Estimated OSS usage"), message.estimated_usage_display || __("Collecting data"))}
			${metric(__("Recovery point objective"), __("Up to approximately 24 hours"))}
		</div>${message.message ? `<div class="text-${indicator} small">${escape_html(message.message)}</div>` : ""}`);
	} catch (error) {
		wrapper.html(`<div class="text-muted">${escape_html(__("Backup capacity data is not available yet."))}</div>`);
	}
}

function render_restore_help(frm) {
	frm.get_field("restore_help").$wrapper.html(`<div class="object-storage-subsection">
		<div class="object-storage-heading">${escape_html(__("Archive Restore"))}</div>
		<div>${escape_html(__("When a user opens an archived attachment, Frappe shows a Restore button instead of a download error. The request is permission-checked, polled every 15 minutes, and the user is notified when the temporary readable copy is ready."))}</div>
		<div class="text-muted">${escape_html(__("Ordinary Standard and IA attachments download immediately. Backup recovery remains a System Manager runbook."))}</div>
	</div>`);
}

async function verify_and_enable(frm, purpose, profile_name) {
	try {
		if (frm.is_dirty()) await frm.save();
		const { message } = await frappe.call({
			method: settings_method("verify_and_enable_profile"),
			type: "POST",
			args: { purpose, profile_name },
			freeze: true,
			freeze_message: __("Checking connection, bucket security, and object operations..."),
		});
		if (message.status === "Passed") show_enable_success(purpose, message);
		else show_enable_failure(message);
		await frm.reload_doc();
	} catch (error) {
		await frm.reload_doc();
		throw error;
	}
}

function show_enable_success(purpose, result) {
	const checks = result.bucket_verification?.checks || [];
	const lifecycle = checks.find((item) => item.code === "LIFECYCLE");
	const remaining = lifecycle?.status !== "Passed" && purpose === "Attachments"
		? `<div class="alert alert-warning">${escape_html(__("Attachment upload and download are enabled. Next: create the 30-day IA and 365-day Archive lifecycle rule."))}</div>` : "";
	frappe.msgprint({
		title: purpose === "Attachments" ? __("Attachment Storage Is Ready") : __("Database Backup Storage Is Ready"),
		message: `<ul><li>${escape_html(__("Jakarta OSS connection passed"))}</li><li>${escape_html(__("Bucket privacy and public-access protection passed"))}</li><li>${escape_html(__("Enabled object operations passed"))}</li></ul>${remaining}`,
		indicator: "green",
	});
}

function show_enable_failure(result) {
	const blockers = result.blockers || [];
	const failed_steps = (result.steps || []).filter((step) => step.status === "Failed");
	const first = blockers[0]?.message || failed_steps[0]?.detail || result.message || __("The check did not pass.");
	const technical = [...blockers, ...failed_steps].map((item) => `<li>${escape_html(JSON.stringify(item))}</li>`).join("");
	const continuity = result.kept_existing_feature
		? `<div class="alert alert-info">${escape_html(__("The existing storage feature remains enabled. Update the read-only RAM permission, then check again."))}</div>`
		: "";
	frappe.msgprint({
		title: __("One Item Needs Attention"),
		message: `<p>${escape_html(first)}</p>${continuity}${technical ? `<details><summary>${escape_html(__("Technical Details"))}</summary><ul>${technical}</ul></details>` : ""}`,
		indicator: "orange",
	});
}

async function verify_bucket(frm, profile_name) {
	const { message } = await frappe.call({
		method: "erpnext_s3_integration.object_storage.bucket_setup.verify_bucket_configuration",
		type: "POST",
		args: { profile_name },
		freeze: true,
		freeze_message: __("Checking bucket security and archive rules..."),
	});
	show_bucket_result(message, profile_name);
	await frm.reload_doc();
}

function show_bucket_result(result, profile_name) {
	const priority = ["BUCKET_INFO_FAILED", "REGION", "PRIVATE_BUCKET", "BLOCK_PUBLIC_ACCESS", "REDUNDANCY", "LIFECYCLE_READ_FAILED", "LIFECYCLE", "LIFECYCLE_SCOPE"];
	const issue = priority.map((code) => result.checks.find((item) => item.code === code && item.status !== "Passed")).find(Boolean);
	const rows = result.checks.map((item) => `<li>${escape_html(item.title)}: ${escape_html(item.summary)}</li>`).join("");
	const permission_actions = issue?.provider_code === "AccessDenied"
		? `<div class="object-storage-secondary-actions"><button class="btn btn-primary btn-sm" data-copy-ram-policy>${escape_html(__("Copy Updated RAM Policy"))}</button><button class="btn btn-default btn-sm" data-open-ram-console>${escape_html(__("Open RAM Console"))}</button></div>`
		: "";
	const dialog = frappe.msgprint({
		title: issue ? __("Bucket Check Needs Attention") : __("Bucket Configuration Passed"),
		message: `<p>${escape_html(issue?.summary || __("The bucket security and recommended lifecycle rule are ready."))}</p>${permission_actions}<details><summary>${escape_html(__("Technical Details"))}</summary><ul>${rows}</ul></details>`,
		indicator: result.status === "Failed" ? "red" : result.status === "Warning" ? "orange" : "green",
	});
	dialog.$wrapper.find("[data-copy-ram-policy]").on("click", () => copy_ram_policy(profile_name));
	dialog.$wrapper.find("[data-open-ram-console]").on("click", () => open_url("https://ram.console.aliyun.com/"));
}

async function copy_ram_policy(profile_name) {
	const { message } = await frappe.call({
		method: "erpnext_s3_integration.object_storage.ram_policy.generate_ram_policy",
		type: "GET",
		args: { profile_name },
	});
	frappe.utils.copy_to_clipboard(message.policy);
	frappe.show_alert({ message: __("Updated least-privilege RAM Policy copied."), indicator: "green" });
}

async function show_bucket_setup_dialog(frm, profile_name) {
	const { message: plan } = await frappe.call({
		method: "erpnext_s3_integration.object_storage.bucket_setup.get_bucket_setup_plan",
		type: "GET",
		args: { profile_name },
	});
	const transitions = plan.transitions.map((item) => `<li>${escape_html(__("After {0} days: {1}", [item.days, item.storage_class]))}</li>`).join("");
	const dialog = new frappe.ui.Dialog({
		title: __("Configure OSS Attachment Archive Rule"),
		fields: [{ fieldname: "instructions", fieldtype: "HTML", options: `<ol>
			<li>${escape_html(__("Open the Attachment Bucket in the Alibaba Cloud OSS console."))}</li>
			<li>${escape_html(__("Open Data Management > Lifecycle."))}</li>
			<li>${escape_html(__("Create an enabled lifecycle rule with prefix: {0}", [plan.prefix || __("empty")]))}</li>
			<li>${escape_html(__("Add tag condition: retention = business-archive"))}</li>
			${transitions}<li>${escape_html(__("Do not configure expiration or Cold Archive."))}</li>
		</ol><div class="alert alert-info">${escape_html(__("The app only reads this rule. It never changes OSS lifecycle settings."))}</div>` }],
		primary_action_label: __("Open OSS and Configure"),
		primary_action: () => open_url(plan.console_url),
		secondary_action_label: __("I Finished, Check Again"),
		secondary_action: () => { dialog.hide(); verify_bucket(frm, profile_name); },
	});
	dialog.show();
}

async function run_health_check(frm) {
	const features = frm.__object_storage_assistant?.features || [];
	const candidates = features.filter((feature) => feature.profile && ["attachments", "backups"].includes(feature.key));
	for (const feature of candidates) {
		const purpose = feature.key === "attachments" ? "Attachments" : "Backups";
		await verify_and_enable(frm, purpose, feature.profile);
	}
}

async function run_backup_now(frm) {
	const { message } = await frappe.call({
		method: "erpnext_s3_integration.backup_hooks.run_backup_now",
		type: "POST",
		freeze: true,
		freeze_message: __("Creating, uploading, and verifying the database backup..."),
	});
	frappe.msgprint({ title: message ? __("Backup Completed") : __("Backup Needs Attention"), message: message ? __("A new successful restore point was uploaded.") : __("The backup did not complete. Open S3 Sync Log for details."), indicator: message ? "green" : "orange" });
	await frm.reload_doc();
}

async function apply_recommended_plan(frm) {
	await frappe.call({ method: settings_method("apply_recommended_storage_plan"), type: "POST", freeze: true, freeze_message: __("Applying recommended settings...") });
	frappe.show_alert({ message: __("Recommended 30/365 attachment policy and 30 backup restore points were applied."), indicator: "green" });
	await frm.reload_doc();
}

function preview_classification() {
	frappe.prompt([{ fieldname: "file_name", fieldtype: "Link", options: "File", label: __("File"), reqd: 1 }], (values) => {
		frappe.call({
			method: "erpnext_s3_integration.object_storage.classification.preview_classification",
			type: "GET",
			args: values,
			callback: ({ message }) => frappe.msgprint({
				title: __("Classification Preview"),
				message: `<p><strong>${escape_html(__("File Policy"))}</strong>: ${escape_html(message.file.policy)}</p><p><strong>${escape_html(__("Matched Source"))}</strong>: ${escape_html(message.file.source)}</p><p><strong>${escape_html(__("Shared Object Policy"))}</strong>: ${escape_html(message.object.policy)}</p>`,
				indicator: message.object.policy === "unclassified" ? "orange" : "blue",
			}),
		});
	}, __("Preview Classification"), __("Preview"));
}

function recalculate_classification() {
	frappe.call({
		method: "erpnext_s3_integration.object_storage.classification.enqueue_recalculate_classification",
		type: "POST",
		callback: ({ message }) => frappe.msgprint(__("Queued classification for {0} object-backed Files.", [message.queued])),
	});
}

function status_surface(title, status, detail, indicator) {
	return `<div class="object-storage-subsection"><div class="object-storage-heading">${escape_html(title)} <span class="indicator-pill ${indicator}">${escape_html(status)}</span></div><div class="text-muted">${detail}</div></div>`;
}

function metric(label, value, indicator = "blue") {
	return `<div class="object-storage-metric"><div class="text-muted">${escape_html(label)}</div><div class="text-${indicator}">${escape_html(value)}</div></div>`;
}

async function profile_values(profile_name) {
	if (!profile_name) return null;
	const { message } = await frappe.db.get_value("Object Storage Profile", profile_name, ["bucket", "environment", "use_internal_endpoint", "last_bucket_verified_at"]);
	return message;
}

function expand_section(frm, fieldname) {
	const field = frm.get_field(fieldname);
	if (field?.collapse) field.collapse(false);
	field?.$wrapper?.get(0)?.scrollIntoView({ behavior: "smooth", block: "start" });
}

function open_form_tab(frm, fieldname) {
	const tab = frm.layout.tabs.find((item) => item.df.fieldname === fieldname);
	if (tab) tab.set_active();
}

function settings_method(method) {
	return `erpnext_s3_integration.erpnext_s3_integration.doctype.object_storage_settings.object_storage_settings.${method}`;
}

function escape_html(value) {
	return frappe.utils.escape_html(String(value || ""));
}

function open_url(url) {
	window.open(url, "_blank", "noopener,noreferrer");
}
