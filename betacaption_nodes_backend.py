"""Admin-managed ComfyUI custom-node directory and installer."""

import os
import shutil

from flask import Blueprint, current_app, jsonify, request

from betacaption_models_backend import load_settings, save_settings

nodes_bp = Blueprint("beta_comfyui_nodes_bp", __name__, url_prefix="/betacaption/models/nodes")
PLUGIN_DIR = os.path.dirname(os.path.abspath(__file__))
NODE_PACK = os.path.join(PLUGIN_DIR, "comfyui-sdcodex")
NODE_PACK_NAME = "comfyui-sdcodex"


def _admin_required():
    from sdcodex.auth import current_user

    user = current_user()
    if user is None:
        return jsonify({"ok": False, "error": "Sign in to configure ComfyUI nodes."}), 401
    if not user.is_admin:
        return jsonify({"ok": False, "error": "Only an administrator can configure ComfyUI nodes."}), 403
    return None


def _validate_install_dir(value):
    raw_path = str(value or "").strip()
    if not raw_path:
        raise ValueError("Choose an existing ComfyUI installation directory.")
    path = os.path.realpath(os.path.abspath(os.path.expanduser(raw_path)))
    if not os.path.isdir(path):
        raise ValueError("Choose an existing ComfyUI installation directory.")
    if not os.path.isfile(os.path.join(path, "main.py")):
        raise ValueError("The selected directory is not a ComfyUI installation (main.py was not found).")
    return path


def _node_destination(install_dir, create=False):
    root = _validate_install_dir(install_dir)
    custom_nodes = os.path.join(root, "custom_nodes")
    if os.path.lexists(custom_nodes) and os.path.islink(custom_nodes):
        raise ValueError("ComfyUI custom_nodes must not be a symbolic link.")
    if os.path.lexists(custom_nodes) and not os.path.isdir(custom_nodes):
        raise ValueError("The ComfyUI custom_nodes path exists but is not a directory.")
    if create:
        os.makedirs(custom_nodes, exist_ok=True)
    if not os.path.isdir(custom_nodes):
        return root, custom_nodes, os.path.join(custom_nodes, NODE_PACK_NAME)
    resolved_root = os.path.realpath(root)
    resolved_custom_nodes = os.path.realpath(custom_nodes)
    if os.path.commonpath((resolved_root, resolved_custom_nodes)) != resolved_root:
        raise ValueError("ComfyUI custom_nodes resolves outside the installation directory.")
    destination = os.path.join(custom_nodes, NODE_PACK_NAME)
    if os.path.lexists(destination) and os.path.islink(destination):
        raise ValueError("The existing SDCodex node-pack folder must not be a symbolic link.")
    if os.path.lexists(destination) and not os.path.isdir(destination):
        raise ValueError("The existing SDCodex node-pack destination is not a directory.")
    return root, custom_nodes, destination


def _contains_symlinks(path):
    for current, dirs, files in os.walk(path, followlinks=False):
        if any(os.path.islink(os.path.join(current, name)) for name in dirs + files):
            return True
    return False


def _file_count(root):
    count = 0
    for _, dirs, files in os.walk(root, followlinks=False):
        dirs[:] = [name for name in dirs if name not in {".git", "__pycache__"}]
        count += sum(not name.endswith(".pyc") for name in files)
    return count


def _current_config():
    install_dir = str(load_settings().get("comfyui_dir", "")).strip()
    if not install_dir:
        return {"install_dir": "", "custom_nodes_dir": "", "installed": False, "valid": False}
    try:
        root, _, destination = _node_destination(install_dir)
    except (OSError, ValueError):
        return {"install_dir": install_dir, "custom_nodes_dir": "", "installed": False, "valid": False}
    return {
        "install_dir": root,
        "custom_nodes_dir": os.path.join(root, "custom_nodes"),
        "installed": os.path.isfile(os.path.join(destination, "__init__.py")),
        "valid": True,
    }


@nodes_bp.get("")
def get_config():
    error = _admin_required()
    if error:
        return error
    return jsonify({"ok": True, **_current_config()})


@nodes_bp.put("/directory")
def set_directory():
    error = _admin_required()
    if error:
        return error
    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        return jsonify({"ok": False, "error": "Provide the ComfyUI installation directory."}), 400
    try:
        install_dir = _validate_install_dir(data.get("install_dir"))
        save_settings(comfyui_dir=install_dir)
        return jsonify({"ok": True, **_current_config()})
    except (OSError, ValueError) as exc:
        return jsonify({"ok": False, "error": str(exc)}), 400


@nodes_bp.post("/install")
def install_nodes():
    error = _admin_required()
    if error:
        return error

    install_dir = str(load_settings().get("comfyui_dir", "")).strip()
    if not install_dir:
        return jsonify({"ok": False, "error": "Set the ComfyUI installation directory first."}), 409
    try:
        if (
            os.path.islink(NODE_PACK)
            or not os.path.isdir(NODE_PACK)
            or not os.path.isfile(os.path.join(NODE_PACK, "__init__.py"))
        ):
            raise FileNotFoundError("The ComfyCaption custom-node pack is missing from this plugin installation.")
        if _contains_symlinks(NODE_PACK):
            raise ValueError("The bundled ComfyCaption node pack contains symbolic links and cannot be installed safely.")
        root, custom_nodes, destination = _node_destination(install_dir, create=True)
        if os.path.isdir(destination) and _contains_symlinks(destination):
            raise ValueError("The existing SDCodex node-pack folder contains symbolic links; remove them before updating.")
        shutil.copytree(
            NODE_PACK,
            destination,
            dirs_exist_ok=True,
            ignore=shutil.ignore_patterns(".git", "__pycache__", "*.pyc"),
        )
        copied = _file_count(NODE_PACK)
        current_app.logger.info("Installed %d ComfyCaption node-pack files to %s", copied, destination)
        return jsonify({
            "ok": True,
            "install_dir": root,
            "custom_nodes_dir": custom_nodes,
            "destination": destination,
            "files_copied": copied,
            "message": f"Installed {copied} node-pack files. Restart ComfyUI to load or update the nodes.",
        })
    except ValueError as exc:
        return jsonify({"ok": False, "error": str(exc)}), 400
    except (OSError, shutil.Error) as exc:
        current_app.logger.exception("ComfyCaption node installation failed")
        return jsonify({"ok": False, "error": str(exc)}), 500
