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
        self.assertIn("应用退出", progress_messages("interrupted", {"videos_done": 2}))

    def test_legacy_cloud_modes_collapse_to_backend(self) -> None:
        from bili.gpu_remote import needs_remote_models, resolve_compute

        self.assertEqual(resolve_compute("official_then_cloud", "local"), ("official_then_whisper", "cloud"))
        self.assertEqual(resolve_compute("whisper", "cloud"), ("whisper", "cloud"))
        self.assertTrue(needs_remote_models("official_then_whisper", "cloud", True))
        self.assertFalse(needs_remote_models("official", "cloud", False))

    def test_gpu_worker_url_requires_host(self) -> None:
        from bili.gpu_remote import gpu_worker_ready, normalize_worker_url

        self.assertEqual(normalize_worker_url("10.0.0.8:8766"), "http://10.0.0.8:8766")
        ok, message = gpu_worker_ready("")
        self.assertFalse(ok)
        self.assertIn("尚未填写", message)

    def test_parses_autodl_ssh_command(self) -> None:
        from bili.autodl import parse_ssh_command

        target = parse_ssh_command("ssh -p 43851 root@region-9.seetacloud.com")
        self.assertEqual(target.user, "root")
        self.assertEqual(target.host, "region-9.seetacloud.com")
        self.assertEqual(target.port, 43851)
        target2 = parse_ssh_command("root@connect.westc.gpuhub.com -p 12345")
        self.assertEqual(target2.host, "connect.westc.gpuhub.com")
        self.assertEqual(target2.port, 12345)
        with self.assertRaises(ValueError):
            parse_ssh_command("ssh -p 你的SSH端口 root@host")


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
        self.assertEqual(
            should_fetch_media("video", "delete_after_text", "official_then_cloud", False),
            "audio",
        )

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


class AcademicCorpusTests(unittest.TestCase):
    def test_parse_bvids_and_seeds(self) -> None:
        from bili.academic import parse_academic_seeds
        from bili.util import parse_bvids

        raw = "看 BV1xx411c7mD 和 https://www.bilibili.com/video/BV1GJ411x7h7\nhttps://space.bilibili.com/208259"
        self.assertEqual(parse_bvids(raw), ["BV1xx411c7mD", "BV1GJ411x7h7"])
        bvids, uids = parse_academic_seeds(raw)
        self.assertEqual(bvids, ["BV1xx411c7mD", "BV1GJ411x7h7"])
        self.assertIn("208259", uids)

    def test_gate_rejects_old_and_keyword_miss(self) -> None:
        import time

        from bili.academic import AcademicConfig, evaluate_gate

        config = AcademicConfig(
            job_id="t",
            max_depth=2,
            min_views=100,
            min_replies=0,
            min_engagement=0,
            category_deny="",
            tag_terms="",
            keyword="乡村振兴",
            time_range="1y",
        )
        now = int(time.time())
        view = {"title": "乡村振兴现场", "desc": "", "pubdate": now, "stat": {"view": 200}, "tname": "知识"}
        passed, reason, _ = evaluate_gate(view, config, 0)
        self.assertTrue(passed)
        self.assertEqual(reason, "pass")
        _, reason, _ = evaluate_gate({**view, "title": "无关标题"}, config, 0)
        self.assertEqual(reason, "keyword")
        _, reason, _ = evaluate_gate({**view, "pubdate": 1}, config, 0)
        self.assertEqual(reason, "too_old")
        _, reason, _ = evaluate_gate({**view, "stat": {"view": 10}}, config, 0)
        self.assertEqual(reason, "min_views")
        _, reason, _ = evaluate_gate(view, config, 9)
        self.assertEqual(reason, "max_depth")

    def test_gate_category_engagement_and_tags(self) -> None:
        import time

        from bili.academic import AcademicConfig, evaluate_gate

        now = int(time.time())
        config = AcademicConfig(job_id="t", time_range="all")
        base = {
            "title": "大厂裁员之后的就业",
            "desc": "",
            "pubdate": now,
            "tname": "知识",
            "tid": 36,
            "stat": {"view": 20000, "reply": 200},
        }
        passed, reason, _ = evaluate_gate(base, config, 0, tags=["就业"])
        self.assertTrue(passed)
        self.assertEqual(reason, "pass")
        _, reason, _ = evaluate_gate({**base, "tname": "游戏"}, config, 0, tags=["就业"])
        self.assertEqual(reason, "category")
        _, reason, _ = evaluate_gate({**base, "stat": {"view": 20000, "reply": 1}}, config, 0, tags=["就业"])
        self.assertEqual(reason, "engagement")
        _, reason, _ = evaluate_gate({**base, "title": "今日天气"}, config, 0, tags=["风景"])
        self.assertEqual(reason, "tags")

    def test_academic_defaults_include_video_transcripts(self) -> None:
        from bili.academic import AcademicConfig

        config = AcademicConfig(job_id="t")
        self.assertEqual(config.transcribe_mode, "official_then_whisper")
        self.assertEqual(config.media_mode, "audio")
        self.assertTrue(config.crawl_comments)

    def test_corpus_topology_does_not_wipe_stats(self) -> None:
        from bili.corpus import Corpus

        with tempfile.TemporaryDirectory() as tmp:
            db = Corpus(Path(tmp) / "corpus.db")
            db.upsert_author({"mid": "1", "name": "测试", "sex": "男", "school": "某大学"})
            db.upsert_video(
                {
                    "bvid": "BV1xx411c7mD",
                    "mid": "1",
                    "title": "hello",
                    "view_count": 12345,
                    "discovery": "space",
                    "run_id": "r1",
                }
            )
            db.set_video_topology("BV1xx411c7mD", discovery="snowball", depth=1, pass_filter=True, run_id="r1")
            row = db.query("SELECT view_count, discovery, depth, pass_filter FROM videos WHERE bvid='BV1xx411c7mD'")[0]
            self.assertEqual(row["view_count"], 12345)
            self.assertEqual(row["discovery"], "snowball")
            self.assertEqual(row["depth"], 1)
            self.assertEqual(row["pass_filter"], 1)
            views = db.query("SELECT name FROM sqlite_master WHERE type='view'")
            names = {row["name"] for row in views}
            self.assertIn("v_transcript_corpus", names)
            self.assertIn("v_comment_corpus", names)
            created = db.query("SELECT captured_at FROM videos LIMIT 1")
            self.assertTrue(created)
            with self.assertRaises(ValueError):
                db.query("DELETE FROM videos")


if __name__ == "__main__":
    unittest.main()

