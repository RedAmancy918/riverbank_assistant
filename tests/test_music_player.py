import sys
import tempfile
import unittest
import json
import urllib.error
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch


REPOSITORY_OR_APP_DIR = Path(__file__).resolve().parents[1]
APP_DIR = REPOSITORY_OR_APP_DIR / "apps" / "expression-ui"
if not APP_DIR.is_dir():
    APP_DIR = REPOSITORY_OR_APP_DIR
sys.path.insert(0, str(APP_DIR))

from music_player import (
    PLAYBACK_MODE_LIST_LOOP,
    PLAYBACK_MODE_SHUFFLE,
    PLAYBACK_MODE_SINGLE_REPEAT,
    MusicPlayer,
    MusicTrack,
    _probe_track,
    fetch_lrclib_lyrics,
    lyric_index_at,
    parse_lrc,
    scan_music_library,
    sidecar_artwork_path,
    sidecar_lyrics_path,
)


class MusicPlayerTests(unittest.TestCase):
    class JsonResponse:
        def __init__(self, payload: object) -> None:
            self.payload = payload

        def __enter__(self):
            return self

        def __exit__(self, *_args) -> None:
            return None

        def read(self) -> bytes:
            return json.dumps(self.payload).encode("utf-8")

    def test_artwork_prefers_track_specific_image_over_folder_cover(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            audio = root / "Song.ogg"
            audio.write_bytes(b"")
            folder_cover = root / "cover.jpg"
            folder_cover.write_bytes(b"folder")
            track_cover = root / "Song.PNG"
            track_cover.write_bytes(b"track")
            self.assertEqual(sidecar_artwork_path(audio), track_cover)

    def test_lrc_parser_handles_offset_multiple_timestamps_and_fraction_precision(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "sample.lrc"
            path.write_text(
                "[ar:RiverBank]\n[offset:+250]\n"
                "[00:01.50][00:03.125]第一句\n[01:02]第二句\n",
                encoding="utf-8",
            )
            lines = parse_lrc(path)
        self.assertEqual([line.text for line in lines], ["第一句", "第一句", "第二句"])
        self.assertEqual(
            [line.timestamp_seconds for line in lines],
            [1.75, 3.375, 62.25],
        )
        self.assertEqual(lyric_index_at(lines, 0.5), -1)
        self.assertEqual(lyric_index_at(lines, 3.4), 1)

    def test_lrc_sidecar_lookup_is_case_insensitive(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            audio = root / "Song.ogg"
            audio.write_bytes(b"")
            lyric = root / "Song.LRC"
            lyric.write_text("[00:00]hello", encoding="utf-8")
            self.assertEqual(sidecar_lyrics_path(audio), lyric)

    def test_scan_filters_supported_and_hidden_files(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "Alpha.wav").write_bytes(b"not-a-real-wave")
            (root / "notes.txt").write_text("ignore", encoding="utf-8")
            (root / ".hidden.mp3").write_bytes(b"hidden")
            tracks = scan_music_library(root, ffprobe_command="/missing/ffprobe")
            self.assertEqual(len(tracks), 1)
            self.assertEqual(tracks[0].title, "Alpha")

    def test_library_preserves_current_track_on_refresh(self) -> None:
        player = MusicPlayer(Path("/music"))
        first = MusicTrack("/music/a.mp3", "A")
        second = MusicTrack("/music/b.mp3", "B")
        player.set_library([first, second])
        player.index = 1
        player.set_library([second, first])
        self.assertEqual(player.index, 0)
        self.assertEqual(player.current_track.title, "B")

    def test_empty_library_snapshot_is_safe(self) -> None:
        player = MusicPlayer(Path("/music"))
        snapshot = player.snapshot()
        self.assertEqual(snapshot["status"], "empty")
        self.assertEqual(snapshot["track_count"], 0)
        self.assertIsNone(snapshot["current_track"])

    def test_probe_reads_ogg_stream_metadata(self) -> None:
        payload = (
            '{"format":{"duration":"63.7"},"streams":[{"codec_type":"audio",'
            '"tags":{"title":"Solfeggietto","artist":"C.P.E. Bach · Nieb"}}]}'
        )
        with patch(
            "music_player.subprocess.run",
            return_value=SimpleNamespace(returncode=0, stdout=payload),
        ):
            track = _probe_track(Path("sample.ogg"), "/usr/bin/ffprobe")
        self.assertEqual(track.title, "Solfeggietto")
        self.assertEqual(track.artist, "C.P.E. Bach · Nieb")
        self.assertAlmostEqual(track.duration_seconds, 63.7)

    def test_elapsed_excludes_paused_time(self) -> None:
        values = iter([20.0, 30.0])
        player = MusicPlayer(Path("/music"), clock=lambda: next(values))
        player.set_library([MusicTrack("/music/a.mp3", "A", duration_seconds=60)])
        player.started_at = 10.0
        player.status = "playing"
        player.process = object()  # pause signal failures are intentionally tolerated
        player._send_signal = lambda _sig: None
        self.assertTrue(player.pause())
        self.assertTrue(player.resume())
        self.assertEqual(player.paused_total, 10.0)

    def test_pause_prefers_native_vlc_control(self) -> None:
        writes: list[str] = []

        class FakeStdin:
            def write(self, value: str) -> None:
                writes.append(value)

            def flush(self) -> None:
                return None

        process = SimpleNamespace(stdin=FakeStdin())
        values = iter([20.0, 30.0])
        player = MusicPlayer(Path("/music"), clock=lambda: next(values))
        player.set_library([MusicTrack("/music/a.mp3", "A", duration_seconds=60)])
        player.started_at = 10.0
        player.status = "playing"
        player.process = process
        self.assertTrue(player.pause())
        self.assertEqual(player.pause_transport, "vlc-rc")
        self.assertTrue(player.resume())
        self.assertEqual(writes, ["pause\n", "pause\n"])
        self.assertEqual(player.pause_transport, "none")

    def test_playback_modes_control_automatic_advance(self) -> None:
        player = MusicPlayer(Path("/music"))
        player.set_library(
            [
                MusicTrack("/music/a.mp3", "A"),
                MusicTrack("/music/b.mp3", "B"),
                MusicTrack("/music/c.mp3", "C"),
            ]
        )
        player.index = 1
        player.play = Mock(return_value=True)

        self.assertTrue(player.set_playback_mode(PLAYBACK_MODE_SINGLE_REPEAT))
        self.assertTrue(player.next(automatic=True))
        player.play.assert_called_with(1)

        self.assertTrue(player.set_playback_mode(PLAYBACK_MODE_LIST_LOOP))
        self.assertTrue(player.next(automatic=True))
        player.play.assert_called_with(2)

        self.assertTrue(player.set_playback_mode(PLAYBACK_MODE_SHUFFLE))
        with patch("music_player.random.choice", return_value=0):
            self.assertTrue(player.next(automatic=True))
        player.play.assert_called_with(0)

    def test_playback_mode_cycle_is_stable(self) -> None:
        player = MusicPlayer(Path("/music"))
        self.assertEqual(player.playback_mode, PLAYBACK_MODE_LIST_LOOP)
        self.assertEqual(player.cycle_playback_mode(), PLAYBACK_MODE_SINGLE_REPEAT)
        self.assertEqual(player.cycle_playback_mode(), PLAYBACK_MODE_SHUFFLE)
        self.assertEqual(player.cycle_playback_mode(), PLAYBACK_MODE_LIST_LOOP)
        self.assertFalse(player.set_playback_mode("unsupported"))

    def test_automatic_lyrics_fetch_saves_synced_lrc(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            audio = Path(directory) / "Song.mp3"
            audio.write_bytes(b"")
            track = MusicTrack(
                str(audio),
                "Song",
                artist="Artist",
                album="Album",
                duration_seconds=62.0,
            )
            requests = []

            def opener(request, *, timeout):
                requests.append((request, timeout))
                return self.JsonResponse(
                    {
                        "trackName": "Song",
                        "artistName": "Artist",
                        "duration": 62,
                        "instrumental": False,
                        "syncedLyrics": "[00:01.00]第一句\n[00:03.00]第二句",
                    }
                )

            result = fetch_lrclib_lyrics(track, opener=opener)
            self.assertTrue(result.found)
            self.assertEqual(parse_lrc(audio.with_suffix(".lrc"))[1].text, "第二句")
            self.assertIn("RiverBank-Assistant", requests[0][0].get_header("User-agent"))

    def test_automatic_lyrics_fetch_uses_ranked_search_fallback(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            audio = Path(directory) / "Song.mp3"
            audio.write_bytes(b"")
            track = MusicTrack(
                str(audio),
                "Song",
                artist="Artist",
                duration_seconds=62.0,
            )
            calls = 0

            def opener(request, *, timeout):
                nonlocal calls
                calls += 1
                if calls == 1:
                    raise urllib.error.HTTPError(
                        request.full_url,
                        404,
                        "not found",
                        None,
                        None,
                    )
                return self.JsonResponse(
                    [
                        {
                            "trackName": "Song",
                            "artistName": "Artist",
                            "duration": 62,
                            "instrumental": False,
                            "syncedLyrics": "[00:01.00]matched",
                        }
                    ]
                )

            with patch("music_player.time.sleep"):
                result = fetch_lrclib_lyrics(track, opener=opener)
            self.assertTrue(result.found)
            self.assertEqual(calls, 2)


if __name__ == "__main__":
    unittest.main()
