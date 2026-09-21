from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from bili.media import fetch_media, select_stream_candidates, select_streams
from bili.pipeline import PipelineState
from bili.transcribe import _normalise_segments, _srt_time
from bili.util import replace_jsonl, write_json


class PipelineTests(unittest.TestCase):
    def test_checkpoint_requires_matching_signature_and_outputs(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp)
            output = folder / "result.json"
            state = PipelineState.load(folder)
            state.mark("ocr", "v2:min=.55", "done")
            self.assertFalse(state.completed("ocr", "v2:min=.55", [output]))
            write_json(output, {"ok": True})
            reloaded = PipelineState.load(folder)
            self.assertTrue(reloaded.completed("ocr", "v2:min=.55", [output]))
            self.assertFalse(reloaded.completed("ocr", "v2:min=.7", [output]))

    def test_replace_jsonl_publishes_complete_dataset(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "rows.jsonl"
            count = replace_jsonl(path, [{"id": 1}, {"id": 2}])
            self.assertEqual(count, 2)
            self.assertEqual(
                [json.loads(line)["id"] for line in path.read_text().splitlines()],
                [1, 2],
            )


class RuntimeTests(unittest.TestCase):
    def test_ffmpeg_candidates_include_windows_exe(self) -> None:
        from bili.runtime import ffmpeg_candidates

        names = [str(path) for path in ffmpeg_candidates("ffmpeg")]
        self.assertTrue(any(item.endswith("ffmpeg") or item.endswith("ffmpeg.exe") for item in names))
        self.assertTrue(any("tools" in item and "bin" in item for item in names))

    def test_cpu_request_does_not_require_cuda(self) -> None:
        from bili.runtime import resolve_whisper_backend

        device, compute, note = resolve_whisper_backend("cpu", "auto")
        self.assertEqual(device, "cpu")
        self.assertEqual(compute, "int8")
        self.assertTrue(note)

    def test_rclone_discovery_does_not_crash(self) -> None:
        from bili.storage import rclone_remote_names, which_rclone

        binary = which_rclone()
        names = rclone_remote_names()
        if binary:
            self.assertTrue(Path(binary).exists())
            self.assertIsInstance(names, list)


class NotifyTests(unittest.TestCase):
    def test_extracts_key_from_bark_url(self) -> None:
        from bili.notify import normalize_bark_key, progress_messages

        self.assertEqual(
            normalize_bark_key("https://api.day.app/FakeKey1234567890/b站爬虫/hello?sound=bell"),
            "FakeKey1234567890",
        )
        self.assertEqual(normalize_bark_key("FakeKey1234567890"), "FakeKey1234567890")
        self.assertIn("视频完成 3 条", progress_messages("video", {"videos_done": 3, "current": "天选OMG / 标题"}))


class StorageTests(unittest.TestCase):
    def test_should_not_download_video_if_it_will_be_deleted(self) -> None:
        from bili.storage import should_fetch_media

        self.assertEqual(
            should_fetch_media("video", "delete_after_text", "official_then_whisper", False),
            "audio",
        )
        self.assertEqual(
            should_fetch_media("video", "delete_after_text", "official_then_whisper", True),
            "link",
        )
        self.assertEqual(should_fetch_media("video", "keep", "whisper", False), "video")
        self.assertEqual(should_fetch_media("link", "delete_after_text", "official", False), "link")

    def test_delete_after_text_removes_media_keeps_transcript(self) -> None:
        from bili.storage import reclaim_folder

        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp)
            media = folder / "media"
            media.mkdir()
            video = media / "BV1.mp4"
            audio = media / "BV1.m4a"
            video.write_bytes(b"0" * 2048)
            audio.write_bytes(b"1" * 1024)
            (folder / "transcript.md").write_text("- hello\n", encoding="utf-8")
            result = reclaim_folder(
                folder,
                policy="delete_after_text",
                page_url="https://www.bilibili.com/video/BV1",
                transcript_ready=True,
                ocr_ready=True,
                include_images=False,
                include_media=True,
            )
            self.assertTrue(result["released"])
            self.assertGreater(result["bytes_freed"], 0)
            self.assertFalse(video.exists())
            self.assertFalse(audio.exists())
            self.assertTrue((folder / "transcript.md").exists())
            state = json.loads((folder / "storage.json").read_text(encoding="utf-8"))
            self.assertEqual(state["page_url"], "https://www.bilibili.com/video/BV1")

    def test_does_not_delete_audio_before_transcript(self) -> None:
        from bili.storage import reclaim_folder

        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp)
            media = folder / "media"
            media.mkdir()
            audio = media / "BV1.m4a"
            audio.write_bytes(b"1" * 512)
            result = reclaim_folder(
                folder,
                policy="delete_after_text",
                transcript_ready=False,
                include_media=True,
                include_images=False,
            )
            self.assertTrue(audio.exists())
            self.assertFalse(result["released"])


class MediaTests(unittest.TestCase):
    def test_selects_highest_video_not_above_requested_quality(self) -> None:
        play = {
            "dash": {
                "video": [
                    {"id": 32, "baseUrl": "480"},
                    {"id": 64, "baseUrl": "720", "backupUrl": ["720-backup"]},
                    {"id": 80, "baseUrl": "1080"},
                ],
                "audio": [
                    {"bandwidth": 64, "baseUrl": "low"},
                    {"bandwidth": 192, "baseUrl": "high"},
                ],
            }
        }
        video, audio = select_streams(play, 64)
        self.assertEqual(video, "720")
        self.assertEqual(audio, "high")
        video_candidates, _ = select_stream_candidates(play, 64)
        self.assertEqual(video_candidates, ["720", "720-backup"])


class MediaWorkflowTests(unittest.IsolatedAsyncioTestCase):
    async def test_link_mode_never_requests_expiring_playurl(self) -> None:
        class Client:
            async def get_playurl(self, *_args, **_kwargs):
                raise AssertionError("link mode must not request playurl")

        with tempfile.TemporaryDirectory() as tmp:
            result = await fetch_media(
                Client(),
                bvid="BV1234567890",
                cid=1,
                folder=Path(tmp),
                mode="link",
                qn=64,
                ffmpeg_path="ffmpeg",
                on_log=lambda *_args: None,
            )
            self.assertEqual(result["status"], "linked")
            manifest = json.loads((Path(tmp) / "cloud_job.json").read_text())
            self.assertNotIn("audio_url", manifest)
            self.assertEqual(manifest["page_url"], "https://www.bilibili.com/video/BV1234567890")


class TranscriptTests(unittest.TestCase):
    def test_normalises_official_subtitles(self) -> None:
        segments = _normalise_segments(
            [{"from": 1.2, "to": 2.5, "content": "你好"}, {"from": 3, "content": ""}]
        )
        self.assertEqual(segments, [{"id": 1, "start": 1.2, "end": 2.5, "text": "你好"}])
        self.assertEqual(_srt_time(65.123), "00:01:05,123")


if __name__ == "__main__":
    unittest.main()

