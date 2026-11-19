import asyncio
import shutil
import subprocess
import tempfile
import threading
import time
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch

from app import main
from mutagen.easyid3 import EasyID3
from mutagen.mp4 import MP4


class LivePipelineTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.root = Path(self.temp_dir.name)

    def test_yt_dlp_does_not_write_tags_before_lookup(self):
        job = main.Job("options", "url", "mp3", "session", playlist_items=[1, 2])
        options = main.ydl_options(job, self.root)
        self.assertEqual([item["key"] for item in options["postprocessors"]], ["FFmpegExtractAudio"])
        self.assertNotIn("writethumbnail", options)

    def test_real_yt_dlp_after_move_hook_registers_converted_file(self):
        work_dir = self.root / "work"
        work_dir.mkdir()
        path = work_dir / "Song.mp3"
        path.write_bytes(b"dummy")
        job = main.Job("hook", "url", "mp3", "session")
        with main.YoutubeDL({"quiet": True, "outtmpl": str(work_dir / "%(title)s.%(ext)s")}) as ydl:
            ydl.add_post_processor(main.ReadyTrackPostProcessor(ydl, job, work_dir), when="after_move")
            ydl.post_process(str(path), {"id": "video", "title": "Song", "artist": "Artist", "ext": "mp3"})
        self.assertEqual(len(job.tracks), 1)
        self.assertEqual(job.tracks[0].source_title, "Song")

    @unittest.skipUnless(shutil.which("ffmpeg"), "ffmpeg is required")
    def test_yt_dlp_hook_waits_for_mp3_conversion(self):
        work_dir = self.root / "convert"
        work_dir.mkdir()
        source = work_dir / "Song.m4a"
        subprocess.run([
            "ffmpeg", "-loglevel", "error", "-f", "lavfi", "-i", "anullsrc=r=44100:cl=mono",
            "-t", "0.2", "-y", str(source),
        ], check=True, capture_output=True)
        job = main.Job("convert", "url", "mp3", "session")
        with main.YoutubeDL(main.ydl_options(job, work_dir)) as ydl:
            ydl.add_post_processor(main.ReadyTrackPostProcessor(ydl, job, work_dir), when="after_move")
            ydl.post_process(str(source), {"id": "local", "title": "Song", "artist": "Artist", "ext": "m4a", "acodec": "mp4a.40.2", "vcodec": "none"})
        self.assertEqual(len(job.tracks), 1)
        self.assertEqual(job.tracks[0].path.suffix, ".mp3")
        self.assertTrue(job.tracks[0].path.exists())

    def test_first_track_is_tagged_while_next_download_waits(self):
        first_tagged = threading.Event()
        first_was_visible_during_download = []
        job = main.Job("live", "url", "mp3", "session", playlist_items=[1, 2])
        test_case = self

        class FakeYoutubeDL:
            def __init__(self, options):
                self.params = options
                self.processor = None

            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

            def add_post_processor(self, processor, when):
                assert when == "after_move"
                self.processor = processor

            def extract_info(self, _url, download):
                assert download
                work_dir = Path(self.params["outtmpl"]).parent
                entries = []
                for number in (1, 2):
                    path = work_dir / f"Song {number} __yt2mpx_{number:05d}.mp3"
                    path.write_bytes(b"dummy")
                    info = {"filepath": str(path), "playlist_index": number, "title": f"Song {number}", "artist": "YouTube Artist", "playlist_title": "Mix"}
                    self.processor.run.__wrapped__(self.processor, info)
                    entries.append(info)
                    if number == 1:
                        first_was_visible_during_download.append(len(job.tracks) == 1 and not job.download_finished)
                        test_case.assertEqual(job.lookup_deadline, float("inf"))
                        assert first_tagged.wait(3), "first track was not tagged before the second download"
                return {"title": "Mix", "entries": entries}

        def tag(path, _metadata, _cover):
            if "Song 1" in path.name:
                first_tagged.set()

        with patch.object(main, "JOBS_DIR", self.root), patch.object(main, "YoutubeDL", FakeYoutubeDL), patch.object(main, "ACOUSTID_API_KEY", "key"), patch.object(main, "fpcalc", return_value=(100, "fingerprint")), patch.object(main, "acoustid_lookup", return_value=(0.99, "recording")), patch.object(main, "musicbrainz_metadata", return_value=main.Metadata(title="Matched", artist="MusicBrainz Artist")), patch.object(main, "cover_for_track", return_value=None), patch.object(main, "apply_mp3_metadata", side_effect=tag):
            main.run_job(job)
            end = time.monotonic() + 3
            while job.status != "review" and time.monotonic() < end:
                time.sleep(0.01)
        job.tag_stop.set()
        self.assertEqual(job.status, "review", job.error)
        self.assertEqual(first_was_visible_during_download, [True])
        self.assertEqual(len(job.tracks), 2)
        self.assertTrue(all(track.tag_status == "written" for track in job.tracks))
        self.assertEqual(job.tracks[0].metadata.title, "Matched")
        self.assertLess(job.lookup_deadline, float("inf"))

    def test_503_stays_untagged_until_later_pass(self):
        tracks = [main.Track(str(i), Path(f"{i}.mp3"), i, f"Song {i}", "", "", main.Metadata(title=f"Song {i}")) for i in (1, 2)]
        job = main.Job("retry", "url", "mp3", "session", status="running", tracks=tracks, download_finished=True)
        job.lookup_deadline = time.monotonic() + 60
        calls = []

        def lookup(recording_id, _fallback, _job):
            calls.append((recording_id, tracks[0].tag_status))
            if recording_id == "1" and len([call for call in calls if call[0] == "1"]) == 1:
                raise main.MetadataHTTPError(503, "busy")
            return main.Metadata(title=f"Match {recording_id}")

        with patch.object(main, "ACOUSTID_API_KEY", "key"), patch.object(main, "fpcalc", return_value=(100, "fingerprint")), patch.object(main, "acoustid_lookup", side_effect=lambda _d, _f, fallback: (0.99, fallback.title[-1])), patch.object(main, "musicbrainz_metadata", side_effect=lookup):
            main.lookup_job_metadata(job)
        self.assertEqual([call[0] for call in calls], ["1", "2", "1"])
        self.assertEqual(calls[1][1], "waiting")
        self.assertEqual(tracks[0].tag_status, "queued")

    def test_definite_no_match_queues_youtube_tags(self):
        track = main.Track("one", Path("one.mp3"), 1, "YouTube title", "Uploader", "", main.Metadata(title="YouTube title", artist="Uploader"))
        job = main.Job("no-match", "url", "mp3", "session", status="running", tracks=[track], download_finished=True)
        with patch.object(main, "ACOUSTID_API_KEY", "key"), patch.object(main, "fpcalc", return_value=(100, "fingerprint")), patch.object(main, "acoustid_lookup", side_effect=RuntimeError("AcoustID found no matching MusicBrainz recording.")):
            main.lookup_job_metadata(job)
        self.assertEqual(track.lookup_status, "no_match")
        self.assertEqual(track.metadata.title, "YouTube title")
        self.assertEqual(track.tag_status, "queued")

    def test_missing_musicbrainz_cover_uses_youtube_cover(self):
        candidate = "https://coverartarchive.org/release/missing/front-500"
        fallback = "https://i.ytimg.com/vi/example/default.jpg"
        fallback_path = self.root / "fallback.jpg"
        fallback_path.write_bytes(b"image")
        track = main.Track("one", self.root / "one.mp3", 1, "Song", "Uploader", fallback, main.Metadata(title="Matched", cover_url=candidate))
        job = main.Job("cover", "url", "mp3", "session", tracks=[track])
        applied = []

        def download(url, _target):
            return None if url == candidate else fallback_path

        with patch.object(main, "JOBS_DIR", self.root), patch.object(main, "download_cover", side_effect=download), patch.object(main, "apply_mp3_metadata", side_effect=lambda _path, metadata, cover: applied.append((metadata.cover_url, cover))):
            main.write_track_tags(job, track)

        self.assertEqual(track.tag_status, "written")
        self.assertEqual(track.metadata.cover_url, fallback)
        self.assertEqual(applied, [(fallback, fallback_path)])

    def test_musicbrainz_cover_is_checked_even_without_embedding(self):
        candidate = "https://coverartarchive.org/release/present/front-500"
        cover_path = self.root / "cover.jpg"
        cover_path.write_bytes(b"image")
        track = main.Track("one", self.root / "one.mp3", 1, "Song", "Uploader", "", main.Metadata(title="Matched", cover_url=candidate))
        job = main.Job("cover", "url", "mp3", "session", tracks=[track], embed_cover=False)
        applied = []

        with patch.object(main, "JOBS_DIR", self.root), patch.object(main, "download_cover", return_value=cover_path) as download, patch.object(main, "apply_mp3_metadata", side_effect=lambda _path, metadata, cover: applied.append((metadata.cover_url, cover))):
            main.write_track_tags(job, track)

        download.assert_called_once()
        self.assertEqual(track.metadata.cover_url, candidate)
        self.assertEqual(applied, [(candidate, None)])

    def test_manual_edit_only_requeues_that_track(self):
        tracks = [main.Track(str(i), Path(f"{i}.mp3"), i, f"Song {i}", "", "", main.Metadata(title=f"Song {i}"), tag_status="written", written_revision=0) for i in (1, 2)]
        job = main.Job("edit", "url", "mp3", "session", status="review", tracks=tracks)
        main.jobs[job.id] = job
        self.addCleanup(main.jobs.pop, job.id, None)
        result = asyncio.run(main.update_track(job.id, "1", main.TrackUpdateRequest(metadata=main.Metadata(title="Corrected"))))
        self.assertEqual(result["track"]["tag_status"], "queued")
        self.assertEqual(job.tag_queue.get_nowait(), "1")
        self.assertEqual(tracks[1].tag_status, "written")

    def test_manual_edit_rewrites_only_changed_file(self):
        paths = []
        tracks = []
        for i in (1, 2):
            path = self.root / f"{i}.mp3"
            path.write_bytes(b"dummy")
            tracks.append(main.Track(str(i), path, i, f"Song {i}", "", "", main.Metadata(title=f"Song {i}"), tag_status="written", written_revision=0))
        job = main.Job("rewrite", "url", "mp3", "session", status="review", tracks=tracks)
        main.jobs[job.id] = job
        self.addCleanup(main.jobs.pop, job.id, None)
        worker = threading.Thread(target=main.tag_job_metadata, args=(job,), daemon=True)
        with patch.object(main, "JOBS_DIR", self.root), patch.object(main, "cover_for_track", return_value=None), patch.object(main, "apply_mp3_metadata", side_effect=lambda path, *_: paths.append(path)):
            worker.start()
            asyncio.run(main.update_track(job.id, "1", main.TrackUpdateRequest(metadata=main.Metadata(title="Corrected"))))
            job.tag_queue.join()
            job.tag_stop.set()
            worker.join(timeout=2)
        self.assertEqual(paths, [tracks[0].path])
        self.assertEqual(tracks[0].written_revision, 1)
        self.assertEqual(tracks[1].written_revision, 0)

    def test_finalize_packages_previously_tagged_files_without_rewriting(self):
        work_dir = self.root / "packed" / "work"
        work_dir.mkdir(parents=True)
        tracks = []
        for number in (1, 2):
            path = work_dir / f"Song {number} __yt2mpx_{number:05d}.mp3"
            path.write_bytes(f"audio {number}".encode())
            tracks.append(main.Track(str(number), path, number, f"Song {number}", "", "", main.Metadata(title=f"Song {number}"), tag_status="written", written_revision=0))
        job = main.Job("packed", "url", "mp3", "session", playlist_items=[1, 2], status="finalizing", tracks=tracks, collection_title="Mix")
        with patch.object(main, "JOBS_DIR", self.root), patch.object(main, "apply_mp3_metadata") as tag:
            main.finalize_job(job)
        self.assertEqual(job.status, "done", job.error)
        tag.assert_not_called()
        with zipfile.ZipFile(job.output_path) as archive:
            self.assertEqual(len(archive.namelist()), 2)

    @unittest.skipUnless(shutil.which("ffmpeg"), "ffmpeg is required")
    def test_retagging_removes_fields_cleared_by_user(self):
        for media_format in ("mp3", "mp4"):
            with self.subTest(media_format=media_format):
                path = self.root / f"sample.{media_format}"
                subprocess.run([
                    "ffmpeg", "-loglevel", "error", "-f", "lavfi", "-i", "anullsrc=r=44100:cl=mono",
                    "-t", "0.2", "-y", str(path),
                ], check=True, capture_output=True)
                apply = main.apply_mp3_metadata if media_format == "mp3" else main.apply_mp4_metadata
                apply(path, main.Metadata(title="Old", artist="Artist", album="Album"), None)
                apply(path, main.Metadata(title="New", artist="", album=""), None)
                if media_format == "mp3":
                    tags = EasyID3(path)
                    self.assertEqual(tags["title"], ["New"])
                    self.assertNotIn("artist", tags)
                    self.assertNotIn("album", tags)
                else:
                    tags = MP4(path)
                    self.assertEqual(tags["\xa9nam"], ["New"])
                    self.assertNotIn("\xa9ART", tags)
                    self.assertNotIn("\xa9alb", tags)
