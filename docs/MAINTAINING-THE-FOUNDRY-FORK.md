# Maintaining the Foundry fork of Omni360

This is the maintenance README for **Nebulas0/omni360-god**. It explains what
was changed, what must survive an official Omni360 update, and how to produce
a new signed Xbox build. Use it with the source, not as a replacement for review.

## Baseline and ownership

Alpha.10 adds the device-scoped website preparation inbox, durable sequential
Xbox list and opt-in automatic downloads. Preserve these alongside the original
Foundry integration; see [Prepared for Xbox](PREPARED-FOR-XBOX.md) for API, files,
limits and validation details.

- Official project: <https://github.com/JustGillin/omni360>, default branch `master`.
- Our fork: <https://github.com/Nebulas0/omni360-god>, deployment branch `main`.
- Recorded upstream baseline: **0.13.4-beta**, commit
  `6d9c323cee0ca9da61fdf1d26dc7a9e0e8049d9f`.
- That baseline was merged with real Git ancestry in fork commit
  `7c52b13b4ca996713c7cf57d1abeeb1b9618ceed`. The Foundry Store and progress work
  followed in `18d674b` and `c616548`. This guide accompanies the multi-select
  DLC and readable source-version changes in the alpha.7 source.
- `CURRENT_VERSION` in `src/settings.h` is **our** version; `UPSTREAM_VERSION`
  records the official code baseline. Do not replace our version with theirs.
- Preserve upstream attribution and AGPL-3.0 notices, including when distributing
  modified XEX files. Ship the corresponding fork source.

The recorded baseline is not a claim about the newest upstream release. Check
the official repository and pin the exact commit for each future update.

## What we added

| Area | Main files | Behavior to retain |
| --- | --- | --- |
| Foundry connection, durable worker, downloads and installation | `src/Foundry.cpp`, `src/Foundry.h` | Pairing, scoped device token, immutable downloads, retries, resume, disk checks and recoverable publication |
| Protocol validation | `src/FoundryProtocol.cpp`, `src/FoundryProtocol.h` | Bounds, FATX-safe paths, byte/hash/range/ETag checks and actual package identity validation |
| Native Foundry Store | `src/FoundryStore.cpp` | Existing native featured/grid/game renderers, source editions/discs, A–Z/search and paged DLC multi-select |
| Shared app integration | `src/main.cpp`, `src/GameListUI.cpp`, `src/GameListUI.h` | Provider setting, Settings controls, Library DLC/TU routes, combined queue progress, readable source versions |
| Xbox projects | `XboxTLS2.vcxproj`, `XboxTLS2.vcxproj.filters` | Every required Foundry translation unit and its project settings |
| Web service | `server/app/` | Catalog/conversion UI, console API, GoD validation, package handling, XboxUnity updates and private install snapshots |
| Browse metadata | `server/app/store_browser.py`, `server/app/store_metadata.json`, `tools/export_foundry_metadata.cpp` | Grouped catalog editions and artwork hints; hints never become authoritative package IDs |
| Fork updater | `src/UpdateCheck.cpp`, `src/UpdateCheck.h`, `src/UpdateKey.h` | Our repository URLs, signature verification and existing fork public key |
| Build and release | `builder/`, `.github/workflows/foundry-checks.yml`, `.github/workflows/wine-release.yml` | Native XDK/Wine build, trusted host signing and independent public artifact verification |
| Regression checks | `server/app/tests/`, `tools/test_foundry/`, `builder/test_*.py` | Synthetic server/API tests, actual portable client runtime/recovery checks, builder/release safeguards |

The original Archive.org provider remains available. Foundry is the default Store
source; changing the provider does not change an existing download's saved origin.
Original game launch, Library scanning, homebrew, settings and Archive.org
credentials must continue to work when merging official UI or installer changes.

### Persistent contracts

- `settings.txt` is retained. Preserve `games-path:` (including multiple Library
  roots), **`xbla-path:`** (the existing content-folder key), `store-source:` and
  unrelated settings. The saved provider values are `foundry` and `archive`.
- `game:\FoundryDevice.json`, `FoundryJob.json` and `FoundryTxn.json` contain
  pairing, saved request/selection and transaction recovery state. Read old
  files or implement an explicit, tested migration. Never discard a recovery
  journal just because a new client was installed.
- An install selection and idempotency key are saved **before** the first POST.
  A lost response/reboot replays the same request rather than creating another.
- Prepared GoD uses individual manifest files, including many approximately
  163 MiB `DataNNNN` parts. ISO-archive downloads use resumable 32 MiB pieces.
  Preserve actual range/ETag/hash validation, bounded retries, retained partials
  and checks before committing files. Do not replace these with a single giant
  download that exceeds FATX limits.
