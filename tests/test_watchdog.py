from __future__ import annotations

import importlib.util
import json
import os
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch


def _load_watchdog_module():
    script = Path(__file__).resolve().parents[1] / "keep_server_alive.py"
    spec = importlib.util.spec_from_file_location("watchdog_under_test", script)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class WatchdogTests(unittest.TestCase):
    def test_source_snapshot_tracks_restart_relevant_files_only(self) -> None:
        watchdog = _load_watchdog_module()
        with TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            source_dir = root / "src" / "transcript_bot" / "templates"
            source_dir.mkdir(parents=True)
            (root / "run_server.py").write_text("print('run')", encoding="utf-8")
            page = source_dir / "index.html"
            page.write_text("first", encoding="utf-8")
            (source_dir / "notes.txt").write_text("ignored", encoding="utf-8")

            first = watchdog.source_snapshot(root)
            page.write_text("updated page", encoding="utf-8")
            # Windows filesystems can retain the same mtime for rapid writes.
            # Advance it explicitly so this test checks snapshot behaviour,
            # rather than the filesystem timestamp resolution.
            stat = page.stat()
            os.utime(page, ns=(stat.st_atime_ns, stat.st_mtime_ns + 1_000_000_000))
            second = watchdog.source_snapshot(root)

        self.assertIn("src\\transcript_bot\\templates\\index.html", first)
        self.assertNotIn("src\\transcript_bot\\templates\\notes.txt", first)
        self.assertNotEqual(first, second)

    def test_process_cleanup_is_started_without_a_console_window(self) -> None:
        watchdog = _load_watchdog_module()
        with patch.object(watchdog.subprocess, "run") as run:
            watchdog.kill_stale()
        self.assertEqual(run.call_args.kwargs["creationflags"], watchdog._CREATE_NO_WINDOW)

    def test_telegram_status_uses_configured_data_directory_without_credentials(self) -> None:
        watchdog = _load_watchdog_module()
        with TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            (root / ".env").write_text(
                "TELEGRAM_BOT_TOKEN=not-inspected\nDATA_DIR=runtime-data\n",
                encoding="utf-8",
            )
            self.assertTrue(watchdog.telegram_configured(root))
            watchdog.write_telegram_status("running", "Polling 已啟用。", root)
            status_path = root / "runtime-data" / "telegram_bot_status.json"
            status = json.loads(status_path.read_text(encoding="utf-8"))

        self.assertEqual(status["state"], "running")
        self.assertEqual(status["message"], "Polling 已啟用。")


if __name__ == "__main__":
    unittest.main()
