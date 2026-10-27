# SDCodex-beta

Beta channels for [SDCodex](https://github.com/LainStable/SDCodex) plugins:
one plugin per branch, each branch published as its own standalone plugin so it
can be installed **side by side** with the stable plugin it previews.

## Branches

| Branch    | Plugin id      | Manifest version | Previews              |
|-----------|----------------|------------------|-----------------------|
| `caption` | `betacaption`  | see `plugin.json`| ComfyCaption          |
| `renamer` | `betarenamer`  | see `plugin.json`| AI Renamer            |
| `gallery` | `beta-gallery` | `0.1.0`          | Gallery               |

The Docker test environment only ever installs from these branches explicitly;
the stable plugin repos stay clean.

## Rules for a beta channel

Each branch is a **complete plugin repository**, not a patch: the core installer
reads `<repo>/plugin.json` at the repository root (not a subfolder), so every
branch must carry its own full manifest.

A beta channel must be standalone and collision-free:

- **Own plugin id** (`beta-gallery`, `betacaption`, `betarenamer`). The id also
  selects the install folder under `<instance>/plugins/`.
- **Own API prefix.** Never reuse the stable plugin's routes (`/gallery/api`,
  `/caption/api`, …). Gallery uses `/beta-gallery/api`, `/beta-gallery/api/immich`
  and the legacy alias `/beta-gallery2/api`.
- **Own Python module names.** The core imports every plugin module into one
  shared `sys.modules`, so a generic name such as `gallery_backend` would make
  the beta silently reuse the stable module and never register its blueprints.
  Gallery therefore ships `beta_gallery_backend.py` and
  `beta_gallery_immich_backend.py` (compare `betacaption_backend.py`,
  `betarenamer_gallery_browser.py`).
- **Own blueprint names** (`beta_gallery_bp`, `beta_gallery_immich`,
  `beta_gallery2_compat`), because Flask blueprint names are global to the app.
- **Own env vars and storage.** Gallery reads `BETA_GALLERY_ROOT`
  (falling back to `GALLERY_ROOT`/`GALLERY`) and `BETA_GALLERY_IMMICH_CONFIG`
  (falling back to `GALLERY_IMMICH_CONFIG`), and stores Immich servers in
  `beta-gallery-immich.json`. It never writes the stable plugin's files.
- **Own frontend routes.** Plugin pages and `legacy-ui` assets are addressed as
  `/api/plugins/beta-gallery/page/<page>` and `/beta-gallery/api/...`.
- **No cross-channel calls.** A beta talks only to its own API; it must not
  depend on a stable plugin being installed.

## This branch: `gallery`

`beta-gallery` is a renamed copy of the stable
[Gallery](https://github.com/LainStable/SDCodex-Gallery) plugin (media browser,
caption/workflow inspector, Immich bridge) and doubles as the boilerplate for
future beta channels: copy this branch, rename the module/blueprint/route/env
names, point `plugin.json` at the new id and branch, and publish.
