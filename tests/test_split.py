import json
import os
import tempfile
import unittest
from types import SimpleNamespace
from unittest import mock


class SplitOverlapTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        os.environ["YT_AUDIO_DB"] = os.path.join(self.temp_dir.name, "split.db")
        os.environ["YT_AUDIO_BOT_TOKEN"] = "test-token"
        import bot

        self.bot = bot

    def tearDown(self):
        self.temp_dir.cleanup()

    def _run_split(self, file_size, duration, max_size, overlap):
        """Run split_audio_file with mocked fs/subprocess; return ffmpeg -ss/-to pairs."""
        calls = []

        def fake_run(cmd, **kwargs):
            if cmd and cmd[0] == "ffprobe":
                return SimpleNamespace(
                    stdout=json.dumps({"format": {"duration": str(duration)}}),
                    returncode=0,
                )
            calls.append(list(cmd))
            return SimpleNamespace(stdout="", returncode=0)

        with mock.patch.object(self.bot.os.path, "getsize", return_value=file_size), \
             mock.patch.object(self.bot.subprocess, "run", side_effect=fake_run), \
             mock.patch.object(self.bot.os, "remove", return_value=None):
            parts = self.bot.split_audio_file("/tmp/fake.opus", max_size, "book", overlap_sec=overlap)

        bounds = []
        for cmd in calls:
            self.assertIn("-ss", cmd)
            start = float(cmd[cmd.index("-ss") + 1])
            if "-to" in cmd:
                end = float(cmd[cmd.index("-to") + 1])
            else:
                end = duration
            bounds.append((start, end))
        return parts, bounds

    def test_no_split_below_threshold(self):
        with mock.patch.object(self.bot.os.path, "getsize", return_value=10 * 1024 * 1024):
            with mock.patch.object(
                self.bot.subprocess,
                "run",
                return_value=SimpleNamespace(stdout=json.dumps({"format": {"duration": "60.0"}}), returncode=0),
            ) as run_mock:
                parts = self.bot.split_audio_file("/tmp/fake.opus", 36 * 1024 * 1024, "book")
        self.assertEqual(len(parts), 1)
        self.assertEqual(parts[0][1:], (1, 1, 60.0))
        run_mock.assert_called_once()  # only ffprobe, no ffmpeg

    def test_overlap_boundaries(self):
        # 4 parts over 100s with 5s overlap: starts 0, 20, 45, 70; ends 25, 50, 75, 100
        parts, bounds = self._run_split(
            file_size=40 * 1024 * 1024, duration=100.0,
            max_size=10 * 1024 * 1024, overlap=5.0,
        )
        self.assertEqual(len(parts), 4)
        self.assertEqual(bounds, [(0.0, 25.0), (20.0, 50.0), (45.0, 75.0), (70.0, 100.0)])
        durations = [p[3] for p in parts]
        self.assertEqual(durations, [25.0, 30.0, 30.0, 30.0])
        # consecutive parts overlap by exactly 5s, full coverage, no gaps
        for (s1, e1), (s2, e2) in zip(bounds, bounds[1:]):
            self.assertAlmostEqual(e1 - s2, 5.0)
            self.assertLess(s2, e1)
        self.assertEqual(bounds[0][0], 0.0)
        self.assertEqual(bounds[-1][1], 100.0)

    def test_overlap_clamped_when_too_large(self):
        # overlap >= part duration gets halved instead of producing inverted ranges
        parts, bounds = self._run_split(
            file_size=30 * 1024 * 1024, duration=30.0,
            max_size=10 * 1024 * 1024, overlap=60.0,
        )
        self.assertEqual(len(parts), 3)
        for start, end in bounds:
            self.assertLess(start, end)

    def test_first_part_has_no_negative_start(self):
        _parts, bounds = self._run_split(
            file_size=40 * 1024 * 1024, duration=100.0,
            max_size=10 * 1024 * 1024, overlap=5.0,
        )
        self.assertGreaterEqual(bounds[0][0], 0.0)


if __name__ == "__main__":
    unittest.main()
