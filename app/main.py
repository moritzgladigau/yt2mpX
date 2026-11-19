import asyncio
import hashlib
import json
import queue
import os
import re
import shutil
import subprocess
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from email.utils import parsedate_to_datetime
import uuid
import zipfile
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from http.cookiejar import LoadError, MozillaCookieJar
from pathlib import Path
from typing import Any, Literal

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from mutagen.easyid3 import EasyID3
from mutagen.id3 import APIC, ID3
from mutagen.mp3 import MP3
from mutagen.mp4 import MP4, MP4Cover, MP4FreeForm
from pydantic import BaseModel, Field
from yt_dlp import YoutubeDL
from yt_dlp.postprocessor.common import PostProcessor


ROOT_DIR = Path(__file__).resolve().parent
STATIC_DIR = ROOT_DIR / "static"
DATA_DIR = Path(os.getenv("YTMPX_DATA_DIR", "/data")).resolve()
JOBS_DIR = DATA_DIR / "jobs"
MAX_PLAYLIST_ITEMS = int(os.getenv("YTMPX_MAX_PLAYLIST_ITEMS", "100"))
JOB_TTL_MINUTES = int(os.getenv("YTMPX_JOB_TTL_MINUTES", str(int(os.getenv("YTMPX_JOB_TTL_HOURS", "2")) * 60)))
PROBE_TIMEOUT_SECONDS = int(os.getenv("YTMPX_PROBE_TIMEOUT_SECONDS", "75"))
YTDLP_SOCKET_TIMEOUT_SECONDS = int(os.getenv("YTMPX_YTDLP_SOCKET_TIMEOUT_SECONDS", "30"))
YOUTUBE_COOKIES_FILE = Path(os.getenv("YTMPX_YOUTUBE_COOKIES_FILE", "/run/yt2mpx-secrets/youtube-cookies.txt"))
ACOUSTID_API_KEY = os.getenv("YTMPX_ACOUSTID_API_KEY") or os.getenv("ACOUSTID_API_KEY") or ""
ACOUSTID_MIN_SCORE = float(os.getenv("YTMPX_ACOUSTID_MIN_SCORE", "0.78"))
METADATA_LOOKUP_TIMEOUT_SECONDS = int(os.getenv("YTMPX_METADATA_LOOKUP_TIMEOUT_SECONDS", "20"))
MUSICBRAINZ_USER_AGENT = os.getenv("YTMPX_MUSICBRAINZ_USER_AGENT", "yt2mpX/1.0 ( local-home-use )")
MAX_DOWNLOAD_STEM_LENGTH = 180

musicbrainz_lock = threading.Lock()
last_musicbrainz_request_at = 0.0
musicbrainz_consecutive_503 = 0
musicbrainz_cooldown_until = 0.0
musicbrainz_cache: dict[str, dict[str, Any]] = {}
MUSICBRAINZ_INTERVAL_SECONDS = 1.1
METADATA_JOB_TIMEOUT_SECONDS = 600
tag_write_lock = threading.Lock()


class MetadataHTTPError(RuntimeError):
    def __init__(self, status: int, message: str, retry_after: float = 0):
        super().__init__(f"HTTP {status}: {message}")
        self.status = status
        self.retry_after = retry_after


class ProbeRequest(BaseModel):
    url: str = Field(min_length=1)
    use_youtube_cookies: bool = False


class Metadata(BaseModel):
    title: str = ""
    artist: str = ""
    album: str = ""
    cover_url: str = ""
    date: str = ""
    track_number: str = ""
    disc_number: str = ""
    isrc: str = ""
    musicbrainz_recording_id: str = ""
    musicbrainz_release_id: str = ""
    musicbrainz_release_group_id: str = ""


class JobRequest(BaseModel):
    url: str = Field(min_length=1)
    format: Literal["mp3", "mp4"]
    session_id: str = Field(default="")
    playlist_items: list[int] = Field(default_factory=list)
    audio_quality: Literal["high", "medium", "small", "minimal"] = "medium"
    embed_cover: bool = True
    video_quality: Literal["360", "720", "1080", "best_compatible"] = "best_compatible"
    use_youtube_cookies: bool = False


class FinalizeTrack(BaseModel):
    id: str
    metadata: Metadata


class FinalizeRequest(BaseModel):
    tracks: list[FinalizeTrack] = Field(default_factory=list)


class TrackUpdateRequest(BaseModel):
    metadata: Metadata


@dataclass
class Track:
    id: str
    path: Path
    position: int
    source_title: str
    source_artist: str
    source_cover_url: str
    metadata: Metadata
    confidence: float = 0
    status: str = "fallback"
    message: str = ""
    lookup_status: str = "pending"
    recording_id: str = ""
    source_album: str = ""
    source_date: str = ""
    phase: str = "queued"
    tag_status: str = "waiting"
    tag_error: str = ""
    metadata_revision: int = 0
    written_revision: int = -1


@dataclass
class Job:
    id: str
    url: str
    media_format: str
    session_id: str
    playlist_items: list[int] = field(default_factory=list)
    audio_quality: str = "medium"
    embed_cover: bool = True
    video_quality: str = "best_compatible"
    use_youtube_cookies: bool = False
    status: str = "queued"
    progress: float = 0
    message: str = "Waiting to start"
    error: str = ""
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    updated_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    output_path: Path | None = None
    download_name: str = ""
    is_playlist: bool = False
    collection_title: str = ""
    tracks: list[Track] = field(default_factory=list)
    lookup_cancel: threading.Event = field(default_factory=threading.Event)
    state_lock: threading.RLock = field(default_factory=threading.RLock)
    lookup_finished: bool = False
    lookup_deadline: float = float("inf")
    download_finished: bool = False
    new_track_event: threading.Event = field(default_factory=threading.Event)
    tag_queue: queue.Queue[str] = field(default_factory=queue.Queue)
    tag_stop: threading.Event = field(default_factory=threading.Event)
    cover_cache: dict[str, Path | None] = field(default_factory=dict)


app = FastAPI(title="yt2mpX", version="1.0.0")
jobs: dict[str, Job] = {}


def youtube_cookies_available() -> bool:
    return YOUTUBE_COOKIES_FILE.is_file() and os.access(YOUTUBE_COOKIES_FILE, os.R_OK)


