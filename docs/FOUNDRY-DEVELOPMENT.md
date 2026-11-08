# Omni360 GoD / Foundry integration

This alpha integration is based on upstream **0.13.4-beta**, commit
`6d9c323`, with the fork's service, updater and signing key preserved. The full
Xbox XDK Release build passes under Docker/Wine. Physical console acceptance
is still required before calling a release stable. GitHub checks the source,
then independently verifies the signed XEX published by ZenServer.

## Source layout and behavior

The Xbox application remains at the repository root. `server/app` is the
matching Flask application. Private configuration, source archives, databases,
game files and signing keys do not belong in this repository.

The main **Store** defaults to Foundry cover browsing. Select Foundry or Archive.org
in **Settings > General > Store source**. Pair through the website's `/consoles`
page, browse/search your catalog, and choose server or
Xbox conversion for a disc game. Digital sources use server processing.
Prepared releases offer explicit component selection. Title updates must match
an actual staged/installed game's Title ID, Media ID and XEX base version;
enable an installed update in Aurora afterward.

The original Archive.org provider keeps its own credentials and catalog. No
Archive.org keys are needed for Foundry. The paired token lives in
`game:\FoundryDevice.json`; it is scoped and revocable, not a server admin key.
`FoundryJob.json` retains an interrupted request and `FoundryTxn.json` records
the file moves needed to restore a prior installation. Do not delete a recovery
journal while a transaction needs its original storage attached.

In 1.0.0-alpha.3 the complete install selection, idempotency key and server/device
identity are saved before the first POST. A lost response or restart replays
that same request. Transient failures retry with backoff while retaining received
bytes, including inside a 32 MB ISO piece. Complete files are SHA-256 checked;
a damaged saved prefix gets one clean retry. Resume progress includes the saved
prefix. An expired artifact can be prepared again from the saved selection.
Changing the server/pairing is blocked during active installs; a resumed job is
bound to its original server. Pairing waits for approval without discarding the
code if browser approval and the console check cross in flight.

Mode A uses an immutable manifest and individual package files. Mode B reads a
ZIP64 stored/deflate ISO from verified 32 MB pieces using the existing
InflateSource/GodConvert engine. RAR, original Xbox games and ambiguous archives
are not advertised by the Foundry source API in this first implementation.
The original provider's supported formats remain separate.

The UI installs mode-B base games separately from add-ons. Use server
mode/component selection for prepared add-ons. **Title updates for installed
games** reads each local disc's execution ID and asks Foundry to fetch the
latest exact XboxUnity match, including when the server never converted that
game. The console independently checks the local game's identity before
committing the downloaded update. Physical-console verification remains required.

## Server protocol

Base path: `/api/console/v1`. All requests use verified HTTPS. Machine responses
are JSON; successful creation/poll responses use HTTP 200 for the native
client. File responses use HTTP 200/206, strong SHA-256 ETags, HEAD, Range and
If-Range. Byte counts and offsets are decimal strings.

| Route | Purpose |
| --- | --- |
| GET capabilities | Protocol version, supported modes/formats and limits |
| POST pairings | Start device pairing; `{ "name": "Xbox 360" }` |
| POST pairings/token | Poll with `device_code`; pending or one-time token |
| GET catalog | `kind=game\|digital`, `q`, `cursor`, `limit` (maximum 50) |
| GET releases/{release_id} | Release metadata and known component IDs |
| POST compatibility/resolve | Actual `title_id`, `media_id`, `base_version` |
| POST install-requests | Prepare a release/component selection |
| GET install-requests/{id} | Server preparation status and artifact ID |
| GET artifacts/{id}/manifest | Immutable prepared/source manifest |
| GET/HEAD artifacts/{id}/files/{file_id} | Authorized immutable file |
| GET/HEAD source-artifacts/{id}/file | Authorized original ZIP archive |
| POST install-requests/{id}/events | Idempotent local install outcome |

Only capabilities and pairing start/poll are exempt from bearer authentication.
Browser administration remains behind the existing SSO/Basic auth and CSRF
header. Browser pairing approval is single-use, expires in ten minutes and is rate-limited.
Token polling can safely retry the same approval until its original expiry;
it never reactivates a revoked device.
Only token hashes are stored server-side. Revoke access at `/consoles`.

Install request example (also set `Idempotency-Key: <unique 16-96 characters>`):

```json
{"release_id":"catalog-123","conversion":"server","component_ids":[]}
```

An empty selection installs newly produced base content only, without silently
adding a TU. Explicit component IDs select prepared components, including DLC
or a verified update. `unlock_packages` is an optional boolean captured for
processing. GoD licenses are always preserved. Changing an already-unlocked
digital package back requires source reprocessing, not a synthetic relock.

To fetch a TU for a locally converted/installed game, submit an install request
with `kind: "title_update"` and `game_identity` containing eight-digit hex
`title_id`, `media_id`, and actual XEX `base_version`. No catalog release or ISO
conversion is required. The endpoint fetches only from XboxUnity, validates the
download against its content ID/size and the exact execution identity, and
returns an ordinary immutable prepared manifest. Missing matches fail clearly.

Artifact storage is separate from mutable combined ZIPs under
`WORK_DIR/console`; snapshots are copied, SHA-256 hashed, validated and published
atomically. Default artifact quota is 256 GiB; retention is 14 days, extended
on access. The service uses one publication worker. Keep one Gunicorn worker;
multiple HTTP threads are supported. Existing browser cleanup does not remove
console snapshots. Expired artifacts require a new request/idempotency key.

