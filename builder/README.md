# Disposable server build environment

The server has `/dev/kvm`. This optional recipe uses the documented
[dockur/windows](https://github.com/dockur/windows) VM container. It needs no
public website or Traefik route; management/RDP ports bind only to localhost.
Access its management console through an SSH tunnel if needed.

It **does not include Visual Studio 2010 or the Xbox 360 XDK**, and is not a
tested native build environment yet. Those tools and their installers must be
provided before it can produce the fork's XEX. Verify their compatibility with
the selected Windows version. Do not interpret a running Windows VM as a
successful Xbox build.

After the tools are available, create a private `.env` containing a strong
`BUILDER_PASSWORD`, pin `OMNI360_BUILDER_IMAGE` to the verified image digest,
and select a private `OMNI360_BUILD_ROOT`. Start with:

```sh
docker compose --profile builder up -d
```

Install the toolchain privately in Windows, copy the source into the shared
folder, and run `tools/build_foundry.ps1` with `XEDK` and the private signing
key path set. A private Windows GitHub runner can use labels `windows,xdk`.
Keep public PRs off that runner and SDK/private-key files out of GitHub.

Stop/remove the container with `docker compose --profile builder down`.
Its VM storage remains under `OMNI360_BUILD_ROOT` for reuse. Delete that
directory only when its source/signing-key backups are safely preserved.
