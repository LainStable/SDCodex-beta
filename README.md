# SDCodex-ComfyCaption

## Install the ComfyUI node pack

Open the plugin's **Caption Models** settings page, enter the ComfyUI installation
directory, and select **Set Directory**. Once validated, select **Install Nodes**
to copy the bundled `comfyui-sdcodex` pack into
`<ComfyUI directory>/custom_nodes/comfyui-sdcodex`. Existing files in that node
pack directory are updated in place; unrelated custom nodes are left untouched.
Restart ComfyUI after installing or updating the pack.

In a local SDCodex installation, enter the ComfyUI path as seen by the local
backend process. In Docker mode, mount the ComfyUI installation directory into
the SDCodex container and enter its **container path**. The default plugin
volume maps `./comfyui` to `/data/comfyui`; change that host path to your actual
ComfyUI installation if needed. The selected folder must contain `main.py`.
Only signed-in administrators can save the target directory or install nodes.
Directory selection and node installation are explicit; installing the plugin
does not write into ComfyUI until an administrator selects **Install Nodes**.