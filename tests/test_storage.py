from __future__ import annotations

import re
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from transcript_bot.storage import create_job_paths


class StorageTests(unittest.TestCase):
    def test_export_names_include_date_and_meeting_id(self) -> None:
        with TemporaryDirectory() as temp_dir:
            paths = create_job_paths(Path(temp_dir), ".ogg")

        expected_prefix = rf"meeting_\d{{8}}_{paths.job_id}"
        self.assertRegex(paths.transcript_txt.name, rf"^{expected_prefix}\.txt$")
        self.assertRegex(paths.transcript_docx.name, rf"^{expected_prefix}\.docx$")


if __name__ == "__main__":
    unittest.main()