- Catalog browsing caches at most 16 responses / 2 MiB for five minutes, bound to
  origin/device/token. Refresh invalidates in-flight prefetch generations. At most
  one background catalog GET runs, without retries or UI calls; active installs
  suppress new preloads. Never cache install POSTs, manifests or download bytes.
  Foreground catalog GETs and the single preload worker each own a separate
  verified keep-alive HTTPS session. Never access one TLS context concurrently
  or hand it to an installer. Device/origin/token changes replace the appropriate
  session. Strict JSON mode rejects redirects and encoded bodies, retains bounded
  error JSON, supports cancellation between reads and reconnects after an idle
  close. The original Archive.org session callers retain their default behavior.
  Catalog reads use a 15-second socket inactivity timeout and at most two outer
  attempts; background preloads have one attempt and a five-second same-page
  backoff. Preserve the existing installer timeouts/retries. A first connection
  still pays DNS/TLS costs; measure real Xbox latency instead of promising a time.
  Clicking an in-flight preload waits at most 150 ms before cancelling that
  speculative request and using the independent foreground session. Keep the
  old worker handle until it exits; never free its context from another thread.
- Queue counters and download ETA cover the whole download, not the current
  data part. ISO checking/indexing and on-Xbox conversion show their own progress
  and measured estimates. Unknown time is shown as estimating.
- DLC-only installation must not include/reinstall the base GoD. `(Addon)` and
  `(DLC)` source tags mean DLC even in the physical disc catalog. Requests for
  a game carry `for_title_id`, checked against the actual STFS package Title ID.
- Multi-select sends one `kind: dlc_bundle` request with up to 32 `release_ids`
  and one unlock choice. Browse rows include a prepared-first default source,
  avoiding a separate source lookup for every selected DLC. `console_request_sources` preserves each preparation
  job across restarts. The server validates all selected DLCs, deduplicates
  identical destination packages and rejects conflicting copies before publishing
  one manifest. Manifests support at most 100 components. Never silently drop
  failed or mismatched entries from a user's selection.
- Foundry disc sets send `kind: disc_bundle` with at most eight sources, selected
  from the same upstream release grouping or conservatively matched filename
  edition. The checklist selects supported matching discs by default and allows
  manual subsets. Keep region/language/revision separation, missing-disc notices,
  actual executable disc-number checks, source job recovery and one manifest.
  Disc sets use server preparation; play/content/mixed roles must stay distinct.
  Preserve `disc_sets.py`, exported `disc_sets` metadata, and the mixed-disc
  pipeline path. See [disc installation and exceptions](MULTI-DISC-INSTALLS.md).
  Optional manifest disc fields remain backward compatible. Do not replace a
  different numbered disc merely because its Title ID / Media ID matches.
- Unlocking applies only to XBLA/DLC on a private installation copy, with a
  backup. Preserve GoD licenses and leave title updates alone. False means
  preserve supplied bytes; it cannot restore licenses erased by an earlier tool.
- TU compatibility uses actual Title ID, Media ID and XEX base version, including
  on-console converted games. Preserve both content-folder and shared Cache TU
  layouts and their transaction-recovery rules.
- Catalog artwork IDs/names are hints. Unknown/ambiguous identities must not
  authorize a DLC or title-update install. Unidentified Store games use the
  installed Library's actual identity for game-specific DLC installation.
- New APIs/schema must remain compatible with installed older clients and their
  existing immutable artifact URLs. Recovery belongs to the preparation worker's
  startup, not to constructing another read-only Flask app.

## Merge an official update

Do this in a development checkout, **not** the server publisher's managed
`/opt/xbox360/xex-builder/source` checkout. First commit/stash unrelated local
work and verify the worktree is clean. Fetches do not authorize running new
upstream scripts or replacing private deployment configuration.

```sh
git remote -v
# Add only if upstream does not already exist:
# git remote add upstream https://github.com/JustGillin/omni360.git
git fetch origin main
git fetch upstream --tags
git switch -c codex/upstream-update-YYYYMMDD origin/main
```

Replace the date and select a real published upstream tag/commit. The following
`vX.Y.Z-beta` is deliberately a placeholder, not a version to execute unchanged:

```sh
OMNI_TARGET_REF=refs/tags/vX.Y.Z-beta
OMNI_TARGET_COMMIT=$(git rev-parse "${OMNI_TARGET_REF}^{commit}")
git log --oneline HEAD.."${OMNI_TARGET_COMMIT}"
git diff --stat "$(git merge-base HEAD "${OMNI_TARGET_COMMIT}")" "${OMNI_TARGET_COMMIT}"
git merge --no-commit --no-ff "${OMNI_TARGET_COMMIT}"
```

