import asyncio
import os
import re
import shutil
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
from mutagen.mp4 import MP4, MP4Cover
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
MAX_DOWNLOAD_STEM_LENGTH = 180


class ProbeRequest(BaseModel):
    url: str = Field(min_length=1)


class Metadata(BaseModel):
    title: str = ""
    artist: str = ""
    album: str = ""
    cover_url: str = ""


class JobRequest(BaseModel):
    url: str = Field(min_length=1)
    format: Literal["mp3", "mp4"]
    metadata: Metadata = Field(default_factory=Metadata)
    session_id: str = Field(default="")
    playlist_items: list[int] = Field(default_factory=list)
    audio_quality: Literal["high", "medium", "small", "minimal"] = "medium"
    video_quality: Literal["360", "720", "1080", "best_compatible"] = "best_compatible"


@dataclass
class Job:
    id: str
    url: str
    media_format: str
    metadata: Metadata
    session_id: str
    playlist_items: list[int] = field(default_factory=list)
    audio_quality: str = "medium"
    video_quality: str = "best_compatible"
    status: str = "queued"
    progress: float = 0
    message: str = "Wartet auf Start"
    error: str = ""
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    updated_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    output_path: Path | None = None
    download_name: str = ""
    is_playlist: bool = False


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
                "title": entry.get("title") or "Unbenannter Eintrag",
                "url": entry.get("url") or entry.get("webpage_url"),
                "duration": entry.get("duration"),
                "uploader": entry.get("uploader") or entry.get("channel"),
            }
        )

    title = info.get("title") or "Unbenannt"
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


def progress_hook(job: Job):
    def hook(data: dict[str, Any]) -> None:
        status = data.get("status")
        if status == "downloading":
            raw_percent = (data.get("_percent_str") or "").strip().replace("%", "")
            try:
                percent = float(raw_percent)
                touch(job, status="running", progress=percent * 0.8, message="Download laeuft")
            except ValueError:
                touch(job, status="running", message="Download laeuft")
        elif status == "finished":
            touch(job, status="running", progress=max(job.progress, 85), message="Konvertiere Datei")

    return hook


def mp3_quality_value(quality: str) -> str:
    return {
        "high": "0",
        "medium": "192",
        "small": "128",
        "minimal": "32",
    }.get(quality, "0")


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
    is_minimal_audio = job.media_format == "mp3" and job.audio_quality == "minimal"
    should_embed_cover = not is_minimal_audio
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


def apply_mp3_metadata(path: Path, metadata: Metadata, cover_path: Path | None) -> None:
    audio = MP3(path, ID3=ID3)
    if audio.tags is None:
        audio.add_tags()
    audio.save()

    easy = EasyID3(path)
    if metadata.title:
        easy["title"] = metadata.title
    if metadata.artist:
        easy["artist"] = metadata.artist
    if metadata.album:
        easy["album"] = metadata.album
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
    video.pop("covr", None)
    if cover_path:
        image_format = MP4Cover.FORMAT_PNG if cover_path.suffix.lower() == ".png" else MP4Cover.FORMAT_JPEG
        video["covr"] = [MP4Cover(cover_path.read_bytes(), imageformat=image_format)]
    video.save()


def media_files(work_dir: Path, media_format: str) -> list[Path]:
    files = [path for path in work_dir.iterdir() if path.is_file() and path.suffix.lower() == f".{media_format}"]
    return sorted(files, key=lambda path: path.stat().st_mtime_ns)


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


def safe_download_name(job: Job, files: list[Path]) -> str:
    base = job.metadata.album.strip() if len(files) != 1 or job.playlist_items else ""
    if not base:
        base = metadata_download_stem(job.metadata)
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


def run_job(job: Job) -> None:
    job_dir = JOBS_DIR / job.id
    work_dir = job_dir / "work"
    work_dir.mkdir(parents=True, exist_ok=True)

    try:
        touch(job, status="running", progress=1, message="Pruefe Link")
        with YoutubeDL(ydl_options(job, work_dir)) as ydl:
            info = ydl.extract_info(job.url, download=True)
            job.is_playlist = bool(info.get("entries"))

        files = media_files(work_dir, job.media_format)
        if not files:
            raise RuntimeError("Keine Ausgabedatei erzeugt.")
        if job.playlist_items:
            files = deduplicate_playlist_filenames(files)

        touch(job, progress=90, message="Schreibe Metadaten")
        should_embed_cover = not (job.media_format == "mp3" and job.audio_quality == "minimal")
        cover_path = (
            download_cover(job.metadata.cover_url, work_dir)
            if should_embed_cover and len(files) == 1 and not job.playlist_items
            else None
        )
        if len(files) == 1 and not job.playlist_items:
            if job.media_format == "mp3":
                apply_mp3_metadata(files[0], job.metadata, cover_path)
            else:
                apply_mp4_metadata(files[0], job.metadata, cover_path)

        job.download_name = safe_download_name(job, files)
        if len(files) == 1 and not job.playlist_items:
            job.output_path = rename_output_file(files[0], job.download_name)
        else:
            zip_path = job_dir / job.download_name
            build_zip(files, zip_path)
            job.output_path = zip_path

        touch(job, status="done", progress=100, message="Fertig")
    except Exception as exc:
        job.error = str(exc)
        touch(job, status="failed", progress=0, message="Fehlgeschlagen")


async def cleanup_loop() -> None:
    while True:
        await asyncio.sleep(5 * 60)
        cutoff = datetime.now(timezone.utc) - timedelta(minutes=JOB_TTL_MINUTES)
        for job_id, job in list(jobs.items()):
            if job.updated_at < cutoff and job.status in {"done", "failed"}:
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
            detail="Link-Pruefung hat zu lange gedauert. Pruefe Internet/DNS auf dem Pi oder versuche es spaeter erneut.",
        ) from exc
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.post("/api/jobs")
async def create_job(request: JobRequest) -> dict[str, str]:
    job = Job(
        id=uuid.uuid4().hex,
        url=request.url,
        media_format=request.format,
        metadata=request.metadata,
        session_id=request.session_id or uuid.uuid4().hex,
        playlist_items=[item for item in request.playlist_items if item > 0],
        audio_quality=request.audio_quality,
        video_quality=request.video_quality,
    )
    jobs[job.id] = job
    asyncio.create_task(asyncio.to_thread(run_job, job))
    return {"job_id": job.id}


@app.get("/api/jobs/{job_id}")
async def get_job(job_id: str) -> dict[str, Any]:
    job = jobs.get(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job nicht gefunden oder bereits geloescht.")
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
        "created_at": job.created_at.isoformat(),
        "expires_after_minutes": JOB_TTL_MINUTES,
    }


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
        raise HTTPException(status_code=404, detail="Download nicht verfuegbar.")
    return FileResponse(job.output_path, filename=job.download_name or job.output_path.name)


app.mount("/", StaticFiles(directory=STATIC_DIR, html=True), name="static")
