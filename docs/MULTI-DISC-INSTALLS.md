# Foundry multi-disc installations

Open a Foundry game, choose its source version, then Install. For a recognized
disc set the app opens a checklist with the available matching discs selected.
A toggles a disc, X selects/clears supported discs, Y shows the full source name
and its instructions, and START continues. You can install one disc or the set.
Missing discs and unknown complete counts are reported; the app does not invent
downloads for archives your server does not have.

Sets come from the original Omni360 release groupings where an exact source
match exists. Otherwise matching removes only numbered disc/installer markers,
retaining the region, languages, revision and edition. Ambiguous duplicate
sources fall back to the chosen source. The server rechecks the selected set
and actual package identities before publishing any files to the Xbox.

| Disc type | Preparation and destination |
| --- | --- |
| Playable | Convert to GoD, install under `games-path:\<TitleID>\00007000\<package>` |
| Install/add-on content | Extract supported packages to `xbla-path:\<TitleID>\00000002\<package>` |
| Mixed | Install the GoD and the extracted content packages |
| Unsupported special layout | Explain the exception; do not queue it as an ordinary GoD |

The paths above use the existing settings. When both roots are the standard
`Hdd1:\Content\0000000000000000`, the files go into the corresponding official
content folders. Existing discs and unrelated DLC are preserved. GoD licenses
are never unlocked. If content discs are selected, the existing unlock/preserve
choice applies only to their XBLA/DLC packages.

One durable `disc_bundle` request prepares all selected sources, records each
source's preparation job, and publishes one checked manifest. Preparation can
resume after a server restart; downloads keep the existing Range/ETag/hash,
partial-file and transaction-recovery checks. Queue file totals and ETA cover
the combined manifest, and GoD transfers identify their disc number.

Disc sets require **server preparation** in this version. The original single
disc conversion choices and Archive.org provider remain available; Archive.org
does not gain these Foundry-specific placement rules or this new checklist.
Previously prepared GoDs lacking header disc fields remain readable. Nonzero
disc metadata is checked against the executable. Replacement distinguishes
disc number as well as Title ID / Media ID where available.

## Exceptions and gameplay

Rules are based on the [Iso2God compatibility / installation notes](https://github.com/r4dius/Iso2God#compatibility--installation-notes)
and actual extracted package metadata. A catalog hint cannot replace a real
package Title ID. New rules need a source, synthetic coverage, and Xbox testing.

- GTA V and many GOTY editions have an install/content disc and a play disc;
  they do not need two separately playable GoDs.
- Splinter Cell: Blacklist Disc 2 needs both its playable GoD and extracted
  content; the server retains both.
- Wolfenstein: The New Order Disc 1 remains a GoD installer. Its instructions
  tell you to launch it once on the Xbox before playing the other discs.
- Assassin's Creed IV's multiplayer disc is not selected automatically.
- Watch_Dogs requires merging disc contents into a custom image. This release
  does not perform that merge and prevents separate automatic GoD installation.
- Multi-title collections requiring different Title IDs are rejected by the
  combined installer rather than silently treated as one game.

Installing all playable discs is separate from switching discs during gameplay.
The app exits when a game launches. It does not install a resident disc-swap
plugin or change ABadAvatar/XeUnshackle/Metro configuration. Automatic in-game
switching depends on the game and the console's loader and needs console testing.
An installed set is not a promise that Metro will swap discs automatically.
