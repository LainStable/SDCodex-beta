import { app } from "../../scripts/app.js";

// SDCodex Gallery Runner
// -----------------------
// A control node that cycle-runs the workflow across the images displayed by a
// connected SDCodexGalleryBrowser node. Usage:
//   1. Drop a Gallery Runner onto the canvas.
//   2. Connect the Gallery Browser's `selected_image` output into the Runner's
//      `current_image` input (or just have a Gallery Browser in the graph).
//   3. Set `executions_per_image` (how many times to run on each image) and
//      `max_images` (images to cycle; 0/blank = until end of list).
//   4. Press ▶ Start to run the workflow for the currently selected image,
//      then advance to the next image and run again until Stop / max reached.
//
// The loop is driven here on the frontend: for each image (and each execution
// repeat) it syncs the selection onto the Gallery Browser + Gallery Loader
// nodes, serializes the current graph, queues it via the ComfyUI API, and
// waits for that exact prompt_id to finish before advancing.

const style = document.createElement("style");
style.innerHTML = `
    .sdcodex-runner-container {
        display: flex;
        flex-direction: column;
        gap: 6px;
        background: #1e1e24;
        border: 1px solid #3e3e4a;
        border-radius: 6px;
        padding: 8px;
        font-family: Arial, sans-serif;
        color: #eee;
        width: 100%;
        box-sizing: border-box;
        margin: 5px 0;
    }
    .sdcodex-runner-buttons {
        display: flex;
        gap: 6px;
    }
    .sdcodex-runner-btn {
        flex: 1;
        background: #2e7d32;
        border: 1px solid #4caf50;
        color: #fff;
        border-radius: 4px;
        padding: 6px 10px;
        cursor: pointer;
        font-size: 12px;
        font-weight: bold;
        transition: background 0.15s, opacity 0.2s;
    }
    .sdcodex-runner-btn.stop {
        background: #b71c1c;
        border: 1px solid #e57373;
    }
    .sdcodex-runner-btn.running {
        opacity: 0.5;
        cursor: not-allowed;
    }
    .sdcodex-runner-status {
        background: #101014;
        border: 1px solid #2a2a30;
        border-radius: 4px;
        padding: 6px 8px;
        font-size: 11px;
        color: #888;
        min-height: 16px;
        word-break: break-all;
    }
`;
document.head.appendChild(style);