def validate_youtube_cookies(path: Path | None = None) -> None:
    cookie_path = path or YOUTUBE_COOKIES_FILE
    if not cookie_path.is_file() or not os.access(cookie_path, os.R_OK):
        raise ValueError("YouTube cookie file is missing or unreadable. Add secrets/youtube-cookies.txt on the Pi.")
    try:
        with cookie_path.open("rb") as cookie_file:
            header = cookie_file.readline().rstrip(b"\r\n")
        if header not in {b"# Netscape HTTP Cookie File", b"# HTTP Cookie File"}:
            raise ValueError("YouTube cookie file must use Netscape cookies.txt format.")
        jar = MozillaCookieJar(str(cookie_path))
        jar.load(ignore_discard=True)
        domains = [cookie.domain.lstrip(".").lower() for cookie in jar]
        if not any(domain == "youtube.com" or domain.endswith(".youtube.com") for domain in domains):
            raise ValueError("YouTube cookie file contains no valid youtube.com cookies.")
        if any(domain != "youtube.com" and not domain.endswith(".youtube.com") for domain in domains):
            raise ValueError("YouTube cookie file must contain only youtube.com cookies.")
    except (OSError, LoadError, UnicodeError) as exc:
        raise ValueError("YouTube cookie file is unreadable or invalid. Export a fresh Netscape cookies.txt file.") from exc


@contextmanager
def yt_dlp_cookie_options(use_youtube_cookies: bool):
    if not use_youtube_cookies:
        yield {}
        return
    if not youtube_cookies_available():
        raise ValueError("YouTube cookie file is missing or unreadable. Add secrets/youtube-cookies.txt on the Pi.")
    fd, path = tempfile.mkstemp(prefix="yt2mpx-cookies-", suffix=".txt")
    try:
        try:
            with os.fdopen(fd, "wb") as target, YOUTUBE_COOKIES_FILE.open("rb") as source:
                shutil.copyfileobj(source, target)
        except OSError as exc:
            raise ValueError("YouTube cookie file is missing or unreadable. Add secrets/youtube-cookies.txt on the Pi.") from exc
        validate_youtube_cookies(Path(path))
        yield {
            "cookiefile": path,
            "extractor_args": {"youtube": {"player_client": ["web_safari"]}},
        }
    finally:
        Path(path).unlink(missing_ok=True)


def touch(job: Job, *, status: str | None = None, progress: float | None = None, message: str | None = None) -> None:
    if status is not None:
        job.status = status
    if progress is not None:
        job.progress = max(0, min(progress, 100))
    if message is not None:
        job.message = message
    job.updated_at = datetime.now(timezone.utc)


def run_probe(url: str, use_youtube_cookies: bool = False) -> dict[str, Any]:
    opts = {
        "quiet": True,
        "no_warnings": True,
        "extract_flat": "in_playlist",
        "playlistend": MAX_PLAYLIST_ITEMS,
        "socket_timeout": YTDLP_SOCKET_TIMEOUT_SECONDS,
    }
    with yt_dlp_cookie_options(use_youtube_cookies) as cookie_options:
        with YoutubeDL({**opts, **cookie_options}) as ydl:
            info = ydl.extract_info(url, download=False)

    entries = info.get("entries") or []
    is_playlist = bool(entries)
    visible_entries = []
    for position, entry in enumerate(entries[:MAX_PLAYLIST_ITEMS], start=1):
        visible_entries.append(
            {
                "position": position,
                "id": entry.get("id"),
                "title": entry.get("title") or "Untitled item",
                "url": entry.get("url") or entry.get("webpage_url"),
                "duration": entry.get("duration"),
                "uploader": entry.get("uploader") or entry.get("channel"),
            }
        )

    title = info.get("title") or "Untitled"
    artist = info.get("artist") or info.get("uploader") or info.get("channel") or ""
    thumbnail = info.get("thumbnail") or ""

    return {
        "title": title,
        "type": "playlist" if is_playlist else "video",
        "count": len(entries) if is_playlist else 1,
        "thumbnail": thumbnail,
        "suggested_metadata": {
            "title": title,
            "artist": artist,
            "album": "",
            "cover_url": thumbnail,
        },
        "entries": visible_entries,
        "truncated": is_playlist and len(entries) >= MAX_PLAYLIST_ITEMS,
    }


def metadata_dict(metadata: Metadata) -> dict[str, str]:
    return {
        "title": metadata.title,
        "artist": metadata.artist,
        "album": metadata.album,
        "cover_url": metadata.cover_url,
        "date": metadata.date,
        "track_number": metadata.track_number,
        "disc_number": metadata.disc_number,
        "isrc": metadata.isrc,
        "musicbrainz_recording_id": metadata.musicbrainz_recording_id,
        "musicbrainz_release_id": metadata.musicbrainz_release_id,
        "musicbrainz_release_group_id": metadata.musicbrainz_release_group_id,
    }


def track_response(track: Track) -> dict[str, Any]:
    return {
        "id": track.id,
        "position": track.position,
        "source_title": track.source_title,
        "source_artist": track.source_artist,
        "source_album": track.source_album,
        "source_date": track.source_date,
        "source_cover_url": track.source_cover_url,
        "metadata": metadata_dict(track.metadata),
        "confidence": round(track.confidence, 3),
        "status": track.status,
        "message": track.message,
        "lookup_status": track.lookup_status,
        "phase": track.phase,
        "tag_status": track.tag_status,
        "tag_error": track.tag_error,
        "metadata_revision": track.metadata_revision,
        "written_revision": track.written_revision,
        "file_name": track.path.name,
    }


def first_text(*values: Any) -> str:
    for value in values:
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


def thumbnail_from_info(info: dict[str, Any]) -> str:
    thumbnail = first_text(info.get("thumbnail"))
    if thumbnail:
        return thumbnail
    thumbnails = info.get("thumbnails") or []
    if isinstance(thumbnails, list) and thumbnails:
        for item in reversed(thumbnails):
            if isinstance(item, dict):
                thumbnail = first_text(item.get("url"))
                if thumbnail:
                    return thumbnail
    return ""


def fallback_metadata(source: dict[str, Any], collection_title: str = "") -> Metadata:
    return Metadata(
        title=first_text(source.get("track"), source.get("title"), "Untitled track"),
        artist=first_text(source.get("artist"), source.get("uploader"), source.get("channel")),
        album=first_text(source.get("album"), collection_title),
        cover_url=thumbnail_from_info(source),
        date=first_text(str(source.get("release_date") or ""), str(source.get("upload_date") or "")),
    )


def json_http_error(exc: urllib.error.HTTPError) -> MetadataHTTPError:
    body = exc.read().decode("utf-8", errors="replace")
    try:
        data = json.loads(body)
    except json.JSONDecodeError:
        data = body
    if isinstance(data, dict):
        error = data.get("error")
        message = first_text(error.get("message") if isinstance(error, dict) else error, data.get("message"), body)
    else:
        message = first_text(data, body)
    retry_after = 0.0
    header = exc.headers.get("Retry-After", "") if exc.headers else ""
    try:
        retry_after = max(0.0, float(header))
    except (TypeError, ValueError):
        try:
            retry_after = max(0.0, (parsedate_to_datetime(header) - datetime.now(timezone.utc)).total_seconds())
        except (TypeError, ValueError, OverflowError):
            pass
    return MetadataHTTPError(exc.code, message or str(exc.reason), retry_after)


