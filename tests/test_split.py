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

    def _run_split(self, file_size, total_duration, max_size, overlap, probe_durations):
        """Run split_audio_file with mocked fs/subprocess.

        probe_durations: ffprobe durations returned in order (total first,
        then one per created part). Returns (parts, [(start, end)]).
        """
        durations = list(probe_durations)
        calls = []

        def fake_run(cmd, **kwargs):
            if cmd and cmd[0] == "ffprobe":
                return SimpleNamespace(
                    stdout=json.dumps({"format": {"duration": str(durations.pop(0)), "start_time": "0"}}),
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
                end = total_duration
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
            file_size=40 * 1024 * 1024, total_duration=100.0,
            max_size=10 * 1024 * 1024, overlap=5.0,
            probe_durations=[100.0, 25.0, 30.0, 30.0, 30.0],
        )
        self.assertEqual(len(parts), 4)
        self.assertEqual(bounds, [(0.0, 25.0), (20.0, 50.0), (45.0, 75.0), (70.0, 100.0)])
        self.assertEqual([p[3] for p in parts], [25.0, 30.0, 30.0, 30.0])
        for (s1, e1), (s2, e2) in zip(bounds, bounds[1:]):
            self.assertAlmostEqual(e1 - s2, 5.0)
            self.assertLess(s2, e1)
        self.assertEqual(bounds[0][0], 0.0)
        self.assertEqual(bounds[-1][1], 100.0)

    def test_overlap_clamped_when_too_large(self):
        # overlap >= part duration gets halved instead of producing inverted ranges
        parts, bounds = self._run_split(
            file_size=30 * 1024 * 1024, total_duration=30.0,
            max_size=10 * 1024 * 1024, overlap=60.0,
            probe_durations=[30.0, 10.0, 15.0, 15.0],
        )
        self.assertEqual(len(parts), 3)
        for start, end in bounds:
            self.assertLess(start, end)

    def test_first_part_has_no_negative_start(self):
        _parts, bounds = self._run_split(
            file_size=40 * 1024 * 1024, total_duration=100.0,
            max_size=10 * 1024 * 1024, overlap=5.0,
            probe_durations=[100.0, 25.0, 30.0, 30.0, 30.0],
        )
        self.assertGreaterEqual(bounds[0][0], 0.0)

    def test_split_raises_on_truncated_part(self):
        # second part probes at 5s while 30s expected -> must fail loudly
        with self.assertRaises(Exception):
            self._run_split(
                file_size=40 * 1024 * 1024, total_duration=100.0,
                max_size=10 * 1024 * 1024, overlap=5.0,
                probe_durations=[100.0, 25.0, 5.0, 30.0, 30.0],
            )


class ProbeTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        os.environ["YT_AUDIO_DB"] = os.path.join(self.temp_dir.name, "split.db")
        os.environ["YT_AUDIO_BOT_TOKEN"] = "test-token"
        import bot

        self.bot = bot

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_probe_audio_parsing(self):
        payload = json.dumps({"format": {"start_time": "12.5", "duration": "345.6"}})
        with mock.patch.object(
            self.bot.subprocess, "run",
            return_value=SimpleNamespace(stdout=payload, returncode=0),
        ):
            self.assertEqual(self.bot._probe_audio("/tmp/x.opus"), (12.5, 345.6))

    def test_verify_output_duration_ok(self):
        with mock.patch.object(self.bot, "_probe_audio", return_value=(0.0, 100.0)):
            self.assertEqual(self.bot._verify_output_duration("/tmp/x.opus", 100.0, "t"), 100.0)

    def test_verify_output_duration_mismatch_raises(self):
        with mock.patch.object(self.bot, "_probe_audio", return_value=(0.0, 100.0)):
            with self.assertRaises(Exception):
                self.bot._verify_output_duration("/tmp/x.opus", 1000.0, "t")

    def test_verify_output_duration_skipped_without_expected(self):
        with mock.patch.object(self.bot, "_probe_audio", return_value=(0.0, 50.0)):
            self.assertEqual(self.bot._verify_output_duration("/tmp/x.opus", 0, "t"), 50.0)

    def test_verify_output_duration_probe_failure_warns(self):
        with mock.patch.object(self.bot, "_probe_audio", side_effect=Exception("no ffprobe")):
            self.assertEqual(self.bot._verify_output_duration("/tmp/x.opus", 100.0, "t"), 0.0)


if __name__ == "__main__":
    unittest.main()
