app_name = "erpnext_s3_integration"
app_title = "ERPNext S3 Integration"
app_publisher = "Solufy"
app_description = "Private multi-provider object storage for ERPNext attachments and cold backups"
app_email = "sahil@solufy.in"
app_license = "mit"

add_to_apps_screen = [
	{
		"name": app_name,
		"logo": "/assets/erpnext_s3_integration/images/object-storage.svg",
		"title": app_title,
		"route": "/app/object-storage-settings",
		"has_permission": "erpnext_s3_integration.permissions.can_open_object_storage_app",
	}
]

scheduler_events = {
	"cron": {
		"*/15 * * * *": ["erpnext_s3_integration.object_storage.restore.process_restore_requests"],
	},
	"all": ["erpnext_s3_integration.backup_hooks.scheduled_backup_and_sync"],
}

extend_doctype_class = {"File": ["erpnext_s3_integration.overrides.s3_file.S3FileMixin"]}

doctype_js = {"File": "public/js/file.js"}

app_include_js = ["/assets/erpnext_s3_integration/js/object_storage.js"]
app_include_css = ["/assets/erpnext_s3_integration/css/object_storage_settings.css"]

write_file = "erpnext_s3_integration.file_hooks.write_file_to_object_storage"
write_file_keys = ["object_storage_profile", "object_storage_key"]
delete_file_data_content = "erpnext_s3_integration.file_hooks.delete_file_data_content"

doc_events = {
	"File": {
		"before_insert": "erpnext_s3_integration.file_hooks.copy_object_reference",
		"validate": "erpnext_s3_integration.object_storage.classification.set_retention_override_audit",
		"after_insert": "erpnext_s3_integration.object_storage.classification.enqueue_file_classification",
		"on_update": "erpnext_s3_integration.object_storage.classification.enqueue_file_classification",
		"after_delete": "erpnext_s3_integration.object_storage.classification.enqueue_shared_object_reclassification",
	},
}

after_install = "erpnext_s3_integration.setup.after_migrate"
after_migrate = "erpnext_s3_integration.setup.after_migrate"

permission_query_conditions = {
	"Object Restore Request": "erpnext_s3_integration.erpnext_s3_integration.doctype.object_restore_request.object_restore_request.get_permission_query_conditions"
}

has_permission = {
	"Object Restore Request": "erpnext_s3_integration.erpnext_s3_integration.doctype.object_restore_request.object_restore_request.has_permission"
}

website_redirects = [
	{
		"source": r"/s3/(.*)",
		"target": r"/api/method/erpnext_s3_integration.api.get_file?key=\1",
	}
]