def fetch_request_json(request: urllib.request.Request) -> dict[str, Any]:
    try:
        with urllib.request.urlopen(request, timeout=METADATA_LOOKUP_TIMEOUT_SECONDS) as response:
            data = json.loads(response.read().decode("utf-8"))
            if not isinstance(data, dict):
                raise RuntimeError("The metadata service returned an unexpected response.")
            return data
    except urllib.error.HTTPError as exc:
        raise json_http_error(exc) from exc


def fetch_json(url: str, *, user_agent: str = "yt2mpX/1.0") -> dict[str, Any]:
    request = urllib.request.Request(url, headers={"User-Agent": user_agent, "Accept": "application/json"})
    return fetch_request_json(request)


def post_form_json(url: str, payload: dict[str, Any], *, user_agent: str = "yt2mpX/1.0") -> dict[str, Any]:
    body = urllib.parse.urlencode(payload).encode("utf-8")
    request = urllib.request.Request(
        url,
        data=body,
        headers={
            "User-Agent": user_agent,
            "Accept": "application/json",
            "Content-Type": "application/x-www-form-urlencoded",
        },
        method="POST",
    )
    return fetch_request_json(request)


def fpcalc(path: Path) -> tuple[int, str]:
    completed = subprocess.run(
        ["fpcalc", "-json", str(path)],
        check=True,
        capture_output=True,
        text=True,
        timeout=METADATA_LOOKUP_TIMEOUT_SECONDS,
    )
    data = json.loads(completed.stdout)
    duration = int(float(data.get("duration") or 0))
    fingerprint = first_text(data.get("fingerprint"))
    if not duration or not fingerprint:
        raise RuntimeError("Could not generate an audio fingerprint.")
    return duration, fingerprint


def text_tokens(value: str) -> set[str]:
    cleaned = re.sub(r"[^a-z0-9]+", " ", value.casefold())
    return {token for token in cleaned.split() if len(token) > 1}


def token_overlap(left: str, right: str) -> int:
    return len(text_tokens(left) & text_tokens(right))


def acoustid_artist_name(recording: dict[str, Any]) -> str:
    artists = []
    for artist in recording.get("artists") or []:
        name = first_text(artist.get("name") if isinstance(artist, dict) else "")
        if name:
            artists.append(name)
    return ", ".join(artists)


def candidate_rank(score: float, recording: dict[str, Any], fallback: Metadata) -> float:
    title = first_text(recording.get("title"))
    artist = acoustid_artist_name(recording)
    source_title = f"{fallback.title} {fallback.artist}"
    release_count = len(recording.get("releases") or []) + len(recording.get("releasegroups") or [])
    rank = score * 100
    rank += token_overlap(title, source_title) * 8
    rank += token_overlap(artist, source_title) * 10
    rank += min(release_count, 12) * 1.5
    if title and title.casefold() in fallback.title.casefold():
        rank += 12
    if artist and artist.casefold() in f"{fallback.title} {fallback.artist}".casefold():
        rank += 12
    return rank


def acoustid_lookup(duration: int, fingerprint: str, fallback: Metadata) -> tuple[float, str]:
    if not ACOUSTID_API_KEY:
        raise RuntimeError("No AcoustID API key is configured.")
    data = post_form_json(
        "https://api.acoustid.org/v2/lookup",
        {
            "client": ACOUSTID_API_KEY,
            "format": "json",
            "duration": duration,
            "fingerprint": fingerprint,
            "meta": "recordings releasegroups releases tracks",
        },
    )
    if data.get("status") == "error":
        error = data.get("error")
        raise RuntimeError(first_text(error.get("message") if isinstance(error, dict) else error, "AcoustID rejected the lookup."))
    results = data.get("results") or []
    if not isinstance(results, list):
        raise RuntimeError("AcoustID returned an unexpected results format.")
    best_acoustid_score = 0.0
    best_rank = 0.0
    best_recording_id = ""
    for result in results:
        if not isinstance(result, dict):
            continue
        try:
            score = float(result.get("score") or 0)
        except (TypeError, ValueError):
            score = 0.0
        for recording in result.get("recordings") or []:
            if not isinstance(recording, dict):
                continue
            recording_id = first_text(recording.get("id"))
            rank = candidate_rank(score, recording, fallback)
            if recording_id and rank > best_rank:
                best_rank = rank
                best_acoustid_score = score
                best_recording_id = recording_id
    if not best_recording_id:
        raise RuntimeError("AcoustID found no matching MusicBrainz recording.")
    return best_acoustid_score, best_recording_id


def wait_for_lookup(job: Job | None, seconds: float) -> bool:
    end = time.monotonic() + max(0, seconds)
    while time.monotonic() < end:
        if job and (job.lookup_cancel.is_set() or time.monotonic() >= job.lookup_deadline):
            return False
        time.sleep(min(0.2, end - time.monotonic()))
    return not job or not job.lookup_cancel.is_set() and time.monotonic() < job.lookup_deadline


def musicbrainz_json(path: str, params: dict[str, str] | None = None, job: Job | None = None) -> dict[str, Any]:
    global last_musicbrainz_request_at, musicbrainz_consecutive_503, musicbrainz_cooldown_until
    params = {**(params or {}), "fmt": "json"}
    url = f"https://musicbrainz.org/ws/2/{path}?{urllib.parse.urlencode(params)}"
    with musicbrainz_lock:
        if url in musicbrainz_cache:
            return musicbrainz_cache[url]
        wait_seconds = max(
            MUSICBRAINZ_INTERVAL_SECONDS - (time.monotonic() - last_musicbrainz_request_at),
            musicbrainz_cooldown_until - time.monotonic(),
        )
        if not wait_for_lookup(job, wait_seconds):
            raise RuntimeError("Metadata lookup was cancelled or timed out.")
        last_musicbrainz_request_at = time.monotonic()
        try:
            data = fetch_json(url, user_agent=MUSICBRAINZ_USER_AGENT)
        except MetadataHTTPError as exc:
            if exc.status == 503:
                musicbrainz_consecutive_503 += 1
                if musicbrainz_consecutive_503 >= 3:
                    cooldown = min(120, 30 * 2 ** (musicbrainz_consecutive_503 - 3))
                    musicbrainz_cooldown_until = max(musicbrainz_cooldown_until, time.monotonic() + cooldown)
            if exc.retry_after:
                musicbrainz_cooldown_until = max(musicbrainz_cooldown_until, time.monotonic() + exc.retry_after)
            raise
        musicbrainz_consecutive_503 = 0
        musicbrainz_cooldown_until = 0.0
        if len(musicbrainz_cache) >= 2048:
            musicbrainz_cache.clear()
        musicbrainz_cache[url] = data
    return data


