#!/usr/bin/env python3
"""Local, dependency-light music library and VLC playback controller."""

from __future__ import annotations

import json
import hashlib
import os
import random
import re
import signal
import subprocess
import time
import urllib.error
import urllib.parse
import urllib.request
from difflib import SequenceMatcher
from bisect import bisect_right
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Callable, Iterable


SUPPORTED_EXTENSIONS = frozenset({".mp3", ".flac", ".wav", ".m4a", ".aac", ".ogg", ".opus"})
SUPPORTED_ARTWORK_EXTENSIONS = (".jpg", ".jpeg", ".png", ".webp")
PLAYBACK_MODE_LIST_LOOP = "list_loop"
PLAYBACK_MODE_SINGLE_REPEAT = "single_repeat"
PLAYBACK_MODE_SHUFFLE = "shuffle"
PLAYBACK_MODES = (
    PLAYBACK_MODE_LIST_LOOP,
    PLAYBACK_MODE_SINGLE_REPEAT,
    PLAYBACK_MODE_SHUFFLE,
)
LRCLIB_API_BASE = "https://lrclib.net/api"
LRCLIB_USER_AGENT = (
    "RiverBank-Assistant/0.17.0 "
    "(https://github.com/riverbank-tech/riverbank_assistant)"
)
LRC_TIMESTAMP_RE = re.compile(
    r"\[(?:(?P<hours>\d{1,2}):)?(?P<minutes>\d{1,3}):"
    r"(?P<seconds>\d{1,2})(?:[.:](?P<fraction>\d{1,3}))?\]"
)
LRC_OFFSET_RE = re.compile(r"\[offset\s*:\s*(?P<milliseconds>[+-]?\d+)\]", re.IGNORECASE)


@dataclass(frozen=True)
class MusicTrack:
    path: str
    title: str
    artist: str = "未知艺术家"
    album: str = ""
    duration_seconds: float = 0.0
    artwork_path: str = ""

    def as_dict(self) -> dict:
        return asdict(self)


@dataclass(frozen=True)
class LyricLine:
    timestamp_seconds: float
    text: str

    def as_dict(self) -> dict:
        return asdict(self)


@dataclass(frozen=True)
class LyricsFetchResult:
    track_path: str
    status: str
    lyrics_path: str = ""
    provider: str = "lrclib"
    error: str = ""

    @property
    def found(self) -> bool:
        return self.status == "found" and bool(self.lyrics_path)


def sidecar_lyrics_path(track_or_path: MusicTrack | Path | str) -> Path | None:
    """Return a same-basename LRC sidecar, accepting a case-insensitive suffix."""
    source = track_or_path.path if isinstance(track_or_path, MusicTrack) else track_or_path
    audio_path = Path(source)
    expected = audio_path.with_suffix(".lrc")
    try:
        target_name = expected.name.casefold()
        matched = next(
            (
                candidate
                for candidate in audio_path.parent.iterdir()
                if candidate.is_file() and candidate.name.casefold() == target_name
            ),
            None,
        )
        if matched is not None:
            return matched
    except OSError:
        pass
    return expected if expected.is_file() else None


def _fraction_seconds(value: str | None) -> float:
    if not value:
        return 0.0
    return int(value) / (10 ** len(value))


def parse_lrc(path: Path) -> list[LyricLine]:
    """Parse common enhanced-LRC timestamps without retaining metadata tags."""
    try:
        contents = Path(path).read_text(encoding="utf-8-sig", errors="replace")
    except OSError:
        return []
    offset_match = LRC_OFFSET_RE.search(contents)
    offset_seconds = (
        int(offset_match.group("milliseconds")) / 1000.0 if offset_match else 0.0
    )
    timed_lines: dict[float, str] = {}
    for raw_line in contents.splitlines():
        matches = list(LRC_TIMESTAMP_RE.finditer(raw_line))
        if not matches:
            continue
        text = LRC_TIMESTAMP_RE.sub("", raw_line).strip()
        if not text:
            continue
        for match in matches:
            timestamp = (
                int(match.group("hours") or 0) * 3600
                + int(match.group("minutes")) * 60
                + int(match.group("seconds"))
                + _fraction_seconds(match.group("fraction"))
                + offset_seconds
            )
            timed_lines[round(max(0.0, timestamp), 3)] = text
    return [
        LyricLine(timestamp_seconds=timestamp, text=text)
        for timestamp, text in sorted(timed_lines.items())
    ]