app.registerExtension({
    name: "SDCodex.GalleryRunner",
    async nodeCreated(node) {
        if (node.comfyClass !== "SDCodexGalleryRunner") return;

        const helpers = window.SDCodexGalleryHelpers || {};
        let running = false;
        let stopRequested = false;

        const getWidget = (n, name) => n?.widgets?.find(w => w.name === name);
        const readWidgetStr = (n, name) => {
            const w = getWidget(n, name);
            return w ? String(w.value ?? "") : "";
        };

        // ---------- DOM controls ----------
        const container = document.createElement("div");
        container.className = "sdcodex-runner-container";

        const buttons = document.createElement("div");
        buttons.className = "sdcodex-runner-buttons";

        const startBtn = document.createElement("button");
        startBtn.className = "sdcodex-runner-btn";
        startBtn.textContent = "▶ Start";
        startBtn.addEventListener("click", () => runLoop());

        const stopBtn = document.createElement("button");
        stopBtn.className = "sdcodex-runner-btn stop";
        stopBtn.textContent = "■ Stop";
        stopBtn.addEventListener("click", () => stopLoop());

        buttons.appendChild(startBtn);
        buttons.appendChild(stopBtn);

        const statusEl = document.createElement("div");
        statusEl.className = "sdcodex-runner-status";
        statusEl.textContent = "Idle";

        container.appendChild(buttons);
        container.appendChild(statusEl);

        const domWidget = node.addDOMWidget("sdcodex_runner_widget", "runner_controls", container);
        domWidget.serializeValue = () => undefined;

        const setStatus = (text) => {
            statusEl.textContent = text;
            if (node.setDirtyCanvas) node.setDirtyCanvas(true, true);
        };

        // ---------- ComfyUI orchestration ----------
        const findBrowserNode = () => {
            const inp = node.inputs?.find(i => i.name === "current_image");
            if (inp && inp.link !== null && inp.link !== undefined) {
                for (const n of app.graph?._nodes || []) {
                    for (const o of n.outputs || []) {
                        if (o.links && o.links.includes(inp.link)) return n;
                    }
                }
            }
            return (app.graph?._nodes || []).find(n => n.comfyClass === "SDCodexGalleryBrowser");
        };

        const loadFolderImages = async (folderPath) => {
            const resp = await fetch(
                `/sdcodex/images?folder_path=${encodeURIComponent(folderPath)}&page=1&limit=100000`
            );
            if (!resp.ok) throw new Error((await resp.text()) || "Failed to load images from folder");
            const data = await resp.json();
            return data.images || [];
        };

        const waitForPrompt = (promptId) => {
            if (!app.api) return;
            return new Promise((resolve) => {
                const finish = () => {
                    app.api.removeEventListener("execution_success", onSuccess);
                    app.api.removeEventListener("execution_interrupted", onInterrupt);
                    app.api.removeEventListener("execution_error", onError);
                    resolve();
                };
                const matches = (evt) => {
                    const data = evt && evt.detail;
                    const pid = data && data.prompt_id;
                    return !pid || pid === promptId;
                };
                const onSuccess = (evt) => { if (matches(evt)) finish(); };
                const onInterrupt = (evt) => { if (matches(evt)) finish(); };
                const onError = (evt) => { if (matches(evt)) finish(); };
                app.api.addEventListener("execution_success", onSuccess);
                app.api.addEventListener("execution_interrupted", onInterrupt);
                app.api.addEventListener("execution_error", onError);
            });
        };

        const queueCurrentGraph = async () => {
            if (typeof app.graphToPrompt !== "function" || !app.api) {
                throw new Error("ComfyUI queue API unavailable");
            }
            const p = await app.graphToPrompt();
            const res = await app.api.queuePrompt(0, p);
            return res && res.prompt_id;
        };

        const runLoop = async () => {
            if (running) {
                setStatus("Already running — press ■ Stop first");
                return;
            }
            running = true;
            stopRequested = false;
            startBtn.classList.add("running");
            stopBtn.classList.remove("running");
            try {
                const browser = findBrowserNode();
                if (!browser) {
                    setStatus("Connect a Gallery Browser (or add one to the graph)");
                    return;
                }
                const folderPath = readWidgetStr(browser, "folder_path");
                if (!folderPath) {
                    setStatus("Set folder_path on the Gallery Browser first");
                    return;
                }
                const images = await loadFolderImages(folderPath);
                if (images.length === 0) {
                    setStatus("No images in the folder");
                    return;
                }
                const selected = readWidgetStr(browser, "selected_image");
                let startIdx = images.findIndex(img => img.image_path === selected);
                if (startIdx < 0) startIdx = 0;

                const maxImages = parseInt(readWidgetStr(node, "max_images") || "0", 10);
                const perImage = Math.max(1, parseInt(readWidgetStr(node, "executions_per_image") || "1", 10));
                const endIdx = (maxImages > 0)
                    ? Math.min(images.length, startIdx + maxImages)
                    : images.length;

                let cycled = 0;
                for (let i = startIdx; i < endIdx && !stopRequested; i++) {
                    const img = images[i];
                    const label = `${i + 1}/${endIdx}: ${img.file_name}`;
                    setStatus(`▶ Running ${label}`);

                    if (helpers.syncSelectionNodes) {
                        helpers.syncSelectionNodes(
                            img.image_path || "",
                            img.caption || "",
                            img.sd_prompt || "",
                            img.sd_negative || "",
                            app.graph
                        );
                    }
                    if (app.graph?.setDirtyCanvas) app.graph.setDirtyCanvas(true, true);

                    for (let e = 0; e < perImage && !stopRequested; e++) {
                        setStatus(`▶ ${label}  —  run ${e + 1}/${perImage}`);
                        try {
                            const promptId = await queueCurrentGraph();
                            if (promptId) await waitForPrompt(promptId);
                        } catch (err) {
                            console.error("Queue error:", err);
                            setStatus(`! ${err.message}`);
                        }
                        if (stopRequested) break;
                    }
                    cycled++;
                }

                if (stopRequested) setStatus("■ Stopped");
                else if (maxImages > 0 && cycled >= maxImages) setStatus("■ Done — max images reached");
                else setStatus("■ Done — reached end of list");
            } catch (err) {
                console.error("Runner error:", err);
                setStatus(`! ${err.message}`);
            } finally {
                running = false;
                startBtn.classList.remove("running");
                stopBtn.classList.remove("running");
            }
        };

        const stopLoop = async () => {
            stopRequested = true;
            try {
                if (app.api && typeof app.api.interrupt === "function") {
                    await app.api.interrupt();
                }
            } catch (err) {
                console.error("Interrupt error:", err);
            }
            setStatus("■ Stopping…");
        };

        node.min_size = [340, 120];
        node.setSize?.([360, 130]);
        setTimeout(() => node.onResize?.(node.size), 50);
    }
});