def artist_credit_name(entity: dict[str, Any]) -> str:
    names = []
    for credit in entity.get("artist-credit") or []:
        if isinstance(credit, dict):
            artist = credit.get("artist") or {}
            name = first_text(credit.get("name"), artist.get("name") if isinstance(artist, dict) else "")
            if name:
                names.append(name)
    return ", ".join(names)


def first_release(recording: dict[str, Any]) -> dict[str, Any]:
    releases = [release for release in recording.get("releases") or [] if isinstance(release, dict)]
    if not releases:
        return {}
    official = [release for release in releases if release.get("status") == "Official"]
    dated = sorted(
        official or releases,
        key=lambda release: first_text(release.get("date")) or "9999",
    )
    return dated[0] if dated else releases[0]


def release_track_numbers(release: dict[str, Any], recording_id: str) -> tuple[str, str]:
    for medium in release.get("media") or []:
        if not isinstance(medium, dict):
            continue
        disc_number = str(medium.get("position") or "")
        for track in medium.get("tracks") or []:
            if not isinstance(track, dict):
                continue
            recording = track.get("recording") or {}
            if isinstance(recording, dict) and recording.get("id") == recording_id:
                return str(track.get("number") or track.get("position") or ""), disc_number
    return "", ""


def musicbrainz_metadata(recording_id: str, fallback: Metadata, job: Job | None = None) -> Metadata:
    recording = musicbrainz_json(
        f"recording/{recording_id}",
        {"inc": "artists+releases+release-groups+media+isrcs"},
        job,
    )
    release = first_release(recording)
    release_group = release.get("release-group") or {}
    track_number, disc_number = release_track_numbers(release, recording_id)
    release_id = first_text(release.get("id"))
    release_group_id = first_text(release_group.get("id"))
    cover_url = f"https://coverartarchive.org/release/{release_id}/front-500" if release_id else fallback.cover_url
    isrcs = recording.get("isrcs") or []
    return Metadata(
        title=first_text(recording.get("title"), fallback.title),
        artist=first_text(artist_credit_name(recording), fallback.artist),
        album=first_text(release.get("title"), fallback.album),
        cover_url=cover_url,
        date=first_text(release.get("date"), fallback.date),
        track_number=first_text(track_number, fallback.track_number),
        disc_number=first_text(disc_number, fallback.disc_number),
        isrc=first_text(isrcs[0] if isrcs else "", fallback.isrc),
        musicbrainz_recording_id=recording_id,
        musicbrainz_release_id=release_id,
        musicbrainz_release_group_id=release_group_id,
    )


def queue_track_tag(job: Job, track: Track) -> None:
    with job.state_lock:
        if track.written_revision >= track.metadata_revision or track.tag_status in {"queued", "writing"}:
            return
        track.tag_status = "queued"
        track.phase = "tags_queued"
        job.tag_queue.put(track.id)


def finish_lookup_track(job: Job, track: Track, status: str, message: str, metadata: Metadata | None = None) -> None:
    with job.state_lock:
        if job.lookup_cancel.is_set() or track.lookup_status == "edited":
            return
        if time.monotonic() >= job.lookup_deadline:
            status = "unavailable"
            message = "The ten-minute metadata lookup limit was reached."
            metadata = None
        track.lookup_status = status
        track.message = message
        if metadata is not None:
            track.metadata = metadata
            track.metadata_revision += 1
            track.status = "matched"
        else:
            track.status = "fallback"
        queue_track_tag(job, track)


def lookup_one_track(job: Job, track: Track) -> bool:
    with job.state_lock:
        if job.lookup_cancel.is_set() or track.lookup_status == "edited":
            return False
        track.lookup_status = "searching"
        track.phase = "fingerprint" if not track.recording_id else "musicbrainz"
    try:
        if not ACOUSTID_API_KEY:
            finish_lookup_track(job, track, "unavailable", "No AcoustID API key is configured.")
            return False
        if not track.recording_id:
            duration, fingerprint = fpcalc(track.path)
            with job.state_lock:
                track.phase = "acoustid"
            score, recording_id = acoustid_lookup(duration, fingerprint, track.metadata)
            if score < ACOUSTID_MIN_SCORE:
                with job.state_lock:
                    track.confidence = score
                finish_lookup_track(job, track, "no_match", "The AcoustID match was too uncertain.")
                return False
            with job.state_lock:
                track.confidence = score
                track.recording_id = recording_id
        with job.state_lock:
            track.phase = "musicbrainz"
        metadata = musicbrainz_metadata(track.recording_id, track.metadata, job)
        finish_lookup_track(job, track, "matched", "MusicBrainz match found.", metadata)
    except MetadataHTTPError as exc:
        if exc.status in {429, 502, 503, 504}:
            with job.state_lock:
                if not job.lookup_cancel.is_set() and track.lookup_status != "edited":
                    track.lookup_status = "retrying"
                    track.phase = "retrying"
                    track.message = f"Metadata service returned HTTP {exc.status}; retrying after this pass."
            return True
        status = "no_match" if exc.status == 404 else "unavailable"
        finish_lookup_track(job, track, status, str(exc))
    except FileNotFoundError:
        finish_lookup_track(job, track, "unavailable", "fpcalc is not installed.")
    except Exception as exc:
        message = str(exc)
        if "invalid api key" in message.casefold():
            message = "Invalid AcoustID API key. Use the Application API Key, not a user key."
        status = "no_match" if "no matching musicbrainz recording" in message.casefold() else "unavailable"
        finish_lookup_track(job, track, status, message)
    return False


def lookup_job_metadata(job: Job) -> None:
    next_index = 0
    retry: list[Track] = []
    try:
        while not job.lookup_cancel.is_set():
            if time.monotonic() >= job.lookup_deadline:
                break
            with job.state_lock:
                track = job.tracks[next_index] if next_index < len(job.tracks) else None
                download_finished = job.download_finished
                if track is not None:
                    next_index += 1
            if track is None:
                if download_finished:
                    break
                job.new_track_event.wait(0.25)
                job.new_track_event.clear()
                continue
            if lookup_one_track(job, track):
                retry.append(track)
        while retry and not job.lookup_cancel.is_set() and time.monotonic() < job.lookup_deadline:
            remaining = []
            for track in retry:
                if job.lookup_cancel.is_set() or time.monotonic() >= job.lookup_deadline:
                    remaining.append(track)
                    continue
                if lookup_one_track(job, track):
                    remaining.append(track)
            retry = remaining
        if not job.lookup_cancel.is_set():
            with job.state_lock:
                unfinished = [*retry, *job.tracks[next_index:]]
            for track in unfinished:
                finish_lookup_track(job, track, "unavailable", "The ten-minute metadata lookup limit was reached.")
    finally:
        with job.state_lock:
            job.lookup_finished = True
        maybe_finish_review(job)