## Tests

```sh
python -m pip install -r server/app/requirements.txt
PYTHONPATH=server/app START_BACKGROUND=false WORK_DIR=/tmp/foundry-work \
OUTPUT_DIR=/tmp/foundry-output CONSOLE_CONTRACT_FIXTURES=tools/test_foundry/fixtures \
python -m pytest -q server/app/tests
python tools/test_foundry/run.py
```

The tests require clang/clang++ as well as a C/C++ compiler. The parser test
compiles actual client protocol code as C++03 and consumes
synthetic manifests exported from real Flask test responses. It checks path
traversal, collisions, provider/version/origin validation, and 64-bit sizes.
The runtime test includes the actual Foundry.cpp with a host filesystem adapter
and fault-injected transport. It exercises mid-file/in-piece resume, cancelled
transfers, corrupted prefixes, lost POST responses across restart, server
binding, and power loss after each package move. Its Win32 adapter is only for
testing; it is not an SDK replacement or proof of hardware compatibility.
Cache title-update replacement and first-install power cuts, and shared-drive
space budgeting, are also exercised. This does not replace physical-console
install/power-loss tests.

## Producing an XEX

The persistent Linux Docker/Wine builder on ZenServer now compiles the actual
Xbox Release projects with the full XDK, bundles fonts and packages the XEX.
See [builder instructions](../builder/README.md) for the installed layout,
private prerequisites and automatic signed GitHub releases. No Windows VM is
used. The SDK/key stay off GitHub; its workflow independently verifies the
published signed pair. The first full native build passed on 2026-11-07.

The original Windows VS2010 route remains available through
`tools/build_foundry.ps1` with `XEDK` and `OMNI360_SIGNING_KEY_FILE` set. It is
an alternative, not a requirement for the Wine build. Both paths retain the
fork's updater repository and public key. Never substitute an upstream binary
for the modified Foundry application.

## Release acceptance still required

- Full Release XDK compilation now passes; physical launch and signed self-update still need console testing.
- Physical console pairing/TLS, both conversion modes, launch after reboot.
- DLC visible in-game and matched TU launch/activation through Aurora.
- Disk-full, cancellation, disconnected storage and power loss through commit.
- Multi-disc and alternate-basename replacements preserve other content.
- Online TU lookup for a fresh console-mode game, followed by an in-game launch.
- Check the longer `FOUNDRY-INTEGRATION-PLAN.txt` against the delivered behavior;
  no untested or incomplete feature is a stable-release promise.


## Foundry browsing API (alpha.6)

Device-authenticated `GET /api/console/v1/store/home` returns featured tiles and
bounded category rows; `/store/titles` accepts `kind=game|digital|dlc`, `letter`,
`q` and offset `cursor` with 48 items/page. `/store/titles/<group_id>` pages the
original source editions/discs (50/page), preserving their existing `catalog-N`
release IDs. DLC browsing optionally takes `title_id` and `parent` (base-game
name). Archives tagged `(Addon)` / `(DLC)` are digital DLC in both browsing
and the older search API, regardless of their physical source catalog.

`for_title_id` in an install request limits it to actual DLC packages with that
Title ID, checked after preparation and before publication. A name/artwork match
never overrides STFS identity. `unlock_packages` is captured with each request;
false copies existing licenses unchanged, including already unlocked sources.
GoD/TU licenses are unaffected. DLC sources offer only their own produced files.

`server/app/store_metadata.json` contains 2,249 artwork/ratings hints from the
existing compiled upstream tables. Regenerate with:

```sh
c++ -std=c++03 tools/export_foundry_metadata.cpp -o /tmp/export_foundry_metadata
/tmp/export_foundry_metadata > server/app/store_metadata.json
```

Unknown or ambiguous artwork IDs get plain tiles. Prepared package identities
remain authoritative. Source groups do not add IDs to the production catalog.
Read-only API initialization never recovers another worker's preparing requests;
crash recovery belongs to the preparation worker's startup.


## DLC multi-select (alpha.7)

The native DLC action is a paged checkbox list. Up to 32 catalog source releases
are submitted in one durable `kind: dlc_bundle` request with `release_ids`,
`for_title_id`, `conversion: server` and one `unlock_packages` choice. Each
source's preparation job is retained in the additive `console_request_sources`
table, so restart/retry does not create duplicate jobs. All selected sources
must yield matching DLCs before an immutable combined manifest is published.
Identical destination packages are deduplicated; differing copies conflict.
The existing manifest/parser and client transaction/resume path install the
combined files. Package/component limit remains 100. The capability response
advertises `dlc_multi_select` and `max_dlc_sources: 32`; the client checks it before
queuing a batch. Single-release and older clients remain compatible.

For preserving these changes across official updates, use the
[maintenance README](MAINTAINING-THE-FOUNDRY-FORK.md).


Catalog GET caching in `Foundry.cpp` is restricted to `/store/` responses: 16
entries, 2 MiB total, 512 KiB per entry, 60-second TTL and keys scoped to the
server/device/token. One bounded background request can preload the next page or
focused title. A foreground request reuses an in-flight preload of that same
page; refresh increments a generation so stale preloads cannot repopulate the
cache. Shutdown cancels/joins the prefetch worker. New preloads are suppressed
during active installs; install requests, manifests and bytes bypass the cache.
