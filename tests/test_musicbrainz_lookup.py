import asyncio
import io
import time
import unittest
import urllib.error
from pathlib import Path
from unittest.mock import patch

from app import main


def track(number):
    return main.Track(str(number), Path(f"{number}.mp3"), number, f"Track {number}", "Artist", "", main.Metadata(title=f"Track {number}"))


def job_with_tracks(count=2):
    job = main.Job("lookup-test", "url", "mp3", "session", status="running", tracks=[track(i) for i in range(count)])
    job.lookup_deadline = time.monotonic() + 600
    job.download_finished = True
    return job


class MusicBrainzLookupTests(unittest.TestCase):
    def setUp(self):
        main.musicbrainz_cache.clear()
        main.last_musicbrainz_request_at = 0
        main.musicbrainz_consecutive_503 = 0
        main.musicbrainz_cooldown_until = 0

    def test_text_error_preserves_status_and_retry_after(self):
        error = urllib.error.HTTPError("url", 503, "busy", {"Retry-After": "7"}, io.BytesIO(b'{"error":"Server busy"}'))
        parsed = main.json_http_error(error)
        error.close()
        self.assertEqual(parsed.status, 503)
        self.assertEqual(parsed.retry_after, 7)
        self.assertIn("Server busy", str(parsed))

    def test_playlist_first_pass_continues_after_503(self):
        job = job_with_tracks(3)
        calls = []

        def lookup(recording_id, fallback, _job):
            calls.append(recording_id)
            if recording_id == "rec-0" and calls.count("rec-0") == 1:
                raise main.MetadataHTTPError(503, "busy")
            return main.Metadata(title=f"Found {recording_id}")

        with patch.object(main, "ACOUSTID_API_KEY", "key"), patch.object(main, "fpcalc", return_value=(100, "fingerprint")), patch.object(main, "acoustid_lookup", side_effect=lambda _d, _f, fallback: (0.99, f"rec-{fallback.title[-1]}")), patch.object(main, "musicbrainz_metadata", side_effect=lookup):
            main.lookup_job_metadata(job)
        self.assertEqual(calls, ["rec-0", "rec-1", "rec-2", "rec-0"])
        self.assertTrue(all(item.lookup_status == "matched" for item in job.tracks))
        self.assertEqual(job.tracks[0].metadata.title, "Found rec-0")
        self.assertTrue(all(item.tag_status == "queued" for item in job.tracks))

    def test_global_rate_cache_and_overload_pause(self):
        calls = []
        with patch.object(main, "MUSICBRAINZ_INTERVAL_SECONDS", 0.0), patch.object(main, "fetch_json", side_effect=lambda url, **_kwargs: calls.append(url) or {"title": "Found"}):
            main.musicbrainz_json("recording/same")
            main.musicbrainz_json("recording/same")
        self.assertEqual(len(calls), 1)

        main.musicbrainz_cache.clear()
        main.last_musicbrainz_request_at = time.monotonic()
        with patch.object(main, "fetch_json", return_value={"title": "Found"}), patch.object(main, "wait_for_lookup", return_value=True) as wait:
            main.musicbrainz_json("recording/another")
        self.assertGreater(wait.call_args.args[1], 1.0)

        main.musicbrainz_cache.clear()
        with patch.object(main, "MUSICBRAINZ_INTERVAL_SECONDS", 0.0), patch.object(main, "fetch_json", side_effect=main.MetadataHTTPError(503, "busy")), patch.object(main, "wait_for_lookup", return_value=True) as wait:
            for i in range(3):
                with self.assertRaises(main.MetadataHTTPError):
                    main.musicbrainz_json(f"recording/{i}")
        self.assertGreaterEqual(main.musicbrainz_cooldown_until - time.monotonic(), 29)
        self.assertEqual(wait.call_count, 3)

    def test_repeated_503_and_duplicate_ids(self):
        job = job_with_tracks(3)
        calls = []

        def lookup(recording_id, fallback, _job):
            calls.append(recording_id)
            if recording_id == "same" and calls.count("same") <= 2:
                raise main.MetadataHTTPError(503, "busy")
            return main.Metadata(title="Found")

        with patch.object(main, "ACOUSTID_API_KEY", "key"), patch.object(main, "fpcalc", return_value=(100, "fingerprint")), patch.object(main, "acoustid_lookup", side_effect=[(0.99, "same"), (0.99, "other"), (0.99, "same")]), patch.object(main, "musicbrainz_metadata", side_effect=lookup):
            main.lookup_job_metadata(job)
        self.assertEqual(calls[:3], ["same", "other", "same"])
        self.assertTrue(all(item.lookup_status == "matched" for item in job.tracks))

    def test_cancel_while_response_is_in_flight(self):
        job = job_with_tracks(1)

        def lookup(_recording_id, _fallback, _job):
            job.lookup_cancel.set()
            return main.Metadata(title="Late match")

        with patch.object(main, "ACOUSTID_API_KEY", "key"), patch.object(main, "fpcalc", return_value=(100, "fingerprint")), patch.object(main, "acoustid_lookup", return_value=(0.99, "same")), patch.object(main, "musicbrainz_metadata", side_effect=lookup):
            main.lookup_job_metadata(job)
        self.assertEqual(job.tracks[0].metadata.title, "Track 0")
        self.assertEqual(job.tracks[0].tag_status, "waiting")

    def test_deadline_writes_fallback_for_unresolved_tracks(self):
        job = job_with_tracks()
        job.lookup_deadline = time.monotonic() - 1
        main.lookup_job_metadata(job)
        self.assertTrue(job.lookup_finished)
        self.assertTrue(all(item.lookup_status == "unavailable" for item in job.tracks))
        self.assertTrue(all(item.tag_status == "queued" for item in job.tracks))

    def test_late_musicbrainz_response_does_not_replace_fallback(self):
        job = job_with_tracks(1)

        def late_result(_recording_id, _fallback, _job):
            job.lookup_deadline = time.monotonic() - 1
            return main.Metadata(title="Too late")

        with patch.object(main, "ACOUSTID_API_KEY", "key"), patch.object(main, "fpcalc", return_value=(100, "fingerprint")), patch.object(main, "acoustid_lookup", return_value=(0.99, "recording")), patch.object(main, "musicbrainz_metadata", side_effect=late_result):
            main.lookup_job_metadata(job)
        self.assertEqual(job.tracks[0].metadata.title, "Track 0")
        self.assertEqual(job.tracks[0].lookup_status, "unavailable")
        self.assertEqual(job.tracks[0].tag_status, "queued")

    def test_finalize_cancels_late_lookup(self):
        job = job_with_tracks(1)
        job.status = "review"
        main.jobs[job.id] = job
        self.addCleanup(main.jobs.pop, job.id, None)
        with patch.object(main, "finalize_job"):
            asyncio.run(main.finalize(job.id, main.FinalizeRequest(tracks=[])))
        self.assertTrue(job.lookup_cancel.is_set())
        self.assertEqual(job.status, "finalizing")
