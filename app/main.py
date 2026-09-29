import asyncio
import json
import os
import re
import shutil
import subprocess
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
import zipfile
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
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


ROOT_DIR = Path(__file__).resolve().parent
STATIC_DIR = ROOT_DIR / "static"
DATA_DIR = Path(os.getenv("YTMPX_DATA_DIR", "/data")).resolve()
JOBS_DIR = DATA_DIR / "jobs"
MAX_PLAYLIST_ITEMS = int(os.getenv("YTMPX_MAX_PLAYLIST_ITEMS", "100"))
JOB_TTL_MINUTES = int(os.getenv("YTMPX_JOB_TTL_MINUTES", str(int(os.getenv("YTMPX_JOB_TTL_HOURS", "2")) * 60)))
PROBE_TIMEOUT_SECONDS = int(os.getenv("YTMPX_PROBE_TIMEOUT_SECONDS", "75"))
YTDLP_SOCKET_TIMEOUT_SECONDS = int(os.getenv("YTMPX_YTDLP_SOCKET_TIMEOUT_SECONDS", "30"))
ACOUSTID_API_KEY = os.getenv("YTMPX_ACOUSTID_API_KEY") or os.getenv("ACOUSTID_API_KEY") or ""
ACOUSTID_MIN_SCORE = float(os.getenv("YTMPX_ACOUSTID_MIN_SCORE", "0.78"))
METADATA_LOOKUP_TIMEOUT_SECONDS = int(os.getenv("YTMPX_METADATA_LOOKUP_TIMEOUT_SECONDS", "20"))
MUSICBRAINZ_USER_AGENT = os.getenv("YTMPX_MUSICBRAINZ_USER_AGENT", "yt2mpX/1.0 ( local-home-use )")
MAX_DOWNLOAD_STEM_LENGTH = 180

musicbrainz_lock = threading.Lock()
last_musicbrainz_request_at = 0.0


class ProbeRequest(BaseModel):
    url: str = Field(min_length=1)


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


class FinalizeTrack(BaseModel):
    id: str
    metadata: Metadata


class FinalizeRequest(BaseModel):
    tracks: list[FinalizeTrack] = Field(default_factory=list)


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


app = FastAPI(title="yt2mpX", version="1.0.0")
jobs: dict[str, Job] = {}


def touch(job: Job, *, status: str | None = None, progress: float | None = None, message: str | None = None) -> None:
    if status is not None:
        job.status = status
    if progress is not None:
        job.progress = max(0, min(progress, 100))
    if message is not None:
        job.message = message
    job.updated_at = datetime.now(timezone.utc)


def run_probe(url: str) -> dict[str, Any]:
    opts = {
        "quiet": True,
        "no_warnings": True,
        "extract_flat": "in_playlist",
        "playlistend": MAX_PLAYLIST_ITEMS,
        "socket_timeout": YTDLP_SOCKET_TIMEOUT_SECONDS,
    }
    with YoutubeDL(opts) as ydl:
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
        "source_cover_url": track.source_cover_url,
        "metadata": metadata_dict(track.metadata),
        "confidence": round(track.confidence, 3),
        "status": track.status,
        "message": track.message,
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


def json_http_error(exc: urllib.error.HTTPError) -> RuntimeError:
    body = exc.read().decode("utf-8", errors="replace")
    try:
        data = json.loads(body)
        error = data.get("error") or {}
        message = first_text(error.get("message"), data.get("message"), body)
    except json.JSONDecodeError:
        message = body.strip() or exc.reason
    return RuntimeError(f"HTTP {exc.code}: {message}")


def fetch_request_json(request: urllib.request.Request) -> dict[str, Any]:
    try:
        with urllib.request.urlopen(request, timeout=METADATA_LOOKUP_TIMEOUT_SECONDS) as response:
            return json.loads(response.read().decode("utf-8"))
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
        error = data.get("error") or {}
        raise RuntimeError(first_text(error.get("message"), "AcoustID rejected the lookup."))
    results = data.get("results") or []
    best_acoustid_score = 0.0
    best_rank = 0.0
    best_recording_id = ""
    for result in results:
        try:
            score = float(result.get("score") or 0)
        except (TypeError, ValueError):
            score = 0.0
        for recording in result.get("recordings") or []:
            recording_id = first_text(recording.get("id"))
            rank = candidate_rank(score, recording, fallback)
            if recording_id and rank > best_rank:
                best_rank = rank
                best_acoustid_score = score
                best_recording_id = recording_id
    if not best_recording_id:
        raise RuntimeError("AcoustID found no matching MusicBrainz recording.")
    return best_acoustid_score, best_recording_id


