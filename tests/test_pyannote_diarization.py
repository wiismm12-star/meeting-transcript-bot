from __future__ import annotations

import unittest

from transcript_bot.pyannote_diarization import SpeakerTurn, apply_pyannote_speakers
from transcript_bot.transcription import TranscriptSegment


class PyannoteDiarizationTests(unittest.TestCase):
    def test_assigns_each_timed_transcript_segment_to_the_largest_overlap(self) -> None:
        segments = [
            TranscriptSegment("UNASSIGNED", 0.0, 1.0, "第一段"),
            TranscriptSegment("UNASSIGNED", 1.0, 2.0, "第二段"),
        ]
        turns = [
            SpeakerTurn(0.0, 0.8, "SPEAKER_00"),
            SpeakerTurn(0.8, 2.0, "SPEAKER_01"),
        ]

        assigned = apply_pyannote_speakers(segments, turns)

        self.assertEqual([segment.speaker for segment in assigned], ["SPEAKER_00", "SPEAKER_01"])


if __name__ == "__main__":
    unittest.main()