Review both upstream changes and the combined result. Resolve shared-file
conflicts by porting both behaviors, especially in `main.cpp`, `GameListUI`,
networking, `GameInstaller`, `GodConvert`, project XML and the updater.
Do not blanket-choose ours/theirs or copy our old `main.cpp` over the new one;
that loses either official improvements or Foundry hooks. Keep our workflows
and signed-release trust boundary when upstream changes its release process.

Update `UPSTREAM_VERSION`, README baseline, and this guide's recorded upstream
commit. For functional changes, increment our `CURRENT_VERSION` to the next
appropriate alpha/beta source version. Keep the `#ifndef CURRENT_VERSION` guard:
the publisher compiles a unique version such as `1.0.0-alpha.7.build.N`, where
`N` is the main commit count. Do not reset/squash main history or reuse release
versions. Documentation-only commits currently also pass through the automatic
main build pipeline; they do not require a source-version bump.

If upstream changes compiled Store/XBLA tables, regenerate artwork hints and
review the generated diff:

```sh
c++ -std=c++03 tools/export_foundry_metadata.cpp -o /tmp/export_foundry_metadata
/tmp/export_foundry_metadata > server/app/store_metadata.json
```

The new upstream table structures may require updating the exporter first.
Do not invent Title IDs for unmatched games. If project files, XDK requirements,
resources or linker settings changed, review `builder/build.py` translation too;
passing host tests is not proof that the Xbox build still works.

## Validate before publishing

Use Python 3.12 and a C/C++ compiler plus `clang`/`clang++`; Python dependencies
are in `server/app/requirements.txt`. Run these in an isolated development
workspace with temporary data and **no production credentials**:

```sh
python3 -m pip install -r server/app/requirements.txt
PYTHONPATH=server/app START_BACKGROUND=false \
  WORK_DIR=/tmp/omni-foundry-check-work OUTPUT_DIR=/tmp/omni-foundry-check-output \
  python3 -m pytest -q server/app/tests
python3 tools/test_foundry/run.py
python3 -m unittest discover -s builder -p 'test_*.py' -v
python3 -c "import xml.etree.ElementTree as E; E.parse('XboxTLS2.vcxproj'); E.parse('XboxTLS2.vcxproj.filters')"
git diff --check
```

The suite's count changes over time; require all relevant tests to pass rather
than hardcoding a count. Contract fixtures can be regenerated by setting
`CONSOLE_CONTRACT_FIXTURES=tools/test_foundry/fixtures` on the server test command;
review changes and run the portable client checks against them.

Compile the **exact candidate** with the real Xbox XDK in an isolated Wine
container before calling it Xbox-buildable. Follow [the builder README](../builder/README.md).
Use separate source/build binds for branch testing; never overwrite the managed
main checkout or share an active Wine prefix. An ad-hoc `docker run` needs
`--init` as in Compose, network disabled, read-only SDK/source, and no secrets,
ports or Docker socket. Keep C++03/XDK limitations. If upstream needs a newer
SDK, resolve that requirement explicitly rather than deleting compiler flags.

Physical Xbox acceptance should cover:

- Existing settings, Library/game launch, Foundry and Archive.org Store sources.
- Server GoD and Xbox ISO conversion, correct install paths and readable source
  versions (focused name scrolls; Foundry **Y** shows the entire source filename).
- Multi-select DLC, selection across pages, one unlock/preserve prompt, correct
  Title ID checks, no base-game redownload, combined queue counters and ETA.
- A matching TU, a rejected wrong game/update, and content/Cache destinations.
- Network interruption/reboot with a partial file, failed/cancelled installs,
  limited free space, transaction recovery and previously saved jobs.
- Signed updates from an older fork build to the new fork build.

Report what was actually tested. A valid signature/XEX loader or a synthetic
interruption test does not establish game-specific runtime compatibility.

## Back up and deploy backend changes before the client

A GitHub push **does not deploy** `server/app` to the website. If the update
changes the backend, take a fresh private backup of `/opt/xbox360/app`, Dockerfile,
Compose/override, private environment configuration, builder configuration,
current image IDs/tags and a consistent SQLite backup. Protect secret-bearing
backups. Keep game outputs, source caches, pairing and console artifacts.

