import sys
import unittest
from pathlib import Path


REPOSITORY_OR_APP_DIR = Path(__file__).resolve().parents[1]
APP_DIR = REPOSITORY_OR_APP_DIR / "apps" / "expression-ui"
if not APP_DIR.is_dir():
    APP_DIR = REPOSITORY_OR_APP_DIR
sys.path.insert(0, str(APP_DIR))

from pomodoro_voice import (
    parse_application_voice_command,
    parse_pomodoro_voice_command,
)


class PomodoroVoiceTests(unittest.TestCase):
    def test_default_start(self) -> None:
        self.assertEqual(
            parse_pomodoro_voice_command("帮我开始一个番茄钟"),
            {"action": "start_pomodoro"},
        )

    def test_custom_duration_in_chinese(self) -> None:
        self.assertEqual(
            parse_pomodoro_voice_command("创建一个四十五分钟的番茄钟"),
            {"action": "start_pomodoro", "minutes": 45},
        )

    def test_half_hour_focus(self) -> None:
        self.assertEqual(
            parse_pomodoro_voice_command("开始半小时专注"),
            {"action": "start_pomodoro", "minutes": 30},
        )

    def test_controls(self) -> None:
        expected = {
            "暂停番茄钟": "pause_pomodoro",
            "继续番茄钟": "resume_pomodoro",
            "重置番茄钟": "reset_pomodoro",
            "跳过本轮番茄钟": "skip_pomodoro",
            "打开番茄钟": "open_pomodoro",
            "退出番茄钟界面": "close_pomodoro",
        }
        for phrase, action in expected.items():
            with self.subTest(phrase=phrase):
                self.assertEqual(
                    parse_pomodoro_voice_command(phrase),
                    {"action": action},
                )

    def test_discussion_is_not_intercepted(self) -> None:
        self.assertIsNone(parse_pomodoro_voice_command("番茄工作法有什么优点"))

    def test_invalid_duration_is_explicit(self) -> None:
        self.assertEqual(
            parse_pomodoro_voice_command("开始一个200分钟番茄钟"),
            {
                "action": "start_pomodoro",
                "minutes": 200,
                "invalid_duration": True,
            },
        )


class ApplicationVoiceTests(unittest.TestCase):
    def test_performance_page_commands(self) -> None:
        self.assertEqual(
            parse_application_voice_command("打开系统性能"),
            {"action": "open_performance"},
        )
        self.assertEqual(
            parse_application_voice_command("退出性能页面"),
            {"action": "close_performance"},
        )

    def test_video_call_commands(self) -> None:
        expected = {
            "打开视频通话": {"action": "open_video_call"},
            "帮我打一个视频电话": {"action": "open_video_call"},
            "开始通话": {"action": "open_video_call"},
            "挂断": {"action": "hangup_video_call"},
            "结束视频通话": {"action": "hangup_video_call"},
        }
        for phrase, command in expected.items():
            with self.subTest(phrase=phrase):
                self.assertEqual(parse_application_voice_command(phrase), command)

    def test_music_page_and_transport_commands(self) -> None:
        expected = {
            "打开音乐": {"action": "open_music"},
            "退出音乐界面": {"action": "close_music"},
            "播放音乐": {"action": "play_music"},
            "暂停音乐": {"action": "pause_music"},
            "下一首": {"action": "next_music"},
            "帮我换一首": {"action": "next_music"},
            "上一首歌": {"action": "previous_music"},
        }
        for phrase, command in expected.items():
            with self.subTest(phrase=phrase):
                self.assertEqual(parse_application_voice_command(phrase), command)

    def test_music_mode_and_lyrics_commands(self) -> None:
        expected = {
            "单曲循环": {"action": "set_music_mode", "mode": "single_repeat"},
            "列表循环": {"action": "set_music_mode", "mode": "list_loop"},
            "乱序播放": {"action": "set_music_mode", "mode": "shuffle"},
            "打开主页歌词": {"action": "set_music_lyrics", "enabled": True},
            "关闭歌词": {"action": "set_music_lyrics", "enabled": False},
        }
        for phrase, command in expected.items():
            with self.subTest(phrase=phrase):
                self.assertEqual(parse_application_voice_command(phrase), command)

    def test_unrelated_discussion_is_not_intercepted(self) -> None:
        for phrase in (
            "继续",
            "音乐有什么好处",
            "下一首论文讲了什么",
            "系统性能为什么重要",
        ):
            with self.subTest(phrase=phrase):
                self.assertIsNone(parse_application_voice_command(phrase))


if __name__ == "__main__":
    unittest.main()
