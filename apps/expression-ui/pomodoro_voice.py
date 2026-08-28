#!/usr/bin/env python3
"""Pure, dependency-free parsing for Daily application voice commands."""

from __future__ import annotations

import re


CHINESE_DIGITS = {
    "零": 0,
    "〇": 0,
    "一": 1,
    "二": 2,
    "两": 2,
    "兩": 2,
    "三": 3,
    "四": 4,
    "五": 5,
    "六": 6,
    "七": 7,
    "八": 8,
    "九": 9,
}
POMODORO_NOUN = r"(?:番茄钟|番茄鐘|番茄任务|番茄任務|专注|專注)"
PERFORMANCE_NOUN = r"(?:性能|系统性能|系統性能|性能监控|性能監控|资源监控|資源監控)"
MUSIC_NOUN = r"(?:音乐|音樂|播放器|歌曲)"
VIDEO_CALL_NOUN = r"(?:视频通话|視頻通話|视频电话|視頻電話|视讯通话|視訊通話|通话|通話)"


def parse_chinese_number(value: str) -> int | None:
    text = value.strip()
    if not text:
        return None
    if text.isdigit():
        return int(text)
    if text == "百":
        return 100
    if "百" in text:
        prefix, _, suffix = text.partition("百")
        hundreds = CHINESE_DIGITS.get(prefix, 1)
        remainder = parse_chinese_number(suffix) if suffix else 0
        return hundreds * 100 + (remainder or 0)
    if "十" in text:
        prefix, _, suffix = text.partition("十")
        tens = CHINESE_DIGITS.get(prefix, 1) if prefix else 1
        ones = CHINESE_DIGITS.get(suffix, 0) if suffix else 0
        return tens * 10 + ones
    digits: list[str] = []
    for character in text:
        digit = CHINESE_DIGITS.get(character)
        if digit is None:
            return None
        digits.append(str(digit))
    return int("".join(digits)) if digits else None


def extract_duration_minutes(compact: str) -> int | None:
    if "半小时" in compact or "半個小時" in compact or "半个小时" in compact:
        return 30
    hour_match = re.search(
        r"([0-9]{1,2}|[零〇一二两兩三四五六七八九十]{1,3})(?:个|個)?小时",
        compact,
    )
    if hour_match:
        hours = parse_chinese_number(hour_match.group(1))
        return None if hours is None else hours * 60
    minute_match = re.search(
        r"([0-9]{1,3}|[零〇一二两兩三四五六七八九十百]{1,5})分钟",
        compact,
    )
    if minute_match:
        return parse_chinese_number(minute_match.group(1))
    return None


def parse_pomodoro_voice_command(text: str) -> dict | None:
    """Return a safe local Pomodoro action, or ``None`` for normal dialogue."""

    compact = re.sub(r"[\s，。！？、,.!?;；:：]", "", text)
    if not re.search(POMODORO_NOUN, compact):
        return None

    if re.search(rf"(?:关闭|关掉|退出)(?:一下)?{POMODORO_NOUN}(?:界面|页面)?", compact):
        return {"action": "close_pomodoro"}
    if re.search(rf"(?:暂停|停一下){POMODORO_NOUN}|{POMODORO_NOUN}(?:暂停|停一下)", compact):
        return {"action": "pause_pomodoro"}
    if re.search(rf"(?:继续|恢复){POMODORO_NOUN}|{POMODORO_NOUN}(?:继续|恢复)", compact):
        return {"action": "resume_pomodoro"}
    if re.search(rf"(?:重置|复位|重新开始|取消|停止){POMODORO_NOUN}|{POMODORO_NOUN}(?:重置|复位|取消|停止)", compact):
        return {"action": "reset_pomodoro"}
    if re.search(r"(?:跳过|结束)(?:这一轮|本轮|当前)?(?:番茄钟|番茄鐘|专注|專注|休息)", compact):
        return {"action": "skip_pomodoro"}
    if re.search(rf"(?:打开|开启|进入|显示|看看)(?:一下)?{POMODORO_NOUN}(?:界面|页面)?", compact):
        return {"action": "open_pomodoro"}

    start_intent = re.search(
        rf"(?:开始|启动|创建|新建|开|来)(?:一个|一個|个|個|一下)?(?:[0-9零〇一二两兩三四五六七八九十百半个個小时分鐘分钟]*)?(?:的)?{POMODORO_NOUN}",
        compact,
    ) or re.search(
        rf"{POMODORO_NOUN}(?:开始|启动|创建|新建)",
        compact,
    )
    if start_intent:
        minutes = extract_duration_minutes(compact)
        command = {"action": "start_pomodoro"}
        if minutes is not None:
            command["minutes"] = minutes
            if not 1 <= minutes <= 180:
                command["invalid_duration"] = True
        return command
    return None


