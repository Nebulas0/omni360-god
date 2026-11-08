# Prepare on the website, install later on Xbox

Requires the Foundry fork's alpha.10 or newer XEX and matching server. Existing
pairing and `settings.txt` install paths still apply; no new API key is needed.

1. Open **Prepare for Xbox** from the website catalog or Console activity. Choose
   your paired console. Selecting catalog sources first opens their review flow.
2. Search for a game or add-on and choose its full source/edition name. Matching
   multi-disc editions show a checklist with supported discs selected by default.
   Add the selection, then search/add more games (up to 32 requests).
3. Choose **Convert on server** for checked GoD/package files, or **Convert on
   Xbox** for supported single-disc ISO archives. Disc sets, install/content discs
   and digital packages use server processing. Unsupported custom merges remain
   unsupported. The XBLA/DLC unlock choice is saved with each selection; GoD/TU
   licenses stay unchanged. Exact-match title updates remain an Xbox-side action.
4. Press **Prepare for Xbox**. Preparation runs even while the console is off.
   Console activity shows jobs, progress, expiry, acceptance and final reports.
   Repeated submissions reuse identical pending requests rather than duplicate
   snapshots. Failed/expired/deleted requests offer **Prepare again**.
5. Update Omni360, open the Foundry Store's **Prepared for Xbox** button and choose
   a tile or press **Start: Download all ready**. Covers use the existing artwork
   cache/hints. Preparing and failed/expired items cannot be downloaded as ready.
   The grid refreshes every 15 seconds, or immediately with X. LB/RB opens the
   existing server/pairing menu. Older connection-menu entries remain available.

The Xbox saves its selected list in `game:\FoundryInbox.json`, separately from
the active `FoundryJob.json` and move journal `FoundryTxn.json`. Selected items
install one at a time. The active/interrupted job takes priority, including after
restart; a failure/pause retains its bytes and stops advancement until resumed
or explicitly discarded. Expired/deleted inbox snapshots release their saved
request so a fresh preparation can proceed. **Discard interrupted job** preserves
installed content and existing partial download files. **Clear pending download
list** in the prepared settings keeps the active job and server copies.

Manual download is the default. **Y: Download settings → Automatic downloads**
opts into checking for ready website items once a minute while the app is idle.
It runs only with Omni360 open, using the current install paths. Clearing the
pending list while automatic downloads are enabled allows it to refill. Server
and pairing identities are checked before adopting saved requests.

Activation saves the active job before removing its pending-list entry. Local
completion receipts prevent a lost final report or a repeated click from causing
an automatic reinstall. Missing success reports are retried during idle checks.
Completed receipts are bounded to 256 request IDs; selection lists are bounded
to 32. Existing checksum, range/ETag resume, disk-space and transactional install
checks remain the file-writing path. A new deliberate reinstall starts with a
new website request; installed content is never assumed solely from server-ready.

Prepared copies retain the server's existing quota/retention (defaults: 256 GiB,
14 days; downloads extend retention). No indefinite storage pin is added.
Deleting prepared files on Console activity removes only that snapshot and may
stop an unfinished Xbox download. Shared GoDs, original sources and installed
Xbox games remain. Removing backup folders does not remove the prepared inbox.

## API / maintenance

- Browser: `GET /consoles/prepare`, `GET /api/consoles/catalog`,
  `GET /api/consoles/releases/<release_id>` and
  `POST /api/consoles/<device_id>/prepare` with `{request_key, selection}`.
  Existing browser auth/CSRF applies; revoked/hidden devices reject submission.
- Device: `GET /api/console/v1/inbox` and idempotent
  `POST /api/console/v1/inbox/<request_id>/accept`. Both use the paired device's
  existing `install:create` scope. Another console cannot list/adopt/download the
  request. Capabilities advertise `prepared_inbox` and `max_inbox_items: 32`.
- Additive SQLite table `console_inbox` tracks browser origin and acceptance.
  The common request normalizer, preparation worker, manifest and installer are
  reused. Existing client admission limits remain unchanged; browser scheduling
  allows 32 per console and 64 globally, with the storage quota enforced later.
- Preserve `src/Foundry.cpp`, `FoundryStore.cpp`, `Foundry.h`, the new page/assets,
  server routes/migration and runtime tests when updating the upstream fork.
  Never replace this XEX with an unmodified upstream release.

Validation includes synthetic offline preparation/owner isolation, duplicate
submission, expiry/reprepare, deletion scope, disc sets/modes, auth/CSRF and native
runtime tests exercising the production queue persistence with a host filesystem.
An XDK build is required; physical Xbox installation remains an acceptance test.
