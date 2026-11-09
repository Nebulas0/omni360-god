# Avatar items in Foundry (alpha.11)

Avatar item packages use content type `00009000`. Find their archives under
**XBLA / DLC** on the website or **DLC / add-ons** in the Foundry Store. Archives
marked `(Addon)` may contain gameplay DLC, Avatar items, or both; actual package
metadata determines installation. Packs can be prepared while the Xbox is off.

Update the fork to **alpha.11 or newer** before installing an Avatar pack. Older
clients reject the new manifest kind. Ordinary GoD, XBLA, DLC and title-update
installations continue to work with older clients.

- **Unlock XBLA / DLC / Avatar items** preserves Avatar packages on the server.
  On the Xbox, the installer detects the online XUIDs of compatible signed-in
  profiles, assigns those owners to a staged copy, changes its Title/asset ID
  suffix to `AAAAAAAA`, and recomputes its metadata hash. It installs under the
  configured content root at `AAAAAAAA\00009000\<32-hex asset ID>`.
- **Leave licenses unchanged** installs the supplied Avatar package unchanged
  under its original Title ID. This does not grant ownership to another profile.

A purely offline profile's local `E...` profile ID is not an online XUID and
cannot own these items. A previously Live-enabled profile, or an offline profile
activated separately with a compatible tool, can supply the online XUID while
signed in locally. No Xbox Live connection is needed for this installation step.
Omni360 does not activate, convert, sign out or modify profiles, and it never
sends their XUIDs to the website. No manual XUID entry or new API key is required.

Without a compatible signed-in profile, **Prepared for Xbox** labels Avatar
unlock requests accordingly and skips them in **Download all ready** and
automatic downloads. Other ready games/DLC can proceed. Mixed packs containing
Avatar unlocks wait as a whole. A directly queued Avatar install retains verified
downloads if the required profile is unavailable. Sign in with a compatible
profile, refresh the prepared list, and install then. Server copies retain the
normal quota/expiry (default 14 days); expired copies can be prepared again.

Already installed item owners are retained when adding another profile; a full
license table is rejected without replacing the installed item. To add a profile
later, create a fresh website preparation and install it while that profile is
signed in. Completed Xbox receipts intentionally prevent reinstalling the same
completed request automatically. Keep source packs for future ownership changes.

Raw downloads keep their original hashes for resume. Ownership is applied to a
separate staged copy, checked again, and published through the existing recovery
journal. Interrupted publication rolls back; temporary copies are cleaned by the
normal successful-install cleanup. Profiles and existing gameplay DLC are not
modified. The server never reports an Avatar package as already unlocked solely
because its license block is empty.

After installation, open the dashboard Avatar editor. Gender-specific items only
appear for the matching avatar. The console also needs the matching dashboard
Avatar system data; this feature does not install a dashboard update. Actual
wardrobe visibility still needs testing on the target Xbox.

The metadata method is documented by
[Avatar Thrift Store](https://github.com/F4b1-/avatar-thrift-store), MIT licensed;
its notice is in [licenses/avatar-thrift-store-MIT.txt](../licenses/avatar-thrift-store-MIT.txt).
Its separate profile activation feature is not included in this fork.

## Protocol and maintenance

The server advertises `avatar_items`, `avatar_min_client_version` and
`avatar_unlock: online_xuid_on_console` in capabilities. Avatar manifests include
`min_client_version: 1.0.0-alpha.11`; each `avatar` component has an explicit
boolean `avatar_unlock`, `content_type: 00009000` and a normal verified package
file. `license_state` is `on_console` or `preserved`, never a promise that the
current profile can wear it. Original Title IDs still enforce game/add-on matches.
Inbox rows expose `contains_avatar` and `avatar_unlock` for profile gating.

Preserve the special exclusion from `server/app/unlock.py`: generic license
zeroing is not Avatar ownership. Tests cover both license modes, 33-package packs,
manifest validation, nonlocal XUID detection, prior-owner preservation, a full
license table, payload integrity and transaction recovery. The XDK build and
signed release checks accompany the portable tests; physical-console acceptance
remains separate.
