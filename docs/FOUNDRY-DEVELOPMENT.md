# Omni360 GoD / Foundry integration

This is a source integration under development, based on upstream commit
`d0f3e5e79e19cc54e439de9bde418dbebf2f7964`. It is **not yet verified by an
Xbox XDK build or a physical Xbox test**. Do not label a release stable until
those checks pass. The ordinary Linux CI job does not produce an XEX.

## Source layout and behavior

The Xbox application remains at the repository root. `server/app` is the
matching Flask application. Private configuration, source archives, databases,
game files and signing keys do not belong in this repository.

From the original Store front page, press **X: Foundry**. Pair through the
website's `/consoles` page, search the Foundry catalog, and choose server or
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

Mode A uses an immutable manifest and individual package files. Mode B reads a
ZIP64 stored/deflate ISO from verified 32 MB pieces using the existing
InflateSource/GodConvert engine. RAR, original Xbox games and ambiguous archives
are not advertised by the Foundry source API in this first implementation.
The original provider's supported formats remain separate.

The UI currently installs mode-B base games separately from add-ons. Use
server mode/component selection for prepared add-ons. Online lookup of updates
for a fresh mode-B game without server-side game metadata remains a follow-up
acceptance item; do not claim that workflow is complete.

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
header. Pairing is single-use, expires in ten minutes and is rate-limited.
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

The parser test compiles actual client protocol code as C++03 and consumes
synthetic manifests exported from real Flask test responses. It checks path
traversal, collisions, provider/version/origin validation, and 64-bit sizes.
This does not replace a full XDK compile or on-console install/power-loss tests.

## Producing an XEX

Use the prerequisites in `COMPILING.md`: Windows, VS2010, official Xbox 360
XDK. Set `XEDK` and `OMNI360_SIGNING_KEY_FILE`, then run
`tools/build_foundry.ps1`. The key must match the fork's `src/UpdateKey.h`.
The updater now reads **Nebulas0/omni360-god**, preserves signature verification,
and does not trust the upstream release key.

The optional workflow **Signed Xbox XDK build** runs only when manually started,
on a private Windows runner with labels `self-hosted, windows, xdk`. It never
executes pull requests. A hosted Ubuntu workflow cannot supply the XDK. No
proprietary compiler, SDK library, or private signing key is distributed here.

Saltbox can host a disposable builder without any public website. KVM is
available on the current server, so a Windows VM managed by Docker is an option.
That still requires a Windows/toolchain setup and the Xbox XDK; an ordinary
Linux container does not make these tools available. No native builder was
installed without those prerequisites, and no placeholder/upstream XEX should
be presented as a compiled Foundry binary.

## Release acceptance still required

- Full Release XDK build, actual SDK/API compatibility, and signed self-update.
- Physical console pairing/TLS, both conversion modes, launch after reboot.
- DLC visible in-game and matched TU launch/activation through Aurora.
- Disk-full, cancellation, disconnected storage and power loss through commit.
- Multi-disc and alternate-basename replacements preserve other content.
- Fresh console-mode games can discover/fetch missing online TUs if advertised.
- Check the longer `FOUNDRY-INTEGRATION-PLAN.txt` against the delivered behavior;
  no untested or incomplete feature is a stable-release promise.