def musicbrainz_json(path: str, params: dict[str, str] | None = None) -> dict[str, Any]:
    global last_musicbrainz_request_at
    params = {**(params or {}), "fmt": "json"}
    url = f"https://musicbrainz.org/ws/2/{path}?{urllib.parse.urlencode(params)}"
    with musicbrainz_lock:
        wait_seconds = 1.05 - (time.monotonic() - last_musicbrainz_request_at)
        if wait_seconds > 0:
            time.sleep(wait_seconds)
        data = fetch_json(url, user_agent=MUSICBRAINZ_USER_AGENT)
        last_musicbrainz_request_at = time.monotonic()
    return data


def artist_credit_name(entity: dict[str, Any]) -> str:
    names = []
    for credit in entity.get("artist-credit") or []:
        if isinstance(credit, dict):
            artist = credit.get("artist") or {}
            name = first_text(credit.get("name"), artist.get("name"))
            if name:
                names.append(name)
    return ", ".join(names)


def first_release(recording: dict[str, Any]) -> dict[str, Any]:
    releases = recording.get("releases") or []
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
        disc_number = str(medium.get("position") or "")
        for track in medium.get("tracks") or []:
            recording = track.get("recording") or {}
            if recording.get("id") == recording_id:
                return str(track.get("number") or track.get("position") or ""), disc_number
    return "", ""


def musicbrainz_metadata(recording_id: str, fallback: Metadata) -> Metadata:
    recording = musicbrainz_json(
        f"recording/{recording_id}",
        {"inc": "artists+releases+release-groups+media+isrcs"},
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


def identify_track(path: Path, fallback: Metadata) -> tuple[Metadata, float, str, str]:
    if not ACOUSTID_API_KEY:
        return fallback, 0, "fallback", "No AcoustID API key is configured."
    try:
        duration, fingerprint = fpcalc(path)
        score, recording_id = acoustid_lookup(duration, fingerprint, fallback)
        if score < ACOUSTID_MIN_SCORE:
            return fallback, score, "fallback", "The AcoustID match was too uncertain."
        metadata = musicbrainz_metadata(recording_id, fallback)
        return metadata, score, "matched", "MusicBrainz match found."
    except FileNotFoundError:
        return fallback, 0, "fallback", "fpcalc is not installed."
    except Exception as exc:
        message = str(exc)
        if "invalid api key" in message.casefold():
            message = "Invalid AcoustID API key. Use the Application API Key, not a user key."
        return fallback, 0, "fallback", message


def progress_hook(job: Job):
    def hook(data: dict[str, Any]) -> None:
        status = data.get("status")
        if status == "downloading":
            raw_percent = (data.get("_percent_str") or "").strip().replace("%", "")
            try:
                percent = float(raw_percent)
                touch(job, status="running", progress=1 + percent * 0.58, message="Downloading")
            except ValueError:
                touch(job, status="running", message="Downloading")
        elif status == "finished":
            touch(job, status="running", progress=max(job.progress, 62), message="Converting file")

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


def ydl_options(job: Job, work_dir: Path) -> dict[str, Any]:
    is_playlist_selection = bool(job.playlist_items)
    is_minimal_audio = job.media_format == "mp3" and is_minimal_audio_quality(job.audio_quality)
    should_embed_cover = should_write_covers(job)
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
    if is_playlist_selection and should_embed_cover:
        common["writethumbnail"] = True
    if job.media_format == "mp3":
        postprocessors: list[dict[str, Any]] = [
            {
                "key": "FFmpegExtractAudio",
                "preferredcodec": "mp3",
                "preferredquality": mp3_quality_value(job.audio_quality),
            }
        ]
        if is_playlist_selection:
            postprocessors.append({"key": "FFmpegMetadata"})
            if should_embed_cover:
                postprocessors.append({"key": "EmbedThumbnail", "already_have_thumbnail": False})
        common.update(
            {
                "format": "worstaudio/worst" if is_minimal_audio else "bestaudio/best",
                "postprocessors": postprocessors,
            }
        )
    else:
        postprocessors = []
        if is_playlist_selection:
            postprocessors.extend(
                [
                    {"key": "FFmpegMetadata"},
                    {"key": "EmbedThumbnail", "already_have_thumbnail": False},
                ]
            )
        common.update(
            {
                "format": mp4_format_selector(job.video_quality),
                "merge_output_format": "mp4",
            }
        )
        if postprocessors:
            common["postprocessors"] = postprocessors
    return common


def download_cover(url: str, work_dir: Path) -> Path | None:
    if not url:
        return None
    cover_path = work_dir / "cover"
    try:
        request = urllib.request.Request(url, headers={"User-Agent": "yt2mpX/1.0"})
        with urllib.request.urlopen(request, timeout=20) as response:
            content_type = response.headers.get("content-type", "")
            suffix = ".jpg"
            if "png" in content_type:
                suffix = ".png"
            final_path = cover_path.with_suffix(suffix)
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


def cover_for_track(track: Track, work_dir: Path) -> Path | None:
    cover_url = track.metadata.cover_url.strip()
    if not cover_url:
        return None
    cover_path = download_cover(cover_url, work_dir)
    if cover_path:
        return cover_path
    fallback_cover_url = track.source_cover_url.strip()
    if fallback_cover_url and fallback_cover_url != cover_url:
        return download_cover(fallback_cover_url, work_dir)
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
            )
        )
    return tracks