def parse_application_voice_command(text: str) -> dict | None:
    """Return a safe local application action when explicit."""

    compact = re.sub(r"[\s，。！？、,.!?;；:：]", "", text)

    if re.fullmatch(
        rf"(?:帮我|幫我|请|請)?(?:挂断|掛斷|结束|結束|关闭|關閉|退出)(?:一下)?(?:当前|目前)?{VIDEO_CALL_NOUN}",
        compact,
    ) or re.fullmatch(r"(?:帮我|幫我|请|請)?(?:挂断|掛斷)", compact):
        return {"action": "hangup_video_call"}
    if re.search(
        rf"(?:打开|打開|开启|開啟|启动|啟動|进入|進入|开始|開始|发起|發起|接通|打)(?:一下|一个|一個)?{VIDEO_CALL_NOUN}(?:界面|页面|頁面)?",
        compact,
    ):
        return {"action": "open_video_call"}

    if re.search(
        rf"(?:关闭|關閉|退出|收起)(?:一下)?{PERFORMANCE_NOUN}(?:界面|页面|頁面)?",
        compact,
    ):
        return {"action": "close_performance"}
    if re.search(
        rf"(?:打开|打開|开启|開啟|进入|進入|显示|顯示|查看|看看)(?:一下)?{PERFORMANCE_NOUN}(?:界面|页面|頁面)?",
        compact,
    ):
        return {"action": "open_performance"}

    if re.search(
        r"(?:关闭|關閉|退出|收起)(?:一下)?(?:音乐|音樂|播放器)(?:界面|页面|頁面)",
        compact,
    ):
        return {"action": "close_music"}

    if re.fullmatch(
        r"(?:帮我|幫我|给我|給我|请|請)?"
        r"(?:(?:播放|切换|切換|切到|换成|換成|换|換|切)(?:到)?)?"
        r"(?:下一首|下首)(?:歌|歌曲|音乐|音樂)?",
        compact,
    ) or re.fullmatch(
        r"(?:帮我|幫我|给我|給我|请|請)?(?:换一首|換一首|切歌)",
        compact,
    ):
        return {"action": "next_music"}
    if re.fullmatch(
        r"(?:帮我|幫我|给我|給我|请|請)?"
        r"(?:(?:播放|切换|切換|切到|换成|換成|换|換|切)(?:到)?)?"
        r"(?:上一首|上首)(?:歌|歌曲|音乐|音樂)?",
        compact,
    ):
        return {"action": "previous_music"}

    mode_patterns = (
        (
            "single_repeat",
            r"(?:切换|切換|设置|設置|设为|設為|改成|改为|改為|开启|開啟)?单曲循环",
        ),
        (
            "list_loop",
            r"(?:切换|切換|设置|設置|设为|設為|改成|改为|改為|开启|開啟)?列表循环",
        ),
        (
            "shuffle",
            r"(?:切换|切換|设置|設置|设为|設為|改成|改为|改為|开启|開啟)?(?:随机播放|隨機播放|乱序播放|亂序播放)",
        ),
    )
    for mode, pattern in mode_patterns:
        if re.fullmatch(pattern, compact):
            return {"action": "set_music_mode", "mode": mode}

    if re.search(r"(?:显示|顯示|打开|打開|开启|開啟)(?:主页|主頁)?歌词", compact):
        return {"action": "set_music_lyrics", "enabled": True}
    if re.search(r"(?:隐藏|隱藏|关闭|關閉|关掉|關掉)(?:主页|主頁)?歌词", compact):
        return {"action": "set_music_lyrics", "enabled": False}

    if re.search(
        rf"(?:暂停|暫停|停一下|停止播放|关掉|關掉)(?:一下)?{MUSIC_NOUN}|"
        rf"{MUSIC_NOUN}(?:暂停|暫停|停一下|停止播放)",
        compact,
    ):
        return {"action": "pause_music"}
    if re.search(
        r"(?:继续|繼續|恢复|恢復)(?:播放(?:音乐|音樂|歌曲)?|(?:音乐|音樂|歌曲))|"
        r"(?:音乐|音樂|播放器)(?:继续|繼續|恢复|恢復)(?:播放)?",
        compact,
    ):
        return {"action": "play_music"}
    if re.search(
        r"(?:播放|放|来点|來點|来首|來首)(?:一下)?(?:音乐|音樂|歌曲|歌)",
        compact,
    ):
        return {"action": "play_music"}
    if re.search(
        r"(?:打开|打開|开启|開啟|进入|進入|显示|顯示|查看|看看)"
        r"(?:一下)?(?:音乐|音樂|播放器)(?:界面|页面|頁面)?",
        compact,
    ):
        return {"action": "open_music"}
    return None
