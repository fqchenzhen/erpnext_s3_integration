# ERPNext Object Storage Integration

Object storage for Frappe / ERPNext v16 attachments, attachment archives, and backup artifacts, with a single-screen International Alibaba Cloud setup for ECS and OSS in Indonesia (Jakarta).

## What is implemented

- Native Alibaba Cloud OSS integration through the official OSS Python SDK V2.
- ECS Instance RAM Role credentials through the official Alibaba Cloud Credentials SDK.
- AWS S3, MinIO, and custom S3-compatible storage through boto3.
- Separate attachment and backup profiles and buckets.
- Private objects with all downloads proxied through Frappe; no public provider URL or pre-signed redirect.
- Authorized HTTP Range downloads with `206`, `Content-Range`, original Unicode filenames, and unsafe-extension download protection.
- Content-hash-only attachment keys, shared-object reference checks, transaction-safe deletion, and asynchronous retries.
- Automatic retention classification and OSS/S3 object tags.
- Archive restore requests with permissions, polling, retry, expiry, and notifications.
- Daily database backups with 30 successful daily restore points by default, plus separately selected local public/private files-folder archives.
- English source UI and Simplified Chinese translation that follows the Frappe user language.
- A capability-aware provider test covering Put, Head, Get, Range, tags, List, Delete, and absence verification where enabled.
- A System Manager-only Apps screen entry that opens Object Storage Settings directly.

## Supported providers

| Provider | SDK | Recommended production credential |
|---|---|---|
| Alibaba Cloud OSS | `alibabacloud-oss-v2` | ECS Instance RAM Role |
| AWS S3 | `boto3` | Default credential chain / instance role |
| MinIO | `boto3` | AccessKey |
| Custom S3 | `boto3` | Provider-appropriate credential chain |

Alibaba Cloud OSS does not use boto3 compatibility mode.

## Installation

From the bench directory:

```bash
bench pip install -e apps/erpnext_s3_integration
bench --site <site> install-app erpnext_s3_integration
bench --site <site> migrate
bench restart
```

The app requires Frappe / ERPNext v16 and Python 3.14+.

## Start configuration

1. Sign in to the International Alibaba Cloud console.
2. From the ERPNext Apps screen open **ERPNext S3 Integration**. It routes directly to **Object Storage Settings**; Awesome Bar and `/app/object-storage-settings` remain valid alternatives.
3. Open **Setup Assistant**. It shows four concise areas: Attachment Storage, Database Backups, optional Attachment Cold Storage, and optional Archive Restore.
4. Create separate **Attachments** and **Backups** profiles. Bucket names are entered only on those profiles.
5. Click **Apply Recommended Jakarta Settings** on each profile. Production selects the Jakarta internal endpoint and ECS RAM Role; Development/Staging selects the public endpoint for isolated testing.
6. Generate the two least-privilege RAM policies, create an ECS RAM Role, attach both policies, and bind the role to the ERPNext ECS instance.
7. In Settings click **Save, Test and Enable**. The assistant saves safely, runs the current Full Test, enables the profile, links it, and enables the feature in the correct order.
8. Unknown attachments remain `unclassified` and Standard. Optionally review the detailed rules and configure the Attachment Bucket lifecycle rule for `retention=business-archive`.
9. Database backup is selected by default. Local public/private files-folder archives are separate options and exclude attachments already stored in the Attachment Bucket.
10. Complete an attachment restore drill only if Archive Restore is enabled.

The detailed Chinese runbook is in [USER_GUIDE.md](USER_GUIDE.md).

## Security invariants

- Buckets must remain Private with Block Public Access enabled.
- The application never modifies bucket ACL, Block Public Access, versioning, lifecycle, or bucket policy.
- Production OSS access stays on `https://oss-ap-southeast-5-internal.aliyuncs.com` from Jakarta ECS.
- AccessKey is intended for isolated development profiles; production uses ECS RAM Role or an approved credential chain.
- No browser direct upload, public OSS endpoint, CDN origin, anonymous policy, or pre-signed download is used.
- Deletes do not specify a version ID, so versioned buckets receive a delete marker instead of permanent version deletion.
- Generated RAM policies contain object operations only and never contain `oss:*`.

## Attachment long-term cold storage

The safe default for unmatched attachments is `unclassified`. Built-in or administrator rules deterministically mark eligible transactional attachments as `business-archive`. Create this rule manually in the Attachment Bucket, filtered by the exact object tag `retention=business-archive`:

- 30 days: IA
- 365 days: Archive
- Cold Archive: disabled
- Deletion: disabled by default (`0`)

Files tagged `permanent-hot`, `business-online`, or `unclassified` are not moved by this rule. Shared content uses the hottest policy across its File references. The app writes the tags and never creates or changes provider lifecycle rules.

Alibaba Cloud OSS evaluates lifecycle age from the object's last modified time, so the configured day counts are not based on the ERPNext File creation timestamp.

## Default backup lifecycle

The app uploads a database backup daily at 02:00 and, only after a completely successful upload, retains the latest 30 successful daily restore points. Successful uploads also remove their local temporary files by default. Failed runs never trigger cleanup.

The default 30-restore-point policy stays in Standard storage. An OSS lifecycle rule is an optional folded setting for longer retention and is never created or modified by the app.

“Attachments” means live Frappe File objects sent directly to the Attachment Bucket. “Local Private Files Folder Backup” means a generated archive of files still present in `sites/<site>/private/files`; it is not a second backup of object-backed attachments.

## Verification

```bash
bench --site <site> migrate
bench --site <site> run-tests --app erpnext_s3_integration
```

The Cypress Desk test is at `erpnext_s3_integration/cypress/integration/object_storage_setup.js` and is run through the bench/Frappe Cypress environment.

## License

MIT
