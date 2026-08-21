from __future__ import annotations

import sys
import tempfile
import time
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import Mock, patch


APP = Path(__file__).resolve().parents[1] / "app"
sys.path.insert(0, str(APP))

import convert as conversion  # noqa: E402
import state  # noqa: E402
import validator  # noqa: E402
import webui  # noqa: E402
from config import Config, ValidationCfg  # noqa: E402
from probe import VideoInfo  # noqa: E402


def _video_info(path: Path, *, codec: str = "h264") -> VideoInfo:
    return VideoInfo(
        path=path,
        codec=codec,
        pix_fmt="yuv420p",
        width=1920,
        height=1080,
        duration=1938.7,
        bit_depth=8,
        chroma="420",
        video_streams=1,
        audio_streams=1,
        subtitle_streams=0,
        attached_pic_streams=0,
        color_primaries="",
        color_trc="",
        color_space="",
        color_range="",
    )


class ProgressRenderingTests(unittest.TestCase):
    def tearDown(self) -> None:
        state.clear_current()
        state.set_pending([])

    def test_validation_progress_shows_reliable_metrics_only(self) -> None:
        """Bitrate/total_size are unreliable under -max_interleave_delta 0;
        the progress card must show reliable metrics (frame count, source
        size) instead, and _fmt_bytes must survive N/A input safely."""
        state.set_current(
            path="episode.mkv",
            stage="validating",
            started_at=time.time(),
            duration=1938.7,
            size=1234567890,
            progress={
                "out_time": "00:10:00.000000",
                "total_size": "N/A",
                "speed": "40x",
                "bitrate": "N/A",
                "frame": "17280",
            },
        )

        markup = webui._render_progress()

        self.assertIn("Frames encoded", markup)
        self.assertIn("17280", markup)
        self.assertIn("Source size", markup)
        self.assertNotIn("Bitrate", markup)
        self.assertNotIn("Output size so far", markup)
        self.assertEqual(webui._fmt_bytes("N/A"), "\u2014")
        self.assertEqual(webui._fmt_bytes("1024"), "1.0 KiB")


class FullDecodeSafetyTests(unittest.TestCase):
    def test_full_decode_rejects_truncated_video_timeline(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            output = Path(temp_dir) / "output.mkv"
            output.write_bytes(b"nonempty")
            original = _video_info(Path(temp_dir) / "source.mkv")
            encoded = replace(original, path=output, codec="hevc")

            with (
                patch.object(validator, "probe_video", return_value=encoded),
                patch.object(
                    validator,
                    "_run_full_decode",
                    return_value=(
                        0,
                        "",
                        {"out_time": "00:30:00.000000", "progress": "end"},
                    ),
                ),
            ):
                with self.assertRaisesRegex(
                    validator.ValidationError,
                    "decoded output is truncated",
                ):
                    validator.validate(original, output, ValidationCfg())

    def test_full_decode_rejects_unmeasurable_completion(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            output = Path(temp_dir) / "output.mkv"
            output.write_bytes(b"nonempty")
            original = _video_info(Path(temp_dir) / "source.mkv")
            encoded = replace(original, path=output, codec="hevc")

            with (
                patch.object(validator, "probe_video", return_value=encoded),
                patch.object(
                    validator,
                    "_run_full_decode",
                    return_value=(0, "", {"out_time": "N/A", "progress": "end"}),
                ),
            ):
                with self.assertRaisesRegex(
                    validator.ValidationError,
                    "completion timestamp",
                ):
                    validator.validate(original, output, ValidationCfg())

    def test_full_decode_uses_independent_software_decoder(self) -> None:
        commands: list[list[str]] = []

        def fake_decode(command, _progress_cb):
            commands.append(command)
            return 0, "", {
                "out_time": "00:32:18.700000",
                "progress": "end",
            }

        with tempfile.TemporaryDirectory() as temp_dir:
            output = Path(temp_dir) / "output.mkv"
            output.write_bytes(b"nonempty")
            original = _video_info(Path(temp_dir) / "source.mkv")
            encoded = replace(original, path=output, codec="hevc")

            with (
                patch.object(validator, "probe_video", return_value=encoded),
                patch.object(validator, "_run_full_decode", side_effect=fake_decode),
            ):
                validator.validate(original, output, ValidationCfg())

        self.assertEqual(len(commands), 1)
        self.assertNotIn("-hwaccel", commands[0])
        self.assertNotIn("qsv", commands[0])
        self.assertEqual(
            commands[0][commands[0].index("-map") + 1],
            "0:v:0",
        )


class ReplacementSafetyTests(unittest.TestCase):
    def tearDown(self) -> None:
        state.clear_current()

    def test_validation_failure_preserves_source(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            source = Path(temp_dir) / "episode.mkv"
            output = Path(temp_dir) / ".episode.converting.mkv"
            source.write_bytes(b"valid original")
            output.write_bytes(b"corrupt output")
            cfg = Config()
            cfg.runtime.stability_check_seconds = 0
            store = Mock()

            with (
                patch.object(conversion, "precheck_source"),
                patch.object(conversion, "transcode", return_value=output),
                patch.object(
                    conversion,
                    "validate",
                    side_effect=validator.ValidationError("corrupt bitstream"),
                ),
                patch.object(conversion, "atomic_replace") as replace_mock,
            ):
                conversion._encode_and_replace(
                    source,
                    _video_info(source),
                    cfg,
                    store,
                )

            self.assertEqual(source.read_bytes(), b"valid original")
            self.assertFalse(output.exists())
            replace_mock.assert_not_called()
            store.record.assert_called_once()
            self.assertEqual(store.record.call_args.args[1], "failed")


if __name__ == "__main__":
    unittest.main()