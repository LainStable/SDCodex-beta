# SDCodex-beta

Beta channels for [SDCodex](https://github.com/LainStable/SDCodex) plugins:
one plugin per branch, each branch published as its own standalone plugin so it
can be installed **side by side** with the stable plugin it previews.

## Branches

| Branch    | Plugin id      | Manifest version | Previews              |
|-----------|----------------|------------------|-----------------------|
| `beta`    | `beta`         | `0.2.0`          | Gallery               |
| `caption` | `betacaption`  | parked           | ComfyCaption          |
| `renamer` | `betarenamer`  | parked           | AI Renamer            |

Only `beta` is published in the Plugin Hub catalogue. The `caption` and
`renamer` branches are parked: their plugins are withdrawn from the store
until they are reworked as standalone channels.

The Docker test environment only ever installs from these branches explicitly;
the stable plugin repos stay clean.

## Rules for a beta channel

Each branch is a **complete plugin repository**, not a patch: the core installer
reads `<repo>/plugin.json` at the repository root (not a subfolder), so every
branch must carry its own full manifest.

A beta channel must be standalone and collision-free:

- **Own plugin id** (`beta`, `betacaption`, `betarenamer`). The id also selects
  the install folder under `<instance>/plugins/`.
- **Own API prefix.** Never reuse the stable plugin's routes (`/gallery/api`,
  `/caption/api`, …). Gallery uses `/beta/api` and the legacy alias `/beta2/api`.
- **Own Python module names.** The core imports every plugin module into one
  shared `sys.modules`, so a generic name such as `gallery_backend` would make
  the beta silently reuse the stable module and never register its blueprints.
  Gallery therefore ships `beta_gallery_backend.py` (compare
  `betacaption_backend.py`, `betarenamer_gallery_browser.py`).
- **Own blueprint names** (`beta_gallery_bp`, `beta_gallery2_compat`), because
  Flask blueprint names are global to the app.
- **Own env vars and storage.** Gallery reads `BETA_GALLERY_ROOT` (falling back
  to `GALLERY_ROOT`/`GALLERY`). It never writes the stable plugin's files.
- **Own frontend routes.** Plugin pages and `legacy-ui` assets are addressed as
  `/api/plugins/beta/page/<page>` and `/beta/api/...`.
- **No cross-channel calls.** A beta talks only to its own API; it must not
  depend on a stable plugin being installed.

## This branch: `beta`

`beta` is a renamed copy of the stable
[Gallery](https://github.com/LainStable/SDCodex-Gallery) plugin and doubles as
the boilerplate for future beta channels: copy this branch, rename the
module/blueprint/route/env names, point `plugin.json` at the new id and branch,
and publish.

It keeps the essentials — disk browsing, the media viewer, the workflow/caption
inspector, the caption-target endpoint, the shared folder picker, the source
rail, scan/rescan status and the queue strip.

Four stable-Gallery features were deliberately **removed** from this channel,
so new beta channels start lean:

- **Immich integration** — no remote-server sources, no `/beta/api/immich`
  routes and no Immich settings card; the gallery browses the local disk only.
- **Convert** — no image-format conversion endpoints or buttons.
- **Optimize** — no jpegoptim/oxipng folder optimisation endpoints or buttons.
- **Favourites / starred media** — no star buttons, starred filter or saved
  starred set.
