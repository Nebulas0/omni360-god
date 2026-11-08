# Linux/Wine builder and automatic signed releases

ZenServer builds the fork with the actual Xbox 360 XDK compiler, librarian,
linker, font bundler and `imagexex`, under Wine in a persistent Linux container.
It builds both Visual Studio projects from source, including Foundry. No
Windows VM or Visual Studio IDE is used. The first complete native build passed
on 2026-11-07 with XDK 2.0.21256.17 and Wine 9.0.

The SDK and release private key are **not included** in this repository.
This setup follows the toolchain approach documented by
[xdk-docker](https://github.com/ClementDreptin/xdk-docker); the build driver here
translates this application's Release project settings instead of using MSBuild.

## Normal use

Push reviewed changes to **main**. The existing server/protocol checks run on
GitHub. Every two minutes, the server publisher checks for a passing main commit,
updates the Wine build image when its inputs change, compiles, signs, verifies,
and publishes a new GitHub prerelease. PRs and other branches never execute on
the server. A newer main commit supersedes an older one waiting for checks.

Each build gets a distinct version, such as
`1.0.0-alpha.5.build.190`: the source base version plus the main commit count.
The generated version is compiled through a forced header and recorded with
the exact source SHA and compiler/container identities in `build.json`.
This ordering is tested against the actual Xbox updater's comparison code.
Published tags/assets are never replaced. Keep normal fast-forward main history;
a history rewrite causing a tag collision fails instead of overwriting a build.
Bump the base version in `src/settings.h` for a new development series.
Automatic publication accepts only `alpha.N` and `beta.N` source base versions.

The **Signed XEX release (ZenServer Wine)** GitHub workflow waits for the private
server build, downloads its public assets, checks the checksum and signature
against `src/UpdateKey.h`, and exposes them as Actions artifacts too. Both native packaging and download
verification check loader fields, image/payload bounds, entry point, title ID
and embedded font/license resources; damaged containers are rejected. It uses
only GitHub's automatic read token, not a secret SDK or signing key on GitHub.

Download `Omni360.xex` from this fork's releases and copy it into a dedicated
folder on your console for the first install. Open **X: Foundry** in the Store
and pair at your server's `/consoles` page. Archive.org keys are for the original
upstream store. Later fork versions are offered in **Settings > Updates**.
An upstream Omni360 binary uses another repository/key and needs that first
manual replacement. Physical console launch, game/DLC/TU and recovery tests
are still required; an automated build is an alpha test release.

## Installed server layout

- `/opt/xbox360/builder/`: Docker recipe and trusted host publisher.
- `/opt/xbox360/xex-builder/sdk/`: private extracted SDK, read-only in Wine.
- `/opt/xbox360/xex-builder/source/`: public main checkout, read-only in Wine.
- `/opt/xbox360/xex-builder/build/`: disposable Wine prefix, objects and logs.
- `/opt/xbox360/xex-builder/releases/`: immutable signed build pairs/metadata.
- `/opt/xbox360/xex-builder/automation/`: private token/config/key and status.

The `xbox360-xex-builder` service is in the existing Xbox Compose project with
profile `builder`. It stays running, has no network or public ports, runs as UID
1000 with dropped capabilities, and receives neither signing key nor token.
The website container and game output volumes are independent.

```sh
cd /opt/xbox360
docker compose --profile builder up -d --no-deps xbox360-xex-builder
docker exec xbox360-xex-builder xvfb-run -a python3 /opt/builder/build.py
sudo systemctl status omni360-release.timer
cat xex-builder/automation/status.json
tail -60 xex-builder/build/native-build.log
# Trigger a check immediately (normal timer interval is two minutes):
sudo systemctl start omni360-release.service
```

Warm builds are locked and clear the private Wine PDB service before/after
compilation. Their caches include source/header, effective-version and compiler
identities. Failed builds publish no files. The host signs only after matching
the output metadata and checking that the private key matches the fork header.

## Recreating the private setup

Supply a complete XDK tree with compiler runtimes, `include/xbox`, `lib/xbox`,
`Bundler.exe` and `imagexex.exe`. Neighborhood alone is insufficient. The
`extract_sdk.py` helper verifies a supplied installer SHA-256, extracts its CABs
without executing the Windows installer, and applies the install manifest's
file-copy operations. For 21256.17, that means 15 CABs and 721 shared-file copies;
plain CAB extraction alone misses required CRT/STL headers. It ignores driver,
registry, shortcut and command execution steps.

Stage the resulting `files/XDK` tree at the SDK path, and set the bind paths in
`compose.yml` or private environment variables. Build the image, check the SDK,
and test a complete native build before enabling the publisher. Record the
installer checksum and keep SDK downloads outside every Docker build context.

Host dependencies are Docker Compose, Git, Python 3, OpenSSL and systemd.
`publish.py` uses a private JSON config:

```json
{
  "base": "/opt/xbox360/xex-builder",
  "compose_dir": "/opt/xbox360",
  "token_file": "/opt/xbox360/xex-builder/automation/github-token",
  "signing_key": "/opt/xbox360/xex-builder/automation/release-signing.pem"
}
```

Keep the automation directory mode 700 and secret files mode 600. The token
needs repository contents read/write (releases/tags) and Actions read
(verification). Runner administration and Actions-secret management are not
needed. Renew it when it expires; revocation stops publication. The publisher
authenticates only to GitHub API/upload hosts and refuses redirects carrying
credentials. Public artifact downloads carry no token.

Install the supplied service/timer with the correct server user, then enable
`omni360-release.timer`. The host publisher itself is deployed deliberately;
it does not replace itself with code from new commits. The compiler receives
no Docker socket. No private SDK, key, credentials or game data may be added
to the repository, image build context, Actions artifact or release.

## Verification downloads

The Actions verifier downloads public assets through GitHub's binary asset API
first, with a public browser-download fallback and at most four fresh attempts
for temporary network errors. It logs the asset and attempt, applies a bounded
socket timeout and the overall verification deadline, and writes completed
files atomically. Neither route nor redirected CDN requests receive the API
token or cookies.

Verification requires all four uploaded assets, matching declared sizes and
available GitHub digests, every SHA256SUMS entry, the native XEX loader fields,
and the existing fork ECDSA signature. Hash/signature mismatches fail the job;
retries do not bypass validation. Missing Actions artifacts are errors.

A network timeout in this verification job does not mean the private compiler
failed. Check the trusted host status and public release, then rerun the failed
job. Its verification and artifact-upload steps must both pass before calling
the Actions run complete. Never replace an existing published XEX to retry a
verification download.