def progress_hook(job: Job):
    def hook(data: dict[str, Any]) -> None:
        status = data.get("status")
        total = max(1, len(set(job.playlist_items)))
        if status == "downloading":
            raw_percent = (data.get("_percent_str") or "").strip().replace("%", "")
            try:
                percent = float(raw_percent)
                with job.state_lock:
                    progress = 1 + (len(job.tracks) + percent / 100) / total * 65
                    touch(job, status="running", progress=max(job.progress, min(progress, 66)), message="Downloading")
            except ValueError:
                with job.state_lock:
                    touch(job, status="running", message="Downloading")
        elif status == "finished":
            with job.state_lock:
                touch(job, status="running", message="Converting file")

    return hook


def mp3_quality_value(quality: str) -> str:
    return {
        "high": "0",
        "medium": "192",
        "small": "128",
        "minimal": "32",
    }.get(quality, "0")


def is_minimal_audio_quality(quality: str) -> bool:
    return quality == "minimal"


def should_write_covers(job: Job) -> bool:
    return job.media_format != "mp3" or job.embed_cover


def mp4_format_selector(quality: str) -> str:
    height_filter = ""
    if quality in {"360", "720", "1080"}:
        height_filter = f"[height<={quality}]"

    return (
        f"bv*[vcodec^=avc1][ext=mp4]{height_filter}+ba[acodec^=mp4a][ext=m4a]/"
        f"b[vcodec^=avc1][ext=mp4]{height_filter}/"
        f"bv*[vcodec^=avc1]{height_filter}+ba[acodec^=mp4a]/"
        f"best[vcodec^=avc1]{height_filter}/"
        f"best[ext=mp4]{height_filter}/"
        "best"
    )


def account_hls_format_selector(media_format: str, quality: str) -> str:
    if media_format == "mp3":
        if quality == "minimal":
            return "worstaudio[protocol=m3u8_native]/worst[protocol=m3u8_native]/worstaudio/worst"
        return "bestaudio[protocol=m3u8_native]/best[protocol=m3u8_native][height<=360]/best[protocol=m3u8_native]/bestaudio/best"
    height_filter = f"[height<={quality}]" if quality in {"360", "720", "1080"} else ""
    return f"best[protocol=m3u8_native][vcodec^=avc1][ext=mp4]{height_filter}/{mp4_format_selector(quality)}"


def ydl_options(job: Job, work_dir: Path) -> dict[str, Any]:
    is_playlist_selection = bool(job.playlist_items)
    is_minimal_audio = job.media_format == "mp3" and is_minimal_audio_quality(job.audio_quality)
    if is_playlist_selection:
        outtmpl = str(work_dir / "%(title).170B __yt2mpx_%(playlist_index)05d.%(ext)s")
    else:
        outtmpl = str(work_dir / "%(title).180B.%(ext)s")
    common: dict[str, Any] = {
        "outtmpl": outtmpl,
        "quiet": True,
        "no_warnings": True,
        "progress_hooks": [progress_hook(job)],
        "playlistend": MAX_PLAYLIST_ITEMS,
        "windowsfilenames": True,
        "restrictfilenames": False,
        "socket_timeout": YTDLP_SOCKET_TIMEOUT_SECONDS,
    }
    if is_playlist_selection:
        common["playlist_items"] = ",".join(str(item) for item in sorted(set(job.playlist_items)))
    if job.media_format == "mp3":
        postprocessors: list[dict[str, Any]] = [
            {
                "key": "FFmpegExtractAudio",
                "preferredcodec": "mp3",
                "preferredquality": mp3_quality_value(job.audio_quality),
            }
        ]
        common.update(
            {
                "format": (
                    account_hls_format_selector("mp3", job.audio_quality)
                    if job.use_youtube_cookies
                    else "worstaudio/worst" if is_minimal_audio else "bestaudio/best"
                ),
                "postprocessors": postprocessors,
            }
        )
    else:
        common.update(
            {
                "format": account_hls_format_selector("mp4", job.video_quality) if job.use_youtube_cookies else mp4_format_selector(job.video_quality),
                "merge_output_format": "mp4",
            }
        )
    return common


def download_cover(url: str, target_stem: Path) -> Path | None:
    if not url:
        return None
    try:
        request = urllib.request.Request(url, headers={"User-Agent": "yt2mpX/1.0"})
        with urllib.request.urlopen(request, timeout=20) as response:
            content_type = response.headers.get("content-type", "")
            suffix = ".jpg"
            if "png" in content_type:
                suffix = ".png"
            final_path = target_stem.with_suffix(suffix)
            final_path.write_bytes(response.read())
            return final_path
    except Exception:
        return None


def easy_set(tags: EasyID3, key: str, value: str) -> None:
    if not value:
        return
    try:
        tags[key] = value
    except KeyError:
        return


def mp4_freeform(value: str) -> list[MP4FreeForm]:
    return [MP4FreeForm(value.encode("utf-8"), dataformat=1)]


def parse_positive_int(value: str) -> int:
    match = re.search(r"\d+", value or "")
    return int(match.group(0)) if match else 0


def apply_mp3_metadata(path: Path, metadata: Metadata, cover_path: Path | None) -> None:
    audio = MP3(path, ID3=ID3)
    if audio.tags is None:
        audio.add_tags()
        audio.save()

    easy = EasyID3(path)
    for key in ("title", "artist", "album", "date", "tracknumber", "discnumber", "isrc", "musicbrainz_trackid", "musicbrainz_albumid", "musicbrainz_releasegroupid"):
        if key in easy:
            del easy[key]
    easy_set(easy, "title", metadata.title)
    easy_set(easy, "artist", metadata.artist)
    easy_set(easy, "album", metadata.album)
    easy_set(easy, "date", metadata.date)
    easy_set(easy, "tracknumber", metadata.track_number)
    easy_set(easy, "discnumber", metadata.disc_number)
    easy_set(easy, "isrc", metadata.isrc)
    easy_set(easy, "musicbrainz_trackid", metadata.musicbrainz_recording_id)
    easy_set(easy, "musicbrainz_albumid", metadata.musicbrainz_release_id)
    easy_set(easy, "musicbrainz_releasegroupid", metadata.musicbrainz_release_group_id)
    easy.save()

    tags = ID3(path)
    tags.delall("APIC")
    if cover_path:
        mime = "image/png" if cover_path.suffix.lower() == ".png" else "image/jpeg"
        tags.add(APIC(encoding=3, mime=mime, type=3, desc="Cover", data=cover_path.read_bytes()))
    tags.save(v2_version=3)


