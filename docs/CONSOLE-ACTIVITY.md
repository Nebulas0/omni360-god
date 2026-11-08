# Foundry console activity

Open `/consoles` on the Foundry website to see paired consoles and installation
requests. The page refreshes every five seconds while visible. Filter by console
or page through older requests; expand **Sources & preparation** for exact source
names and server job details.

- **Preparing on server** shows the preparation jobs' measured progress. For a
  bundle, progress is averaged over the selected sources. It is not a download ETA.
- **File N of M requested** shows the latest authenticated file request. For an
  ISO archive, **Archive part N of M** reflects its 32 MiB HTTP range position.
  This is request activity, not proof that those bytes arrived or were installed.
- **Installed on Xbox** appears only after the Xbox reports successful installation.
  The existing XEX reports final installed/failed/cancelled events. It does not
  send continuous Xbox-side conversion percentages to the website.

**Delete download files** removes only that request's prepared console download
directory. Shared converted library outputs, original source archives, source
caches and files installed on the Xbox remain. An unfinished download may stop;
request the installation again in Omni360 to prepare a fresh copy. Active server
preparation cannot be deleted. The final installation report remains in history.
Deleting files is idempotent and releases their prepared-storage quota.

**Revoke access** disables a console's device token. **Remove entry** then hides
the revoked entry and its requests from the default view, retaining the audit
record and any prepared files. **Show removed entries** reveals them again.
Removing an entry does not itself delete files or revoke another console.

## Browser API and storage

These routes use the existing browser authentication and same-origin write checks;
console bearer tokens alone do not authorize browser management:

- `GET /api/consoles`: bounded 50-request pages; `cursor` is an offset, `device`
  optionally filters a device ID, and `include_removed=1` includes hidden entries.
  Responses are private and uncached, and omit device credentials.
- `POST /api/consoles/requests/<request_id>/delete-files`: JSON `{"confirm":true}`.
  Returns deleted bytes; rejects requests still preparing with HTTP 409.
- `POST /api/consoles/<device_id>/remove`: hides only an already revoked device.

Additive SQLite migrations add `console_devices.hidden` and `console_transfers`.
Transfers store only the latest filename/index/count/request timestamp per request.
Range responses retain their existing streaming, ETag and resume behavior; no
response-body wrapper or byte-copying progress callback is added. The console
protocol and existing XEX remain compatible; this page requires no app update.

Regression coverage is in `server/app/tests/test_console_management.py`, including
download ranges, final-event separation, auth/CSRF, pagination, revocation, path
escape rejection and preservation of shared/original/installed content.
