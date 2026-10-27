"""SDCodex Beta entrypoint (Stitch UI + slim JSON API).

Beta channel of the Gallery plugin: same pages and handlers, mounted under its
own blueprint names and ``/beta`` API prefix so it can be installed alongside
the stable Gallery plugin in one instance.
"""

import os
import sys

plugin_dir = os.path.dirname(os.path.abspath(__file__))
if plugin_dir not in sys.path:
    sys.path.insert(0, plugin_dir)


def init_plugin(app, db, plugin_info=None):
    """Register the beta gallery and legacy /beta2 compatibility blueprints."""
    from beta_gallery_backend import beta_gallery_bp

    if "beta_gallery_bp" not in app.blueprints:
        app.register_blueprint(beta_gallery_bp)
        app.logger.info("SDCodex Beta mounted at /beta/api")

    # Legacy compatibility: ComfyUI custom nodes that still hit /beta2/api/*
    # are silently re-routed to the same handlers via a second blueprint mount.
    if "beta_gallery2_compat" not in app.blueprints:
        from flask import Blueprint
        from beta_gallery_backend import (
            gallery_status, list_folders, list_media, get_media_file,
            get_media_meta, save_caption, download_workflow, rescan_cache,
            export_workflows, caption_target, get_prompt,
            browse_folders, browse_roots, require_login,
        )

        g2 = Blueprint("beta_gallery2_compat", __name__, url_prefix="/beta2/api")
        g2.before_request(require_login)
        g2.add_url_rule("/status",                "g2_status",          gallery_status)
        g2.add_url_rule("/folders",               "g2_folders",         list_folders)
        g2.add_url_rule("/media",                 "g2_media",           list_media)
        g2.add_url_rule("/file/<path:relpath>",   "g2_file",            get_media_file)
        g2.add_url_rule("/meta",                  "g2_meta",            get_media_meta)
        g2.add_url_rule("/caption",               "g2_caption_save",    save_caption,    methods=["POST"])
        g2.add_url_rule("/workflow",              "g2_workflow",        download_workflow)
        g2.add_url_rule("/rescan",                "g2_rescan",          rescan_cache,    methods=["POST"])
        g2.add_url_rule("/export-workflows",      "g2_export",          export_workflows)
        g2.add_url_rule("/caption-target",        "g2_caption_target",  caption_target)
        g2.add_url_rule("/prompt",                "g2_prompt",          get_prompt)
        g2.add_url_rule("/browse-folders",        "g2_browse_folders",  browse_folders)
        g2.add_url_rule("/browse-roots",          "g2_browse_roots",    browse_roots)
        app.register_blueprint(g2)
        app.logger.info("SDCodex Beta legacy alias mounted at /beta2/api")

    return True
