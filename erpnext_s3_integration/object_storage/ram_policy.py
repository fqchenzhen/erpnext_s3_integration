import json

import frappe
from frappe import _


@frappe.whitelist(methods=["GET"])
def generate_ram_policy(profile_name: str) -> dict:
	frappe.only_for("System Manager")
	profile = frappe.get_doc("Object Storage Profile", profile_name)
	if profile.provider != "Alibaba Cloud OSS":
		frappe.throw(_("RAM policy generation is available only for Alibaba Cloud OSS profiles."))
	policy = build_ram_policy(profile, frappe.get_single("Object Storage Settings"))
	return {
		"policy_name": f"ERPNext-{profile.purpose}-{profile.bucket}",
		"policy": json.dumps(policy, ensure_ascii=False, indent=2),
		"notes": [
			_("Create a custom policy in RAM Console > Permissions > Policies."),
			_("Attach it to the ECS instance RAM role, not to a human RAM user."),
			_("Generate it again whenever bucket, prefix, delete, classification, or restore settings change."),
			_("No bucket-management or public-access action is included."),
		],
	}


def build_ram_policy(profile, settings) -> dict:
	prefix = (profile.prefix or "").strip("/")
	resource_prefix = f"{prefix}/" if prefix else ""
	object_resource = f"acs:oss:*:*:{profile.bucket}/{resource_prefix}*"
	actions = ["oss:PutObject", "oss:GetObject", "oss:GetObjectMeta"]

	if profile.purpose == "Attachments":
		if settings.delete_on_last_reference:
			actions.append("oss:DeleteObject")
		if settings.enable_auto_classification:
			actions.extend(["oss:PutObjectTagging", "oss:GetObjectTagging"])
		if settings.enable_archive_restore:
			actions.append("oss:RestoreObject")
	else:
		actions.extend(["oss:PutObjectTagging", "oss:GetObjectTagging"])
		if settings.backup_retention_days:
			actions.append("oss:DeleteObject")
		if settings.lifecycle_reviewed:
			actions.append("oss:RestoreObject")

	statements = [{"Effect": "Allow", "Action": sorted(set(actions)), "Resource": [object_resource]}]
	if profile.purpose == "Backups":
		statements.append(
			{
				"Effect": "Allow",
				"Action": ["oss:ListObjects"],
				"Resource": [f"acs:oss:*:*:{profile.bucket}"],
				"Condition": {"StringLike": {"oss:Prefix": [f"{resource_prefix}*"]}},
			}
		)
	bucket_actions = ["oss:GetBucketInfo"]
	if profile.purpose == "Attachments":
		bucket_actions.append("oss:GetBucketLifecycle")
	statements.append(
		{
			"Effect": "Allow",
			"Action": bucket_actions,
			"Resource": [f"acs:oss:*:*:{profile.bucket}"],
		}
	)
	return {"Version": "1", "Statement": statements}
