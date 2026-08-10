from __future__ import annotations

import importlib.util
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
            (source_dir / "index.html").write_text("first", encoding="utf-8")
            (source_dir / "notes.txt").write_text("ignored", encoding="utf-8")

            first = watchdog.source_snapshot(root)
            (source_dir / "index.html").write_text("updated page", encoding="utf-8")
            second = watchdog.source_snapshot(root)

        self.assertIn("src\\transcript_bot\\templates\\index.html", first)
        self.assertNotIn("src\\transcript_bot\\templates\\notes.txt", first)
        self.assertNotEqual(first, second)

    def test_process_cleanup_is_started_without_a_console_window(self) -> None:
        watchdog = _load_watchdog_module()
        with patch.object(watchdog.subprocess, "run") as run:
            watchdog.kill_stale()
        self.assertEqual(run.call_args.kwargs["creationflags"], watchdog._CREATE_NO_WINDOW)


if __name__ == "__main__":
    unittest.main()
