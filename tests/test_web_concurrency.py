from __future__ import annotations

import tempfile
import threading
import time
import unittest
from contextlib import ExitStack
from io import BytesIO
from pathlib import Path
from unittest.mock import patch

import transcript_bot.web as web
from transcript_bot.config import settings
from transcript_bot.database import init_database
from transcript_bot.transcription import TranscriptSegment

# Module-level coordination events so a prior test's lingering threads can always
# be released by a later test (self.* events would not reach them).
_BLOCKER = threading.Event()
_STARTED = threading.Event()


def _fake_transcribe(audio_path, progress_callback=None):
    _STARTED.set()
    _BLOCKER.wait()
    return [TranscriptSegment("Speaker 1", 0.0, 1.0, "暫停測試用內容")]


class ConcurrencyLimitTests(unittest.TestCase):
    def _drain_threads(self) -> None:
        """Release any lingering blocker and join leftover background threads."""
        _BLOCKER.set()
        for thread in list(web._active_jobs.values()):
            thread.join(timeout=5)
        web._active_jobs.clear()
        web._job_progress.clear()
        web._job_queue.clear()
        web._queued_payloads.clear()
        web._running_count = 0

    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.data_dir = Path(self.temp_dir.name)
        init_database(self.data_dir)
        # Pin a low limit so a second upload must be queued
        self._orig_max = settings.max_concurrent_jobs
        settings.max_concurrent_jobs = 1
        self._drain_threads()  # clear any threads left by a prior test in this process
        _BLOCKER.clear()
        _STARTED.clear()
        self.app = web.create_web_app(self.data_dir)
        self.client = self.app.test_client()

    def tearDown(self) -> None:
        settings.max_concurrent_jobs = self._orig_max
        self._drain_threads()
        try:
            self.temp_dir.cleanup()
        except PermissionError:
            pass  # Windows: a stale handle may linger; ignore on teardown

    def _patched(self) -> ExitStack:
        """Block the slow/real audio steps so the job reaches the (faked) transcription.

        web.py binds these via `from ... import`, so we must patch the names in the
        transcript_bot.web namespace that the job actually calls. ``normalize_audio``
        is replaced with a stub that actually writes a tiny normalized file, because
        the size gate added after normalization reads its size.
        """
        stack = ExitStack()

        def _fake_normalize(input_path, output_path):
            Path(output_path).write_bytes(b"tiny")

        stack.enter_context(patch("transcript_bot.web.normalize_audio", _fake_normalize))
        stack.enter_context(patch("transcript_bot.web.get_audio_duration", return_value=10.0))
        stack.enter_context(
            patch("transcript_bot.web.transcribe_audio_smart", _fake_transcribe)
        )
        return stack

    def _upload(self, name: str) -> None:
        self.client.post(
            "/upload",
            data={"audio_file": (BytesIO(b"fake-audio"), name)},
            content_type="multipart/form-data",
        )

    def test_second_upload_is_queued_when_limit_reached(self) -> None:
        with self._patched():
            self._upload("a.m4a")
            self._upload("b.m4a")
        # give the spawned thread a moment to enter the fake (and grab a slot)
        _STARTED.wait(timeout=2)
        time.sleep(0.1)

        # Exactly one job should be actively running, one queued.
        self.assertEqual(web._running_count, 1)
        self.assertEqual(len(web._active_jobs), 1)
        self.assertEqual(len(web._job_queue), 1)

        queued_id = web._job_queue[0]
        prog = web._job_progress.get(queued_id)
        self.assertIsNotNone(prog)
        self.assertTrue(prog.get("queued", False))

    def test_completed_job_frees_slot_for_queued(self) -> None:
        with self._patched():
            self._upload("a.m4a")
            self._upload("b.m4a")
        _STARTED.wait(timeout=2)
        time.sleep(0.1)
        self.assertEqual(len(web._job_queue), 1)

        # Release the running job; its completion must promote the queued one.
        _BLOCKER.set()
        for _ in range(40):  # up to ~4s
            if len(web._job_queue) == 0:
                break
            time.sleep(0.1)
        else:
            print("DIAG queue=", list(web._job_queue), "running=", web._running_count,
                  "active=", list(web._active_jobs),
                  "progress=", {k: v.get("step") for k, v in web._job_progress.items()})
            self.fail("queued job was not promoted after a slot freed")
        # The promoted job started and ran (blocker was already set, so it finishes fast).
        self.assertIn(web._running_count, (0, 1))

    def test_deleting_queued_job_does_not_leak(self) -> None:
        with self._patched():
            self._upload("a.m4a")
            self._upload("b.m4a")
        _STARTED.wait(timeout=2)
        time.sleep(0.1)
        self.assertEqual(len(web._job_queue), 1)

        queued_id = web._job_queue[0]
        # Deleting the still-queued meeting must drop it from the queue entirely.
        self.client.post(
            f"/meetings/{queued_id}/delete",
            data={"cancelled": "1"},
            follow_redirects=True,
        )
        self.assertNotIn(queued_id, web._job_queue)
        self.assertNotIn(queued_id, web._queued_payloads)
        self.assertEqual(web._running_count, 1)  # running job untouched


if __name__ == "__main__":
    unittest.main()