def lyric_index_at(lines: list[LyricLine], elapsed_seconds: float) -> int:
    """Find the line active at one playback position, or -1 before the first line."""
    return bisect_right(
        [line.timestamp_seconds for line in lines],
        max(0.0, float(elapsed_seconds)),
    ) - 1


def _lyrics_match_text(value: object) -> str:
    return "".join(character.casefold() for character in str(value or "") if character.isalnum())


def _lyrics_candidate_score(track: MusicTrack, payload: dict) -> float:
    title = SequenceMatcher(
        None,
        _lyrics_match_text(track.title),
        _lyrics_match_text(payload.get("trackName") or payload.get("name")),
    ).ratio()
    artist = SequenceMatcher(
        None,
        _lyrics_match_text(track.artist),
        _lyrics_match_text(payload.get("artistName")),
    ).ratio()
    try:
        duration_delta = abs(float(payload.get("duration", 0.0)) - track.duration_seconds)
    except (TypeError, ValueError):
        duration_delta = 999.0
    duration = 1.0 if duration_delta <= 2.0 else 0.7 if duration_delta <= 5.0 else 0.0
    return title * 0.62 + artist * 0.23 + duration * 0.15


def _lrclib_request_json(
    endpoint: str,
    params: dict[str, object],
    *,
    timeout: float,
    opener: Callable | None,
) -> object:
    query = urllib.parse.urlencode(
        {key: value for key, value in params.items() if value not in (None, "")}
    )
    request = urllib.request.Request(
        f"{LRCLIB_API_BASE}{endpoint}?{query}",
        headers={"User-Agent": LRCLIB_USER_AGENT, "Accept": "application/json"},
    )
    open_url = opener or urllib.request.urlopen
    with open_url(request, timeout=timeout) as response:
        return json.loads(response.read().decode("utf-8"))


def fetch_lrclib_lyrics(
    track: MusicTrack,
    *,
    timeout: float = 8.0,
    opener: Callable | None = None,
) -> LyricsFetchResult:
    """Fetch synchronized lyrics for one playing track and cache beside the audio."""
    destination = Path(track.path).with_suffix(".lrc")
    if destination.is_file():
        return LyricsFetchResult(str(track.path), "local", str(destination), "local")
    signature = {
        "track_name": track.title,
        "artist_name": track.artist,
        "album_name": track.album,
        "duration": max(0, round(track.duration_seconds)),
    }
    payload: dict | None = None
    try:
        exact = _lrclib_request_json(
            "/get",
            signature,
            timeout=timeout,
            opener=opener,
        )
        if isinstance(exact, dict):
            payload = exact
    except urllib.error.HTTPError as exc:
        if exc.code == 429:
            return LyricsFetchResult(str(track.path), "rate_limited", error="HTTP 429")
        if exc.code != 404:
            return LyricsFetchResult(str(track.path), "error", error=f"HTTP {exc.code}")
    except (OSError, TimeoutError, ValueError, json.JSONDecodeError) as exc:
        return LyricsFetchResult(str(track.path), "error", error=str(exc))

    if payload is None:
        time.sleep(0.3)
        try:
            search = _lrclib_request_json(
                "/search",
                {
                    "track_name": track.title,
                    "artist_name": track.artist,
                },
                timeout=timeout,
                opener=opener,
            )
        except urllib.error.HTTPError as exc:
            status = "rate_limited" if exc.code == 429 else "error"
            return LyricsFetchResult(str(track.path), status, error=f"HTTP {exc.code}")
        except (OSError, TimeoutError, ValueError, json.JSONDecodeError) as exc:
            return LyricsFetchResult(str(track.path), "error", error=str(exc))
        candidates = [
            candidate
            for candidate in search
            if isinstance(candidate, dict) and str(candidate.get("syncedLyrics") or "").strip()
        ] if isinstance(search, list) else []
        ranked = sorted(
            ((_lyrics_candidate_score(track, candidate), candidate) for candidate in candidates),
            key=lambda item: item[0],
            reverse=True,
        )
        if ranked and ranked[0][0] >= 0.72:
            payload = ranked[0][1]

    if payload is None:
        return LyricsFetchResult(str(track.path), "not_found")
    if bool(payload.get("instrumental")):
        return LyricsFetchResult(str(track.path), "instrumental")
    synced = str(payload.get("syncedLyrics") or "").strip()
    if not synced:
        return LyricsFetchResult(str(track.path), "not_found")
    try:
        temporary = destination.with_suffix(".lrc.tmp")
        temporary.write_text(synced + "\n", encoding="utf-8")
        os.replace(temporary, destination)
    except OSError as exc:
        return LyricsFetchResult(str(track.path), "error", error=str(exc))
    return LyricsFetchResult(str(track.path), "found", str(destination))


