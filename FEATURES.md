# Feature Summary

## Single-screen administration

- Fixed four-tab **Object Storage Settings** interface with no Guided/Advanced mode or hidden global view state.
- Common status and recommendations stay visible; technical fields expand only inside their related section.
- System Manager-only Apps screen entry; no separate Workspace is required.
- Chinese and English UI based on the Frappe user language.
- Direct links to OSS Console, RAM Console, and ECS Console.
- Environment-aware Jakarta recommendations: public Endpoint for Development/Staging and internal Endpoint plus ECS RAM Role for Production.
- One-click save, Full Test, profile enablement, Settings linking, and feature enablement.
- Exact blockers for missing fields, Endpoint mismatch, stale tests, wrong Purpose, and provider permissions.
- Production enablement is blocked until bucket safety and the full profile test pass.

## Attachments

- Official Alibaba OSS SDK V2 or boto3 provider adapters.
- Private server-side uploads and Frappe-authorized downloads.
- HTTP single-range support and `206` responses.
- Original Unicode `File.file_name` retained; object key uses only content hash and visibility.
- Shared object uses the hottest effective retention policy.
- Delete only after the last reference and database commit, with background retry.
- Versioned-bucket deletes create normal delete markers; no permanent version purge.

## Classification and lifecycle

- Precedence: manual File override, DocType + field, DocType, MIME/extension, default.
- Policies: `permanent-hot`, `business-online`, `business-archive`, and `unclassified`.
- Tags: retention, category, application, environment, and site.
- No automatic provider lifecycle, ACL, versioning, or bucket-policy mutation.

## Attachment long-term cold storage

- Safe unmatched default is `unclassified`; only deterministic rules or an authorized override assign `business-archive`.
- Read-only Attachment Bucket lifecycle check for the rule filtered by `retention=business-archive`.
- Default 30-day IA and 90-day Archive transitions; Cold Archive and deletion are disabled.
- Attachment and backup lifecycle confirmations are independent and cannot be applied to the wrong bucket by the app.
- Preview, background Recalculate, Unclassified list, and non-destructive Reset to Default Rules actions.

## Backups

- Dedicated backup profile and bucket.
- Database backup is enabled by default; local public/private files-folder archives are separate, disabled-by-default choices.
- Object-backed attachments are not duplicated into local files-folder archives.
- Manual and cron-triggered backup creation/upload.
- Local temporary files are deleted only after successful upload by default.
- Daily 02:00 schedule, date-organized keys, and the latest 30 successful daily restore points by default.
- Cleanup runs only after a complete successful upload and never removes the last successful restore point.
- Size, generation-time, last-success, estimated usage, and local-space health indicators.
- OSS backup lifecycle is an optional folded section and does not block backup enablement.

## Archive restore

- States: Requested, Restoring, Ready, Expired, Failed, and Cancelled.
- One active request per object.
- Original File read permission plus an allowed restore role.
- Provider submission can no longer be cancelled.
- Failed requests can retry up to the configured limit.
- 15-minute active-request polling, system notifications by default, optional email.
