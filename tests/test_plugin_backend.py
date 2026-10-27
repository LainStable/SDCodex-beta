import os
import sys
import tempfile
import time
import types
import unittest
from unittest.mock import patch

from flask import Flask

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import plugin_backend


class PluginBackendTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = os.path.join(self.temp.name, "media")
        os.mkdir(self.root)
        self.instance = os.path.join(self.temp.name, "instance")
        os.mkdir(self.instance)
        self.app = Flask(__name__, instance_path=self.instance)
        self.app.register_blueprint(plugin_backend.ai_renamer_bp)
        self.client = self.app.test_client()
        self.auth_package = types.ModuleType("sdcodex")
        self.auth_module = types.ModuleType("sdcodex.auth")
        self.auth_module.current_user = lambda: object()
        self.auth_package.auth = self.auth_module
        self.modules = patch.dict(sys.modules, {
            "sdcodex": self.auth_package,
            "sdcodex.auth": self.auth_module,
        })
        self.modules.start()
        self.env = patch.dict(os.environ, {"AI_RENAMER_ROOT": self.root})
        self.env.start()
        plugin_backend._jobs.clear()
        plugin_backend._last_undo = None

    def tearDown(self):
        self.env.stop()
        self.modules.stop()
        self.temp.cleanup()

    def test_filename_formatting_preserves_supported_case_choices(self):
        self.assertEqual(plugin_backend._format_name("Cyber Punk portrait", "kebabCase", 35), "cyber-punk-portrait")
        self.assertEqual(plugin_backend._format_name("Cyber Punk portrait", "camelCase", 35), "cyberPunkPortrait")
        self.assertEqual(plugin_backend._format_name("Cyber Punk portrait", "snakeCase", 35), "cyber_punk_portrait")
        self.assertEqual(plugin_backend._format_name("AI.generated portrait", "kebabCase", 35), "ai-generated-portrait")

    def test_existing_names_get_a_unique_suffix(self):
        original = os.path.join(self.root, "original.png")
        first = os.path.join(self.root, "suggested.png")
        second = os.path.join(self.root, "suggested1.png")
        for path in (original, first, second):
            with open(path, "wb"):
                pass
        self.assertEqual(plugin_backend._new_name(original, "suggested"), os.path.join(self.root, "suggested2.png"))

    def test_path_resolution_rejects_escape(self):
        with self.assertRaisesRegex(ValueError, "outside the configured media root"):
            plugin_backend._resolve_path(os.path.join(self.root, "..", "outside.png"), must_exist=False)
        with self.assertRaisesRegex(ValueError, "inside the configured media root"):
            plugin_backend._resolve_path("../outside.png", must_exist=False)

    def test_video_tools_prefers_host_binaries(self):
        with patch.object(plugin_backend.shutil, "which", side_effect=lambda name: f"/usr/bin/{name}"):
            self.assertEqual(plugin_backend._video_tools(), ("/usr/bin/ffmpeg", "/usr/bin/ffprobe"))

    def test_video_tools_fall_back_to_the_bundled_ffmpeg(self):
        bundled = types.ModuleType("imageio_ffmpeg")
        bundled.get_ffmpeg_exe = lambda: "/pkg/imageio_ffmpeg/binaries/ffmpeg"
        with patch.dict(sys.modules, {"imageio_ffmpeg": bundled}), patch.object(
            plugin_backend.shutil, "which", side_effect=lambda name: "/usr/bin/ffprobe" if name == "ffprobe" else None
        ):
            self.assertEqual(
                plugin_backend._video_tools(),
                ("/pkg/imageio_ffmpeg/binaries/ffmpeg", "/usr/bin/ffprobe"),
            )

    def test_video_tools_survive_a_missing_bundled_ffmpeg(self):
        def get_ffmpeg_exe():
            raise RuntimeError("No ffmpeg exe could be found.")

        bundled = types.ModuleType("imageio_ffmpeg")
        bundled.get_ffmpeg_exe = get_ffmpeg_exe
        with patch.dict(sys.modules, {"imageio_ffmpeg": bundled}), patch.object(
            plugin_backend.shutil, "which", return_value=None
        ):
            self.assertEqual(plugin_backend._video_tools(), (None, None))

    def test_video_duration_uses_ffprobe_when_present(self):
        calls = []

        def run(args, **kwargs):
            calls.append(args)
            return types.SimpleNamespace(stdout="12.5\n", stderr="")

        with patch.object(plugin_backend.subprocess, "run", side_effect=run):
            self.assertEqual(plugin_backend._video_duration("clip.mp4", "/usr/bin/ffmpeg", "/usr/bin/ffprobe"), 12.5)
        self.assertEqual(calls[0][0], "/usr/bin/ffprobe")

    def test_video_duration_falls_back_to_ffmpeg_output(self):
        stderr = "Input #0, mov, from 'clip.mp4':\n  Duration: 00:01:02.50, start: 0.000000, bitrate: 800 kb/s\n"
        with patch.object(plugin_backend.subprocess, "run", return_value=types.SimpleNamespace(stdout="", stderr=stderr)):
            self.assertEqual(plugin_backend._video_duration("clip.mp4", "/usr/bin/ffmpeg", None), 62.5)

    def test_video_duration_rejects_unreadable_media(self):
        with patch.object(plugin_backend.subprocess, "run", return_value=types.SimpleNamespace(stdout="", stderr="nope")):
            with self.assertRaisesRegex(RuntimeError, "Could not determine video duration"):
                plugin_backend._video_duration("clip.mp4", "/usr/bin/ffmpeg", None)

    def test_settings_redact_api_key_and_authenticate_endpoints(self):
        saved = self.client.post("/betarenamer/api/settings", json={
            "api_key": "private-test-key",
            "provider": "openai",
            "base_url": "https://api.openai.com",
        })
        self.assertEqual(saved.status_code, 200, saved.get_json())
        self.assertEqual(saved.get_json()["api_key"], "")
        self.assertTrue(saved.get_json()["api_key_set"])
        fetched = self.client.get("/betarenamer/api/settings")
        self.assertEqual(fetched.get_json()["api_key"], "")
        self.assertTrue(fetched.get_json()["api_key_set"])

        self.auth_module.current_user = lambda: None
        denied = self.client.get("/betarenamer/api/status")
        self.assertEqual(denied.status_code, 401)

    def test_dry_run_apply_and_undo(self):
        original = os.path.join(self.root, "old-image.png")
        with open(original, "wb") as image:
            image.write(b"image bytes")

        scan = self.client.get("/betarenamer/api/files")
        self.assertEqual(scan.status_code, 200)
        staged = scan.get_json()["files"][0]

        with patch.object(plugin_backend, "_request_name", return_value="suggested name"):
            response = self.client.post("/betarenamer/api/dry-run", json={
                "files": [staged],
                "settings": {
                    "provider": "ollama",
                    "model": "llava",
                    "base_url": "http://127.0.0.1:11434",
                    "case": "kebabCase",
                },
            })
            self.assertEqual(response.status_code, 202, response.get_json())
            job_id = response.get_json()["job_id"]
            for _ in range(100):
                job_response = self.client.get(f"/betarenamer/api/jobs/{job_id}")
                job = job_response.get_json()
                if job["status"] == "complete":
                    break
                time.sleep(0.01)
            self.assertEqual(job["items"][0]["status"], "ready")
            self.assertNotIn("api_key", job)

            renamed = self.client.post(f"/betarenamer/api/jobs/{job_id}/rename", json={
                "paths": [staged["path"]],
                "names": {staged["path"]: "Reviewed New Name"},
            })
            self.assertEqual(renamed.status_code, 200, renamed.get_json())
            new_path = os.path.join(self.root, "reviewed-new-name.png")
            self.assertTrue(os.path.isfile(new_path))

            undo = self.client.post("/betarenamer/api/undo", json={
                "undo_token": renamed.get_json()["undo_token"],
            })
            self.assertEqual(undo.status_code, 200, undo.get_json())
            self.assertTrue(os.path.isfile(original))
            self.assertFalse(os.path.exists(new_path))


if __name__ == "__main__":
    unittest.main()
