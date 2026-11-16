import asyncio
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi import HTTPException

from app import main


VALID_COOKIES = (
    "# Netscape HTTP Cookie File\n"
    ".youtube.com\tTRUE\t/\tTRUE\t2147483647\tTEST_SESSION\ttest-value\n"
)


class FakeYoutubeDL:
    calls = []

    def __init__(self, options):
        self.options = options
        self.calls.append(self)

    def __enter__(self):
        return self

    def __exit__(self, *_):
        return False

    def extract_info(self, url, download):
        cookiefile = self.options.get("cookiefile")
        if cookiefile:
            assert Path(cookiefile).is_file()
            assert "TEST_SESSION" in Path(cookiefile).read_text()
        if download:
            Path(self.options["outtmpl"]).parent.joinpath("Example.mp3").write_bytes(b"dummy")
        return {"title": "Example", "artist": "Artist", "thumbnail": ""}


class YoutubeCookieTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.root = Path(self.temp_dir.name)
        self.cookie_path = self.root / "youtube-cookies.txt"
        self.cookie_path.write_text(VALID_COOKIES)
        FakeYoutubeDL.calls.clear()
        cookie_patch = patch.object(main, "YOUTUBE_COOKIES_FILE", self.cookie_path)
        ydl_patch = patch.object(main, "YoutubeDL", FakeYoutubeDL)
        cookie_patch.start()
        ydl_patch.start()
        self.addCleanup(cookie_patch.stop)
        self.addCleanup(ydl_patch.stop)

    def test_anonymous_probe_and_job_do_not_use_cookiefile(self):
        main.run_probe("https://www.youtube.com/watch?v=example")
        job = main.Job("anonymous", "https://www.youtube.com/watch?v=example", "mp3", "browser")
        with patch.object(main, "JOBS_DIR", self.root), patch.object(main, "identify_track", side_effect=lambda _, metadata: (metadata, 0, "fallback", "")):
            main.run_job(job)
        self.assertEqual(job.status, "review")
        self.assertEqual(len(FakeYoutubeDL.calls), 2)
        self.assertTrue(all("cookiefile" not in call.options for call in FakeYoutubeDL.calls))

    def test_account_probe_and_job_use_private_temporary_copies(self):
        main.run_probe("https://www.youtube.com/watch?v=example", True)
        job = main.Job("account", "https://www.youtube.com/watch?v=example", "mp3", "browser", use_youtube_cookies=True)
        with patch.object(main, "JOBS_DIR", self.root), patch.object(main, "identify_track", side_effect=lambda _, metadata: (metadata, 0, "fallback", "")):
            main.run_job(job)
        self.assertEqual(job.status, "review")
        paths = [Path(call.options["cookiefile"]) for call in FakeYoutubeDL.calls]
        self.assertEqual(len(paths), 2)
        self.assertNotEqual(paths[0], self.cookie_path)
        self.assertNotEqual(paths[0], paths[1])
        self.assertTrue(all(call.options["extractor_args"]["youtube"]["player_client"] == ["web_safari"] for call in FakeYoutubeDL.calls))
        self.assertTrue(all(not path.exists() for path in paths))
        self.assertEqual(self.cookie_path.read_text(), VALID_COOKIES)

    def test_missing_and_invalid_cookies_return_clear_errors(self):
        self.assertEqual(asyncio.run(main.get_config()), {"youtube_cookies_available": True})
        self.cookie_path.unlink()
        self.assertFalse(main.youtube_cookies_available())
        self.assertEqual(asyncio.run(main.get_config()), {"youtube_cookies_available": False})
        with self.assertRaisesRegex(ValueError, "missing or unreadable"):
            main.run_probe("https://www.youtube.com/watch?v=example", True)
        with self.assertRaises(HTTPException) as probe_error:
            asyncio.run(main.probe(main.ProbeRequest(url="https://www.youtube.com/watch?v=example", use_youtube_cookies=True)))
        self.assertEqual(probe_error.exception.status_code, 400)
        with self.assertRaises(HTTPException) as raised:
            asyncio.run(main.create_job(main.JobRequest(url="https://www.youtube.com/watch?v=example", format="mp3", use_youtube_cookies=True)))
        self.assertEqual(raised.exception.status_code, 400)

        self.cookie_path.write_text("not a cookie file")
        with self.assertRaisesRegex(ValueError, "Netscape"):
            main.run_probe("https://www.youtube.com/watch?v=example", True)

        self.cookie_path.write_text(VALID_COOKIES + ".example.com\tTRUE\t/\tTRUE\t2147483647\tOTHER\tsecret\n")
        with self.assertRaisesRegex(ValueError, "only youtube.com"):
            main.run_probe("https://www.youtube.com/watch?v=example", True)

    def test_temporary_copy_is_deleted_after_yt_dlp_failure(self):
        class FailingYoutubeDL(FakeYoutubeDL):
            def extract_info(self, url, download):
                super().extract_info(url, download)
                raise RuntimeError("network error")

        with patch.object(main, "YoutubeDL", FailingYoutubeDL):
            with self.assertRaisesRegex(RuntimeError, "network error"):
                main.run_probe("https://www.youtube.com/watch?v=example", True)
        self.assertFalse(Path(FakeYoutubeDL.calls[-1].options["cookiefile"]).exists())


if __name__ == "__main__":
    unittest.main()
