# SDCodex AI Renamer

An SDCodex plugin for safely staging image and video files, generating filename
suggestions with a vision model, reviewing a dry-run, and applying or undoing
the resulting renames.

## Features

- Folder browser and recursive media scan for common image and video formats.
- Ollama, LM Studio, and OpenAI-compatible vision providers.
- Model discovery, configurable base URL, language, filename case, character
  limit, video frame count, and custom prompt.
- Dry-run preview leaves files untouched until **Rename Now** or **Batch Rename
  All** is selected.
- Rename operations keep the original extension and choose a numbered filename
  when a target already exists; they never intentionally overwrite a file.
- Revert the latest rename batch while the renamed files remain in place.
- All file operations are restricted to the configured downloads root and
  require an authenticated SDCodex session.

## Install

Install **AI Renamer** from SDCodex **Settings → Plugins**. The plugin declares
`./downloads` as its default persistent media volume. Choose a folder under
that volume in the plugin page.

For Docker installs, set the provider URL to an address reachable from the
container. `127.0.0.1` inside a container refers to the container itself; use a
host gateway or LAN address for a model server running elsewhere.

Video analysis needs `ffmpeg`. The plugin requirements install
[`imageio-ffmpeg`](https://pypi.org/project/imageio-ffmpeg/), whose wheel bundles
an `ffmpeg` binary for Linux, macOS, and Windows, so video support works right
after the plugin install with no extra system package and no download on first
use. A system `ffmpeg` on `PATH` is always preferred, and `ffprobe` is optional:
video duration falls back to reading ffmpeg's own output. Image-only use does
not need ffmpeg at all.

## Upstream and license

The provider APIs, image/video analysis options, filename prompt, and output
conventions are adapted from
[ai-renamer](https://github.com/nakedlittlezombie/ai-renamer) (upstream package
version 1.0.24; now hosted at [ozgrozer/ai-renamer](https://github.com/ozgrozer/ai-renamer)),
by Ozgur Ozer. The original project is licensed under GPL-3.0; this plugin is
distributed under the same license.

The upstream CLI renames files as it processes them. This plugin instead adds
a staged review and dry-run before applying changes, plus safe path checks and
undo support.

Run the plugin backend checks with `python -m unittest discover -s tests -v`.