Build/test a candidate image against temporary data. Ensure backward-compatible
endpoints/schema are deployed before publishing a client requiring them. Preserve
the existing Saltbox/Traefik browser and machine-API routing. Wait for active
conversion/preparation to become idle, drain any current Xbox file downloads,
and restart only the relevant service. Verify `/healthz`, public capabilities,
unauthorized access rejection, and authenticated browsing/download behavior.
On failure restore the prior app/image; do not overwrite the database with an
old snapshot and discard new user data without a specific recovery decision.

Historical local deployment scripts contain dated backup paths/image hashes.
Do not rerun them blindly: create a new reviewed rollout with the current source
checkpoint. The live host publisher itself is deployed deliberately and does
not replace itself from every new commit.

## Publish the new fork XEX

Commit the resolved merge and push the review branch. Require **Foundry protocol
and server checks** to pass. Land it on `main` by a normal fast-forward or merge
that preserves the upstream merge ancestry; do not squash/rebase away upstream
history. The automatic publisher only builds passing **main** commits.

ZenServer uses the installed XDK 2.0.21256.17 and Wine 9 in the persistent,
networkless `xbox360-xex-builder` service. No Windows VM/VS IDE is needed.
The trusted host service checks CI, builds, signs and publishes a prerelease;
GitHub then downloads and verifies the public XEX. GitHub-hosted Actions alone
cannot compile this project without the privately supplied XDK toolchain.

```sh
ssh ZenServer_no_passphrase
cd /opt/xbox360
sudo systemctl status omni360-release.timer
cat xex-builder/automation/status.json
tail -60 xex-builder/build/native-build.log
# Trigger an immediate check after main CI passes:
sudo systemctl start --no-block omni360-release.service
```

Keep the existing private SDK, signing key and GitHub credential outside Git,
Docker build contexts, release assets and Actions artifacts. The compiler gets
neither credentials nor signing key. Keep **our** `UpdateKey.h`; replacing it
with upstream's key breaks the installed fork's update trust chain. Reauthenticate
through the private host setup if the token expires; do not paste keys into docs.

Require all four release assets: `Omni360.xex`, `Omni360.xex.sig`, `build.json`,
`SHA256SUMS`. Independently verify the public download with
`builder/wait_for_release.py`/its `verify` function and our public key; match the
version, exact source SHA, checksums, signature and XEX loader. Published assets
are immutable. A failed build produces no new release. Roll back an installed
client deliberately; never replace bytes under an existing published tag.

Existing fork clients update through **Settings > Updates**. Official Omni360
uses another repository/key, so switching from official to our fork requires
copying the fork XEX manually once. Save the final commit, release, test results,
backup/image paths and any remaining console checks for the next maintainer.

## Artwork caveat: Metro is separate

The small embedded GoD PNG icon, Omni360's cached full box art and the official
Metro dashboard's artwork are different things. `server/app/icons.py` embeds
small icons; Omni360's `CoverArt`/`StoreArt` caches do not populate Metro's cache.
Checking that our app shows a cover does not prove Metro will display one.

During the 2026-11-08 review, all five existing server-prepared GoDs had PNG
icons and valid header digests. The last Kinect Sports: Season Two server
snapshot also had both icon fields populated. Missing full Metro covers for
both official Archive.org and Foundry installs therefore need console/dashboard
network/cache diagnosis; embedding an icon is not a guaranteed Metro box-art fix.
Do not change the console's Xbox Live blocking or system caches automatically.

Known client gap at this review: Foundry's Xbox conversion calls `GodConvert`
with `NULL, 0` for the optional icon, while the upstream GameInstaller and
DiscWorker supply an icon when available. A future icon improvement should reuse
a bounded PNG fetch and pass validated bytes before writing the header, preserve
licenses/GoD data, and test offline package icons separately from Metro box art.
Existing installed headers need an explicit backed-up repair; a new XEX alone
does not rewrite them. This documentation does not claim that gap is fixed.

## Prompt for a future implementation agent

> Update Nebulas0/omni360-god to the selected official JustGillin/omni360 release.
> Read this maintenance guide, the builder README and current code first. Confirm
> clean local/remote state, pin and review the upstream commit, and preserve Git
> merge ancestry. Keep all Foundry contracts above and port shared-file changes
> deliberately. Back up/deploy compatible backend changes before releasing a
> client that needs them. Run server, portable runtime, builder and actual XDK
> checks; report remaining physical-console checks honestly. Preserve our updater
> URL/public key, private SDK/signing/credential boundary and existing user data.
> Publish only passing reviewed main source through the established signed
> builder. Verify downloaded public assets and save the final handoff. Do not
> blindly copy old files, regenerate the signing key, force-push main, rerun a
> dated deployment script or install official binaries over the fork.