def _clean_tag(value: object, fallback: str = "") -> str:
    text = " ".join(str(value or "").strip().split())
    return text or fallback


def sidecar_artwork_path(audio_path: Path) -> Path | None:
    """Find track-specific artwork first, then conventional album artwork names."""
    try:
        files = {
            candidate.name.casefold(): candidate
            for candidate in audio_path.parent.iterdir()
            if candidate.is_file()
        }
    except OSError:
        return None
    for suffix in SUPPORTED_ARTWORK_EXTENSIONS:
        candidate = files.get(f"{audio_path.stem}{suffix}".casefold())
        if candidate is not None:
            return candidate
    for stem in ("cover", "folder", "front", "album"):
        for suffix in SUPPORTED_ARTWORK_EXTENSIONS:
            candidate = files.get(f"{stem}{suffix}".casefold())
            if candidate is not None:
                return candidate
    return None


def _embedded_artwork_path(
    audio_path: Path,
    artwork_cache_dir: Path | None,
    ffmpeg_command: str,
) -> Path | None:
    if artwork_cache_dir is None:
        return None
    try:
        stat = audio_path.stat()
        cache_key = hashlib.sha256(
            f"{audio_path.resolve()}:{stat.st_size}:{stat.st_mtime_ns}".encode("utf-8")
        ).hexdigest()[:24]
        artwork_cache_dir.mkdir(parents=True, exist_ok=True)
        output = artwork_cache_dir / f"{cache_key}.png"
        if output.is_file() and output.stat().st_size > 0:
            return output
        temporary = artwork_cache_dir / f"{cache_key}.tmp.png"
        result = subprocess.run(
            [
                ffmpeg_command,
                "-v",
                "error",
                "-i",
                str(audio_path),
                "-map",
                "0:v:0",
                "-frames:v",
                "1",
                "-y",
                str(temporary),
            ],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=8.0,
            check=False,
        )
        if result.returncode == 0 and temporary.is_file() and temporary.stat().st_size > 0:
            os.replace(temporary, output)
            return output
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass
    except (OSError, subprocess.SubprocessError):
        return None
    return None