def run_job(job: Job) -> None:
    job_dir = JOBS_DIR / job.id
    work_dir = job_dir / "work"
    work_dir.mkdir(parents=True, exist_ok=True)

    try:
        touch(job, status="running", progress=1, message="Checking link")
        with YoutubeDL(ydl_options(job, work_dir)) as ydl:
            info = ydl.extract_info(job.url, download=True)
            job.is_playlist = bool(info.get("entries"))
            job.collection_title = first_text(info.get("title"), "yt2mpX-download")

        files = media_files(work_dir, job.media_format)
        if not files:
            raise RuntimeError("No output file was created.")
        file_positions = [playlist_position_from_name(path) for path in files]
        if job.playlist_items:
            files = deduplicate_playlist_filenames(files)

        job.tracks = build_tracks(job, info, files, file_positions)
        total_tracks = len(job.tracks)
        for index, track in enumerate(job.tracks, start=1):
            progress = 68 + ((index - 1) / max(total_tracks, 1)) * 20
            touch(job, progress=progress, message=f"Checking metadata {index}/{total_tracks}")
            metadata, confidence, status, message = identify_track(track.path, track.metadata)
            track.metadata = metadata
            track.confidence = confidence
            track.status = status
            track.message = message

        touch(job, status="review", progress=91, message="Review metadata")
    except Exception as exc:
        job.error = str(exc)
        touch(job, status="failed", progress=0, message="Failed")


def finalize_job(job: Job) -> None:
    job_dir = JOBS_DIR / job.id
    work_dir = job_dir / "work"
    try:
        if not job.tracks:
            raise RuntimeError("No tracks were found to finalize.")

        total_tracks = len(job.tracks)
        for index, track in enumerate(job.tracks, start=1):
            progress = 93 + ((index - 1) / max(total_tracks, 1)) * 4
            touch(job, status="finalizing", progress=progress, message=f"Writing tags {index}/{total_tracks}")
            cover_path = cover_for_track(track, work_dir) if should_write_covers(job) else None
            if job.media_format == "mp3":
                apply_mp3_metadata(track.path, track.metadata, cover_path)
            else:
                apply_mp4_metadata(track.path, track.metadata, cover_path)

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


async def cleanup_loop() -> None:
    while True:
        await asyncio.sleep(5 * 60)
        cutoff = datetime.now(timezone.utc) - timedelta(minutes=JOB_TTL_MINUTES)
        for job_id, job in list(jobs.items()):
            if job.updated_at < cutoff and job.status in {"done", "failed", "review"}:
                shutil.rmtree(JOBS_DIR / job_id, ignore_errors=True)
                jobs.pop(job_id, None)


@app.on_event("startup")
async def startup() -> None:
    JOBS_DIR.mkdir(parents=True, exist_ok=True)
    asyncio.create_task(cleanup_loop())


@app.post("/api/probe")
async def probe(request: ProbeRequest) -> dict[str, Any]:
    try:
        return await asyncio.wait_for(asyncio.to_thread(run_probe, request.url), timeout=PROBE_TIMEOUT_SECONDS)
    except TimeoutError as exc:
        raise HTTPException(
            status_code=504,
            detail="Checking the link timed out. Check the Pi's internet and DNS connection, then try again.",
        ) from exc
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.post("/api/jobs")
async def create_job(request: JobRequest) -> dict[str, str]:
    job = Job(
        id=uuid.uuid4().hex,
        url=request.url,
        media_format=request.format,
        session_id=request.session_id or uuid.uuid4().hex,
        playlist_items=[item for item in request.playlist_items if item > 0],
        audio_quality=request.audio_quality,
        embed_cover=request.embed_cover,
        video_quality=request.video_quality,
    )
    jobs[job.id] = job
    asyncio.create_task(asyncio.to_thread(run_job, job))
    return {"job_id": job.id}


@app.get("/api/jobs/{job_id}")
async def get_job(job_id: str) -> dict[str, Any]:
    job = jobs.get(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found or already deleted.")
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
        "created_at": job.created_at.isoformat(),
        "expires_after_minutes": JOB_TTL_MINUTES,
    }


@app.post("/api/jobs/{job_id}/finalize")
async def finalize(job_id: str, request: FinalizeRequest) -> dict[str, str]:
    job = jobs.get(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found or already deleted.")
    if job.status != "review":
        raise HTTPException(status_code=409, detail="Job is not ready for metadata review.")

    submitted = {track.id: track.metadata for track in request.tracks}
    for track in job.tracks:
        if track.id in submitted:
            track.metadata = submitted[track.id]

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