def apply_mp4_metadata(path: Path, metadata: Metadata, cover_path: Path | None) -> None:
    video = MP4(path)
    for key in ("\xa9nam", "\xa9ART", "\xa9alb", "\xa9day", "trkn", "disk", "----:com.apple.iTunes:ISRC", "----:com.apple.iTunes:MusicBrainz Track Id", "----:com.apple.iTunes:MusicBrainz Album Id", "----:com.apple.iTunes:MusicBrainz Release Group Id"):
        video.pop(key, None)
    if metadata.title:
        video["\xa9nam"] = [metadata.title]
    if metadata.artist:
        video["\xa9ART"] = [metadata.artist]
    if metadata.album:
        video["\xa9alb"] = [metadata.album]
    if metadata.date:
        video["\xa9day"] = [metadata.date]
    track_number = parse_positive_int(metadata.track_number)
    disc_number = parse_positive_int(metadata.disc_number)
    if track_number:
        video["trkn"] = [(track_number, 0)]
    if disc_number:
        video["disk"] = [(disc_number, 0)]
    if metadata.isrc:
        video["----:com.apple.iTunes:ISRC"] = mp4_freeform(metadata.isrc)
    if metadata.musicbrainz_recording_id:
        video["----:com.apple.iTunes:MusicBrainz Track Id"] = mp4_freeform(metadata.musicbrainz_recording_id)
    if metadata.musicbrainz_release_id:
        video["----:com.apple.iTunes:MusicBrainz Album Id"] = mp4_freeform(metadata.musicbrainz_release_id)
    if metadata.musicbrainz_release_group_id:
        video["----:com.apple.iTunes:MusicBrainz Release Group Id"] = mp4_freeform(metadata.musicbrainz_release_group_id)
    video.pop("covr", None)
    if cover_path:
        image_format = MP4Cover.FORMAT_PNG if cover_path.suffix.lower() == ".png" else MP4Cover.FORMAT_JPEG
        video["covr"] = [MP4Cover(cover_path.read_bytes(), imageformat=image_format)]
    video.save()


def cover_for_track(track: Track, work_dir: Path, metadata: Metadata | None = None, cache: dict[str, Path | None] | None = None) -> Path | None:
    cover_url = (metadata or track.metadata).cover_url.strip()
    if not cover_url:
        return None
    def get_cover(url: str) -> Path | None:
        if cache is not None and url in cache:
            return cache[url]
        target_stem = work_dir / f"cover-{hashlib.sha256(url.encode('utf-8')).hexdigest()[:24]}"
        path = download_cover(url, target_stem)
        if cache is not None:
            cache[url] = path
        return path

    cover_path = get_cover(cover_url)
    if cover_path:
        return cover_path
    fallback_cover_url = track.source_cover_url.strip()
    if fallback_cover_url and fallback_cover_url != cover_url:
        return get_cover(fallback_cover_url)
    return None


def media_files(work_dir: Path, media_format: str) -> list[Path]:
    files = [path for path in work_dir.iterdir() if path.is_file() and path.suffix.lower() == f".{media_format}"]
    return sorted(files, key=lambda path: path.stat().st_mtime_ns)


def playlist_position_from_name(path: Path) -> int | None:
    match = re.search(r"__yt2mpx_(\d+)$", path.stem)
    if not match:
        return None
    return int(match.group(1))


def deduplicate_playlist_filenames(files: list[Path]) -> list[Path]:
    seen: dict[str, int] = {}
    renamed_files = []
    for file_path in files:
        stem = re.sub(r" __yt2mpx_\d+$", "", file_path.stem)
        stem_key = stem.casefold()
        count = seen.get(stem_key, 0)
        seen[stem_key] = count + 1
        suffix = "" if count == 0 else f" ({count})"
        candidate = file_path.with_name(f"{stem}{suffix}{file_path.suffix}")
        while candidate.exists():
            if candidate == file_path:
                break
            count += 1
            seen[stem_key] = count + 1
            candidate = file_path.with_name(f"{stem} ({count}){file_path.suffix}")
        if candidate != file_path:
            file_path.rename(candidate)
            renamed_files.append(candidate)
        else:
            renamed_files.append(file_path)
    return renamed_files