def _probe_track(
    path: Path,
    ffprobe_command: str,
    artwork_cache_dir: Path | None = None,
    ffmpeg_command: str = "/usr/bin/ffmpeg",
) -> MusicTrack:
    fallback_title = _clean_tag(path.stem.replace("_", " ").replace("-", " "), path.stem)
    try:
        result = subprocess.run(
            [
                ffprobe_command,
                "-v",
                "quiet",
                "-print_format",
                "json",
                "-show_format",
                "-show_streams",
                str(path),
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            timeout=4.0,
            check=False,
        )
        payload = json.loads(result.stdout or "{}") if result.returncode == 0 else {}
        file_format = payload.get("format", {}) if isinstance(payload, dict) else {}
        tags = file_format.get("tags", {}) if isinstance(file_format, dict) else {}
        normalized_tags = {
            str(key).lower(): value for key, value in tags.items()
        } if isinstance(tags, dict) else {}
        streams = payload.get("streams", []) if isinstance(payload, dict) else []
        has_embedded_artwork = False
        if isinstance(streams, list):
            audio_stream = next(
                (
                    stream
                    for stream in streams
                    if isinstance(stream, dict)
                    and str(stream.get("codec_type", "audio")) == "audio"
                ),
                None,
            )
            stream_tags = (
                audio_stream.get("tags", {})
                if isinstance(audio_stream, dict)
                else {}
            )
            if isinstance(stream_tags, dict):
                normalized_tags.update(
                    {str(key).lower(): value for key, value in stream_tags.items()}
                )
            has_embedded_artwork = any(
                isinstance(stream, dict)
                and str(stream.get("codec_type", "")) == "video"
                and bool((stream.get("disposition") or {}).get("attached_pic", 0))
                for stream in streams
            )
        try:
            duration = max(0.0, float(file_format.get("duration", 0.0)))
        except (TypeError, ValueError):
            duration = 0.0
        artwork = sidecar_artwork_path(path)
        if artwork is None and has_embedded_artwork:
            artwork = _embedded_artwork_path(path, artwork_cache_dir, ffmpeg_command)
        return MusicTrack(
            path=str(path),
            title=_clean_tag(normalized_tags.get("title"), fallback_title),
            artist=_clean_tag(normalized_tags.get("artist"), "未知艺术家"),
            album=_clean_tag(normalized_tags.get("album")),
            duration_seconds=duration,
            artwork_path=str(artwork) if artwork is not None else "",
        )
    except (OSError, subprocess.SubprocessError, ValueError, json.JSONDecodeError):
        artwork = sidecar_artwork_path(path)
        return MusicTrack(
            path=str(path),
            title=fallback_title,
            artwork_path=str(artwork) if artwork is not None else "",
        )


def scan_music_library(
    library_dir: Path,
    ffprobe_command: str = "/usr/bin/ffprobe",
    artwork_cache_dir: Path | None = None,
    ffmpeg_command: str = "/usr/bin/ffmpeg",
) -> list[MusicTrack]:
    """Recursively scan one local library without following hidden entries."""
    root = Path(library_dir)
    if not root.is_dir():
        return []
    paths = sorted(
        (
            path
            for path in root.rglob("*")
            if path.is_file()
            and path.suffix.lower() in SUPPORTED_EXTENSIONS
            and not any(part.startswith(".") for part in path.relative_to(root).parts)
        ),
        key=lambda path: str(path).casefold(),
    )
    return [
        _probe_track(path, ffprobe_command, artwork_cache_dir, ffmpeg_command)
        for path in paths
    ]


class MusicPlayer:
    """Own one foreground VLC process and expose deterministic transport state."""

    def __init__(
        self,
        library_dir: Path,
        *,
        player_command: str = "/usr/bin/cvlc",
        clock: Callable[[], float] = time.monotonic,
        process_factory: Callable[..., subprocess.Popen] = subprocess.Popen,
    ) -> None:
        self.library_dir = Path(library_dir)
        self.player_command = player_command
        self.clock = clock
        self.process_factory = process_factory
        self.tracks: list[MusicTrack] = []
        self.index = 0
        self.status = "empty"
        self.process: subprocess.Popen | None = None
        self.started_at = 0.0
        self.paused_at = 0.0
        self.paused_total = 0.0
        self.pause_transport = "none"
        self.last_control_latency_seconds = 0.0
        self.playback_mode = PLAYBACK_MODE_LIST_LOOP
        self.last_error = ""

    @property
    def current_track(self) -> MusicTrack | None:
        if not self.tracks:
            return None
        self.index = max(0, min(self.index, len(self.tracks) - 1))
        return self.tracks[self.index]

    def set_library(self, tracks: Iterable[MusicTrack]) -> None:
        previous_path = self.current_track.path if self.current_track else None
        self.tracks = list(tracks)
        if not self.tracks:
            self.index = 0
            if self.process is None:
                self.status = "empty"
            return
        if previous_path:
            self.index = next(
                (
                    index
                    for index, track in enumerate(self.tracks)
                    if track.path == previous_path
                ),
                min(self.index, len(self.tracks) - 1),
            )
        else:
            self.index = min(self.index, len(self.tracks) - 1)
        if self.process is None:
            self.status = "stopped"

    def _send_signal(self, sig: int) -> None:
        if self.process is None:
            return
        try:
            os.killpg(self.process.pid, sig)
        except (OSError, ProcessLookupError):
            try:
                self.process.send_signal(sig)
            except (OSError, ProcessLookupError):
                pass

    def _send_player_command(self, command: str) -> bool:
        """Send a native VLC RC command without blocking the render thread."""
        process = self.process
        stdin = getattr(process, "stdin", None) if process is not None else None
        if stdin is None:
            return False
        try:
            stdin.write(f"{command}\n")
            stdin.flush()
            return True
        except (AttributeError, BrokenPipeError, OSError, ValueError):
            return False

    def _stop_process(self) -> None:
        process = self.process
        if process is None:
            return
        if self.status == "paused" and self.pause_transport == "signal":
            self._send_signal(signal.SIGCONT)
        self._send_signal(signal.SIGTERM)
        try:
            process.wait(timeout=0.35)
        except subprocess.TimeoutExpired:
            self._send_signal(signal.SIGKILL)
        self.process = None
        self.pause_transport = "none"

    def play(self, index: int | None = None) -> bool:
        if not self.tracks:
            self.status = "empty"
            return False
        if index is not None:
            self.index = int(index) % len(self.tracks)
        track = self.current_track
        if track is None:
            return False
        self._stop_process()
        command = [
            self.player_command,
            "--intf",
            "rc",
            "--rc-fake-tty",
            "--no-video",
            "--file-caching=100",
            "--play-and-exit",
            "--no-repeat",
            "--no-loop",
            "--quiet",
            "--",
            track.path,
        ]
        try:
            self.process = self.process_factory(
                command,
                stdin=subprocess.PIPE,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                start_new_session=True,
                text=True,
                bufsize=1,
            )
        except OSError as exc:
            self.process = None
            self.status = "error"
            self.last_error = str(exc)
            return False
        self.started_at = self.clock()
        self.paused_at = 0.0
        self.paused_total = 0.0
        self.pause_transport = "none"
        self.last_control_latency_seconds = 0.0
        self.status = "playing"
        self.last_error = ""
        return True

    def pause(self) -> bool:
        if self.process is None or self.status != "playing":
            return False
        control_started = time.perf_counter()
        if self._send_player_command("pause"):
            self.pause_transport = "vlc-rc"
        else:
            self._send_signal(signal.SIGSTOP)
            self.pause_transport = "signal"
        self.paused_at = self.clock()
        self.last_control_latency_seconds = max(
            0.0,
            time.perf_counter() - control_started,
        )
        self.status = "paused"
        return True

    def resume(self) -> bool:
        if self.process is None or self.status != "paused":
            return False
        now = self.clock()
        control_started = time.perf_counter()
        if self.pause_transport == "vlc-rc":
            if not self._send_player_command("pause"):
                self.last_error = "VLC resume control pipe unavailable"
                return False
        else:
            self._send_signal(signal.SIGCONT)
        self.paused_total += max(0.0, now - self.paused_at)
        self.paused_at = 0.0
        self.pause_transport = "none"
        self.last_control_latency_seconds = max(
            0.0,
            time.perf_counter() - control_started,
        )
        self.status = "playing"
        return True

    def toggle(self) -> bool:
        if self.status == "playing":
            return self.pause()
        if self.status == "paused":
            return self.resume()
        return self.play(self.index)

    def set_playback_mode(self, mode: str) -> bool:
        normalized = str(mode).strip().lower()
        if normalized not in PLAYBACK_MODES:
            return False
        self.playback_mode = normalized
        return True

    def cycle_playback_mode(self) -> str:
        current = PLAYBACK_MODES.index(self.playback_mode)
        self.playback_mode = PLAYBACK_MODES[(current + 1) % len(PLAYBACK_MODES)]
        return self.playback_mode

    def _random_index(self) -> int:
        if len(self.tracks) <= 1:
            return self.index
        return random.choice(
            [index for index in range(len(self.tracks)) if index != self.index]
        )

    def stop(self) -> None:
        self._stop_process()
        self.started_at = 0.0
        self.paused_at = 0.0
        self.paused_total = 0.0
        self.pause_transport = "none"
        self.last_control_latency_seconds = 0.0
        self.status = "stopped" if self.tracks else "empty"

    def next(self, *, automatic: bool = False) -> bool:
        if not self.tracks:
            return False
        if automatic and self.playback_mode == PLAYBACK_MODE_SINGLE_REPEAT:
            next_index = self.index
        elif self.playback_mode == PLAYBACK_MODE_SHUFFLE:
            next_index = self._random_index()
        else:
            next_index = (self.index + 1) % len(self.tracks)
        return self.play(next_index)

    def previous(self) -> bool:
        if not self.tracks:
            return False
        if self.elapsed() >= 3.0:
            return self.play(self.index)
        if self.playback_mode == PLAYBACK_MODE_SHUFFLE:
            return self.play(self._random_index())
        return self.play((self.index - 1) % len(self.tracks))

    def poll(self) -> bool:
        if self.process is None or self.status == "paused":
            return False
        if self.process.poll() is None:
            return False
        self.process = None
        self.status = "stopped"
        if len(self.tracks) > 1:
            self.next(automatic=True)
        return True

    def elapsed(self, now: float | None = None) -> float:
        if self.started_at <= 0.0 or self.status not in {"playing", "paused"}:
            return 0.0
        current = self.clock() if now is None else float(now)
        endpoint = self.paused_at if self.status == "paused" else current
        elapsed = max(0.0, endpoint - self.started_at - self.paused_total)
        track = self.current_track
        if track is not None and track.duration_seconds > 0.0:
            return min(elapsed, track.duration_seconds)
        return elapsed

    def snapshot(self, now: float | None = None) -> dict:
        track = self.current_track
        elapsed = self.elapsed(now)
        duration = track.duration_seconds if track is not None else 0.0
        return {
            "library_dir": str(self.library_dir),
            "track_count": len(self.tracks),
            "index": self.index if self.tracks else None,
            "status": self.status,
            "current_track": track.as_dict() if track is not None else None,
            "elapsed_seconds": elapsed,
            "duration_seconds": duration,
            "progress": max(0.0, min(elapsed / duration, 1.0)) if duration else 0.0,
            "backend": Path(self.player_command).name,
            "transport_control": "vlc-rc-with-signal-fallback",
            "playback_mode": self.playback_mode,
            "pause_transport": self.pause_transport,
            "last_control_latency_ms": round(
                self.last_control_latency_seconds * 1000.0,
                3,
            ),
            "last_error": self.last_error,
        }

    def shutdown(self) -> None:
        self.stop()
