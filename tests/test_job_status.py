from __future__ import annotations

import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from transcript_bot.job_status import (
    cancel_requested,
    get_job_status,
    list_active_job_statuses,
    request_cancel,
    write_job_status,
)


class SharedJobStatusTests(unittest.TestCase):
    def test_telegram_progress_is_visible_and_can_be_cancelled(self) -> None:
        with TemporaryDirectory() as temp_dir:
            data_dir = Path(temp_dir)
            write_job_status(
                data_dir, "telegram-job", source="telegram", step="transcribing", pct=45,
                label="transcribing (語音辨識)",
            )

            self.assertEqual(list_active_job_statuses(data_dir)["telegram-job"]["pct"], 45)
            request_cancel(data_dir, "telegram-job")

            self.assertTrue(cancel_requested(data_dir, "telegram-job"))
            self.assertEqual(get_job_status(data_dir, "telegram-job")["step"], "cancelling")

    def test_terminal_status_is_not_listed_as_active(self) -> None:
        with TemporaryDirectory() as temp_dir:
            data_dir = Path(temp_dir)
            write_job_status(data_dir, "finished", source="telegram", step="done", pct=100, label="done")

            self.assertNotIn("finished", list_active_job_statuses(data_dir))

    def test_status_without_a_live_owner_is_not_active(self) -> None:
        with TemporaryDirectory() as temp_dir:
            data_dir = Path(temp_dir)
            (data_dir / "job-status").mkdir()
            (data_dir / "job-status" / "stale.json").write_text(
                '{"source":"telegram","step":"transcribing","pct":20}', encoding="utf-8"
            )

            self.assertNotIn("stale", list_active_job_statuses(data_dir))