def build_zip(files: list[Path], target: Path) -> None:
    with zipfile.ZipFile(target, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for file_path in files:
            archive.write(file_path, arcname=file_path.name)


def safe_filename_stem(value: str, fallback: str = "yt2mpX-download") -> str:
    safe = "".join(char if char.isalnum() or char in " ._-" else "_" for char in value).strip(" ._-")
    safe = safe[:MAX_DOWNLOAD_STEM_LENGTH].rstrip(" ._-")
    return safe or fallback


def metadata_download_stem(metadata: Metadata) -> str:
    title = metadata.title.strip()
    artist = metadata.artist.strip()
    if title and artist:
        return f"{title} - {artist}"
    return title or artist or "yt2mpX-download"


def common_album(tracks: list[Track]) -> str:
    albums = {track.metadata.album.strip() for track in tracks if track.metadata.album.strip()}
    return albums.pop() if len(albums) == 1 else ""


def safe_download_name(job: Job, files: list[Path]) -> str:
    base = ""
    if len(files) == 1 and not job.playlist_items and job.tracks:
        base = metadata_download_stem(job.tracks[0].metadata)
    else:
        base = common_album(job.tracks) or job.collection_title or "yt2mpX-playlist"
    safe = "".join(char if char.isalnum() or char in " ._-" else "_" for char in base).strip()
    suffix = files[0].suffix if len(files) == 1 and not job.playlist_items else ".zip"
    return f"{safe_filename_stem(safe)}{suffix}"


def rename_output_file(path: Path, download_name: str) -> Path:
    target = path.with_name(download_name)
    if target == path:
        return path
    path.rename(target)
    return target


def unpacked_zip_size(path: Path) -> int | None:
    if path.suffix.lower() != ".zip":
        return None
    try:
        with zipfile.ZipFile(path) as archive:
            return sum(info.file_size for info in archive.infolist())
    except zipfile.BadZipFile:
        return None


def playlist_entries_by_position(info: dict[str, Any], selected_positions: list[int]) -> dict[int, dict[str, Any]]:
    entries_by_position: dict[int, dict[str, Any]] = {}
    for index, entry in enumerate(info.get("entries") or []):
        if not isinstance(entry, dict):
            continue
        position = entry.get("playlist_index") or entry.get("playlist_autonumber")
        if not isinstance(position, int):
            position = selected_positions[index] if index < len(selected_positions) else index + 1
        entries_by_position[position] = entry
    return entries_by_position


def build_tracks(job: Job, info: dict[str, Any], files: list[Path], file_positions: list[int | None]) -> list[Track]:
    selected_positions = sorted(set(job.playlist_items))
    entries_by_position = playlist_entries_by_position(info, selected_positions)
    entries = [entry for entry in (info.get("entries") or []) if isinstance(entry, dict)]
    tracks = []
    for index, path in enumerate(files):
        if job.is_playlist or job.playlist_items:
            position = file_positions[index] if index < len(file_positions) else None
            if position is None:
                position = selected_positions[index] if index < len(selected_positions) else index + 1
            source = entries_by_position.get(position) or (entries[index] if index < len(entries) else {})
        else:
            position = 1
            source = info
        fallback = fallback_metadata(source, job.collection_title if job.is_playlist or job.playlist_items else "")
        tracks.append(
            Track(
                id=uuid.uuid4().hex,
                path=path,
                position=position,
                source_title=fallback.title,
                source_artist=fallback.artist,
                source_cover_url=fallback.cover_url,
                metadata=fallback,
                source_album=fallback.album,
                source_date=fallback.date,
            )
        )
    return tracks


def register_ready_track(job: Job, work_dir: Path, info: dict[str, Any]) -> None:
    path_text = first_text(info.get("filepath"), info.get("_filename"))
    if not path_text:
        return
    path = Path(path_text).resolve()
    if not path.is_relative_to(work_dir.resolve()) or not path.is_file() or path.suffix.lower() != f".{job.media_format}":
        return
    position = info.get("playlist_index")
    if not isinstance(position, int) or position < 1:
        position = playlist_position_from_name(path) or 1
    collection_title = first_text(info.get("playlist_title"), job.collection_title)
    fallback = fallback_metadata(info, collection_title)
    with job.state_lock:
        if any(track.path == path for track in job.tracks):
            return
        job.tracks.append(Track(
            id=uuid.uuid4().hex,
            path=path,
            position=position,
            source_title=fallback.title,
            source_artist=fallback.artist,
            source_cover_url=fallback.cover_url,
            source_album=fallback.album,
            source_date=fallback.date,
            metadata=fallback,
            phase="fingerprint",
        ))
        job.new_track_event.set()


class ReadyTrackPostProcessor(PostProcessor):
    def __init__(self, downloader: YoutubeDL, job: Job, work_dir: Path):
        super().__init__(downloader)
        self.job = job
        self.work_dir = work_dir

    def run(self, information: dict[str, Any]):
        register_ready_track(self.job, self.work_dir, information)
        return [], information


def maybe_finish_review(job: Job) -> None:
    with job.state_lock:
        if job.status != "running" or not job.download_finished or not job.lookup_finished:
            return
        if not job.tracks or any(track.tag_status not in {"written", "error"} for track in job.tracks):
            return
        touch(job, status="review", progress=91, message="Review completed metadata")


def write_track_tags(job: Job, track: Track) -> None:
    with job.state_lock:
        if track.written_revision >= track.metadata_revision:
            track.tag_status = "written"
            track.phase = "ready"
            return
        revision = track.metadata_revision
        metadata = track.metadata.model_copy(deep=True)
        track.tag_status = "writing"
        track.phase = "tagging"
        track.tag_error = ""
    try:
        with tag_write_lock:
            work_dir = JOBS_DIR / job.id / "work"
            cover_path = cover_for_track(track, work_dir, metadata, job.cover_cache) if should_write_covers(job) else None
            if job.media_format == "mp3":
                apply_mp3_metadata(track.path, metadata, cover_path)
            else:
                apply_mp4_metadata(track.path, metadata, cover_path)
        with job.state_lock:
            track.written_revision = revision
            if track.metadata_revision != revision:
                track.tag_status = "waiting"
                queue_track_tag(job, track)
            else:
                track.tag_status = "written"
                track.phase = "ready"
    except Exception as exc:
        with job.state_lock:
            track.tag_status = "error"
            track.phase = "tag_error"
            track.tag_error = str(exc)


def tag_job_metadata(job: Job) -> None:
    while not job.tag_stop.is_set() or not job.tag_queue.empty():
        try:
            track_id = job.tag_queue.get(timeout=0.25)
        except queue.Empty:
            continue
        try:
            with job.state_lock:
                track = next((item for item in job.tracks if item.id == track_id), None)
            if track is not None and job.status != "failed":
                write_track_tags(job, track)
        finally:
            job.tag_queue.task_done()
            maybe_finish_review(job)


def run_job(job: Job) -> None:
    job_dir = JOBS_DIR / job.id
    work_dir = job_dir / "work"
    work_dir.mkdir(parents=True, exist_ok=True)

    try:
        touch(job, status="running", progress=1, message="Checking link")
        threading.Thread(target=tag_job_metadata, args=(job,), daemon=True).start()
        threading.Thread(target=lookup_job_metadata, args=(job,), daemon=True).start()
        with yt_dlp_cookie_options(job.use_youtube_cookies) as cookie_options:
            with YoutubeDL({**ydl_options(job, work_dir), **cookie_options}) as ydl:
                ydl.add_post_processor(ReadyTrackPostProcessor(ydl, job, work_dir), when="after_move")
                info = ydl.extract_info(job.url, download=True)
                job.is_playlist = bool(info.get("entries"))
                job.collection_title = first_text(info.get("title"), "yt2mpX-download")

        files = media_files(work_dir, job.media_format)
        if not files:
            raise RuntimeError("No output file was created.")
        file_positions = [playlist_position_from_name(path) for path in files]
        with job.state_lock:
            for track in build_tracks(job, info, files, file_positions):
                if not any(existing.path.resolve() == track.path.resolve() for existing in job.tracks):
                    job.tracks.append(track)
            job.new_track_event.set()
            job.lookup_deadline = time.monotonic() + METADATA_JOB_TIMEOUT_SECONDS
            job.download_finished = True
            touch(job, progress=70, message="Download complete; finishing metadata")
        maybe_finish_review(job)
    except Exception as exc:
        with job.state_lock:
            job.lookup_cancel.set()
            job.tag_stop.set()
            job.new_track_event.set()
            job.error = str(exc)
            touch(job, status="failed", progress=0, message="Failed")


def finalize_job(job: Job) -> None:
    job_dir = JOBS_DIR / job.id
    work_dir = job_dir / "work"
    try:
        if not job.tracks:
            raise RuntimeError("No tracks were found to finalize.")
        touch(job, status="finalizing", progress=93, message="Finishing remaining tags")
        for track in job.tracks:
            if track.tag_status == "error" or track.written_revision < track.metadata_revision:
                with job.state_lock:
                    track.tag_status = "waiting"
                queue_track_tag(job, track)
        job.tag_queue.join()
        failed_tracks = [track for track in job.tracks if track.tag_status != "written"]
        if failed_tracks:
            raise RuntimeError(f"Could not write metadata for {len(failed_tracks)} track(s): {failed_tracks[0].tag_error}")

        if job.playlist_items:
            ordered = sorted(job.tracks, key=lambda item: item.position)
            renamed = deduplicate_playlist_filenames([track.path for track in ordered])
            for track, path in zip(ordered, renamed):
                track.path = path

        files = [track.path for track in sorted(job.tracks, key=lambda item: item.position) if track.path.exists()]
        if not files:
            raise RuntimeError("No output files were found to package.")

        job.download_name = safe_download_name(job, files)
        touch(job, progress=98, message="Preparing download")
        if len(files) == 1 and not job.playlist_items:
            job.output_path = rename_output_file(files[0], job.download_name)
            job.tracks[0].path = job.output_path
        else:
            zip_path = job_dir / job.download_name
            build_zip(files, zip_path)
            job.output_path = zip_path

        touch(job, status="done", progress=100, message="Ready")
    except Exception as exc:
        job.error = str(exc)
        touch(job, status="failed", progress=0, message="Failed")
    finally:
        job.tag_stop.set()


async def cleanup_loop() -> None:
    while True:
        await asyncio.sleep(5 * 60)
        cutoff = datetime.now(timezone.utc) - timedelta(minutes=JOB_TTL_MINUTES)
        for job_id, job in list(jobs.items()):
            if job.updated_at < cutoff and job.status in {"done", "failed", "review"}:
                job.lookup_cancel.set()
                job.tag_stop.set()
                shutil.rmtree(JOBS_DIR / job_id, ignore_errors=True)
                jobs.pop(job_id, None)


@app.on_event("startup")
async def startup() -> None:
    JOBS_DIR.mkdir(parents=True, exist_ok=True)
    asyncio.create_task(cleanup_loop())


@app.post("/api/probe")
async def probe(request: ProbeRequest) -> dict[str, Any]:
    try:
        return await asyncio.wait_for(asyncio.to_thread(run_probe, request.url, request.use_youtube_cookies), timeout=PROBE_TIMEOUT_SECONDS)
    except TimeoutError as exc:
        raise HTTPException(
            status_code=504,
            detail="Checking the link timed out. Check the Pi's internet and DNS connection, then try again.",
        ) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.get("/api/config")
async def get_config() -> dict[str, bool]:
    return {"youtube_cookies_available": youtube_cookies_available()}


@app.post("/api/jobs")
async def create_job(request: JobRequest) -> dict[str, str]:
    if request.use_youtube_cookies:
        try:
            validate_youtube_cookies()
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
    job = Job(
        id=uuid.uuid4().hex,
        url=request.url,
        media_format=request.format,
        session_id=request.session_id or uuid.uuid4().hex,
        playlist_items=[item for item in request.playlist_items if item > 0],
        audio_quality=request.audio_quality,
        embed_cover=request.embed_cover,
        video_quality=request.video_quality,
        use_youtube_cookies=request.use_youtube_cookies,
    )
    jobs[job.id] = job
    asyncio.create_task(asyncio.to_thread(run_job, job))
    return {"job_id": job.id}


@app.get("/api/jobs/{job_id}")
async def get_job(job_id: str) -> dict[str, Any]:
    job = jobs.get(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found or already deleted.")
    with job.state_lock:
        return {
            "job_id": job.id,
            "status": job.status,
            "progress": round(job.progress, 1),
            "message": job.message,
            "error": job.error,
            "download_ready": job.status == "done" and job.output_path is not None,
            "download_name": job.download_name,
            "download_size_bytes": job.output_path.stat().st_size if job.output_path and job.output_path.exists() else None,
            "unpacked_size_bytes": unpacked_zip_size(job.output_path) if job.output_path and job.output_path.exists() else None,
            "tracks": [track_response(track) for track in job.tracks],
            "lookup_active": job.status == "running" and not job.lookup_finished and not job.lookup_cancel.is_set(),
            "download_complete": job.download_finished,
            "expected_tracks": len(set(job.playlist_items)) if job.playlist_items else max(1, len(job.tracks)),
            "created_at": job.created_at.isoformat(),
            "expires_after_minutes": JOB_TTL_MINUTES,
        }


@app.patch("/api/jobs/{job_id}/tracks/{track_id}")
async def update_track(job_id: str, track_id: str, request: TrackUpdateRequest) -> dict[str, Any]:
    job = jobs.get(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found or already deleted.")
    with job.state_lock:
        if job.status != "review":
            raise HTTPException(status_code=409, detail="Wait until download and metadata lookup finish.")
        track = next((item for item in job.tracks if item.id == track_id), None)
        if track is None:
            raise HTTPException(status_code=404, detail="Track not found.")
        if track.metadata != request.metadata:
            track.metadata = request.metadata
            track.metadata_revision += 1
            track.lookup_status = "edited"
            track.status = "edited"
            track.message = "Metadata edited manually."
            queue_track_tag(job, track)
        return {"track": track_response(track)}


@app.post("/api/jobs/{job_id}/finalize")
async def finalize(job_id: str, request: FinalizeRequest) -> dict[str, str]:
    job = jobs.get(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found or already deleted.")
    with job.state_lock:
        if job.status != "review":
            raise HTTPException(status_code=409, detail="Job is not ready for metadata review.")
        job.lookup_cancel.set()
        submitted = {track.id: track.metadata for track in request.tracks}
        for track in job.tracks:
            if track.id in submitted and track.metadata != submitted[track.id]:
                track.metadata = submitted[track.id]
                track.metadata_revision += 1
                queue_track_tag(job, track)
        touch(job, status="finalizing", progress=92, message="Finalizing download")
    asyncio.create_task(asyncio.to_thread(finalize_job, job))
    return {"job_id": job.id}


@app.get("/api/history")
async def get_history(session_id: str = "") -> dict[str, Any]:
    history_items = []
    for job in jobs.values():
        if session_id and job.session_id != session_id:
            continue
        if job.status != "done" or not job.output_path or not job.output_path.exists():
            continue
        expires_at = job.updated_at + timedelta(minutes=JOB_TTL_MINUTES)
        remaining_seconds = max(0, int((expires_at - datetime.now(timezone.utc)).total_seconds()))
        history_items.append(
            {
                "job_id": job.id,
                "download_name": job.download_name or job.output_path.name,
                "download_url": f"/api/jobs/{job.id}/download",
                "download_size_bytes": job.output_path.stat().st_size,
                "media_format": job.media_format,
                "is_playlist": job.is_playlist or bool(job.playlist_items),
                "updated_at": job.updated_at.isoformat(),
                "expires_at": expires_at.isoformat(),
                "remaining_seconds": remaining_seconds,
            }
        )
    history_items.sort(key=lambda item: item["updated_at"], reverse=True)
    return {"items": history_items}


@app.get("/api/jobs/{job_id}/download")
async def download_job(job_id: str) -> FileResponse:
    job = jobs.get(job_id)
    if not job or job.status != "done" or not job.output_path or not job.output_path.exists():
        raise HTTPException(status_code=404, detail="Download is unavailable.")
    return FileResponse(job.output_path, filename=job.download_name or job.output_path.name)


app.mount("/", StaticFiles(directory=STATIC_DIR, html=True), name="static")
