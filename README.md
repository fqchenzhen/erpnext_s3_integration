# ERPNext S3 Integration

ERPNext S3 Integration is a Frappe app by Solufy that stores ERPNext attachments and backups in S3-compatible object storage.

It supports AWS S3, Alibaba Cloud OSS in S3-compatible mode, and providers such as MinIO. It keeps existing S3-backed files accessible inside ERPNext and provides a settings-driven workflow for file migration and backup sync.

## Features

- Store new ERPNext `File` attachments in S3 instead of the local filesystem.
- Open S3-backed files inside ERPNext through a secure application route.
- Support both direct streaming and pre-signed URL redirects for file delivery.
- Migrate existing local attachments to S3 in the background.
- Sync database and file backups to S3.
- Schedule backup sync using a CRON expression from the settings doctype.
- Optionally delete S3 objects when the related ERPNext `File` record is deleted.
- Optionally remove local backup files after successful S3 upload.
- Support custom bucket prefixes for both attachments and backups.
- Support S3-compatible endpoints with path-style addressing for MinIO and similar providers.

For a concise GitHub feature summary, see [FEATURES.md](FEATURES.md).

## Requirements

- Frappe / ERPNext v16 bench
- Python 3.14+
- An S3-compatible bucket
- Valid access credentials for the target bucket

## Installation

Run the following from your bench directory:

```bash
bench get-app https://github.com/solufy/erpnext_s3_integration
bench --site <your-site> install-app erpnext_s3_integration
bench --site <your-site> migrate
bench restart
```

If you are installing from a local app path or a private repository, use your normal `bench get-app` flow and then install the app on the target site.

## Dependencies

This app requires:

- `boto3`
- `croniter`

Bench normally installs these dependencies from `pyproject.toml`. To repair a local editable installation, run:

```bash
bench pip install -e apps/erpnext_s3_integration
bench restart
```

## Setup

1. Open **S3 Integration Settings** in ERPNext.
2. Enter the S3 connection details:
   - `Access Key ID`
   - `Secret Access Key`
   - `Region Name`
   - `Bucket Name`
   - `Endpoint URL` for Alibaba Cloud OSS, MinIO, or another custom endpoint
   - `Addressing Style` for your provider
3. Optionally set `Folder Prefix` to keep all objects under a dedicated root path.
4. Save the document.
5. Click `Test Bucket Access`.

Provider examples:

- AWS S3: leave `Endpoint URL` blank and use `Addressing Style = auto`.
- Alibaba Cloud OSS international site, Indonesia (Jakarta): use Region `ap-southeast-5`, Endpoint URL `https://s3.oss-ap-southeast-5.aliyuncs.com`, and `Addressing Style = virtual`. If ERPNext also runs in Alibaba Cloud Jakarta, use `https://s3.oss-ap-southeast-5-internal.aliyuncs.com`.
- MinIO: set the MinIO endpoint, for example `http://minio:9000`, and use `Addressing Style = path`.

Alibaba OSS uses boto3 S3 V2 signing because Alibaba's current boto3 guidance does not support boto3 SigV4 uploads. S3 V2 compatibility must be enabled for the OSS account. Jakarta is outside the Chinese mainland and is not affected by the Chinese-mainland default public endpoint restriction. Bucket-bound CNAME endpoints are not supported by the current boto3 client mode and require a future native OSS client implementation.

Use a private bucket with public access blocked. Objects are uploaded with a private ACL; ERPNext decides whether a `File` is public or private and serves it through an application stream or a temporary signed URL.

## Attachment Storage Setup

To store new attachments in S3:

1. Enable `Enable S3 for file attachments`.
2. Choose whether files should be streamed through ERPNext or served with pre-signed URLs using `Stream file content from S3 when viewing/downloading`.
3. Optionally enable `Delete from S3 when File is deleted`.
4. Save the settings.

From that point onward, newly uploaded ERPNext attachments are stored in S3.

## Existing File Migration

To migrate existing files already stored on disk:

1. Open **S3 Integration Settings**.
2. Ensure `Enable Attachments S3` is enabled.
3. Enable `Migrate Only Unmigrated` if you want to skip rows already pointing to S3.
4. Click `Migrate Existing Files`.

The migration runs as a background job. It uploads local files to S3 and updates the corresponding `File.file_url` to the `/s3/...` route used by the app.

## Backup Sync Setup

To send ERPNext backups to S3:

1. Enable `Enable Backups S3`.
2. Choose whether to upload database backups, file backups, or both.
3. Set `Backup Folder Prefix` if you want backups stored separately from attachments.
4. Set `Backup CRON Expression`.
5. Choose whether to create a fresh backup before sync.
6. Choose whether to keep local backups after upload.
7. Save the settings.

The scheduler checks the CRON expression and uploads backup files to S3 when the next run time is due.

You can also trigger an immediate manual sync from the settings form with `Take Backup and Sync`.

## Behavior Notes

- Disabling `Enable Attachments S3` stops new uploads from being redirected to S3.
- Existing files that already point to `/s3/...` remain accessible as long as the S3 credentials are still configured.
- External file URLs such as `http://` and `https://` are skipped by the migration tool.
- The app uses a `File` override so ERPNext can read S3-backed files without expecting them on local disk.
- Changing an existing S3-backed File between public and private is blocked; re-upload it with the intended visibility.
- `Test Bucket Access` checks bucket listing only. Verify a sample upload, open, and delete before enabling production traffic.

## Project Structure

- [hooks.py](erpnext_s3_integration/hooks.py) registers file hooks, scheduler hooks, redirects, and the `File` override.
- [file_hooks.py](erpnext_s3_integration/file_hooks.py) handles upload interception, key generation, and delete sync.
- [api.py](erpnext_s3_integration/api.py) serves S3-backed files through ERPNext.
- [migration.py](erpnext_s3_integration/migration.py) migrates existing local files to S3.
- [backup_hooks.py](erpnext_s3_integration/backup_hooks.py) syncs backups and handles retention cleanup.
- [s3_client.py](erpnext_s3_integration/s3_client.py) wraps S3 client setup, upload, delete, and URL generation.

## Verification Checklist

- `Test Bucket Access` succeeds.
- A newly uploaded attachment gets a `/s3/...` URL in the `File` record.
- Opening an existing S3-backed file works from ERPNext.
- Manual backup sync uploads the expected backup artifacts.
- Scheduled backup sync runs with the configured CRON expression.

## Support

Maintained by Solufy.

- Company: Solufy Pvy. Ltd.
- Email: sahil@solufy.in

## License

MIT
