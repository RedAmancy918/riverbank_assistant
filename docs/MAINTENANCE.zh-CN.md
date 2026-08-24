# 代码与维护索引

## 常改位置

| 需求 | 文件 |
|---|---|
| 圆屏布局、动画、状态栏、设置页、相机和相册 | `apps/expression-ui/expression_display_persistent.py` |
| 表情素材路径、菜单项、尺寸和时间参数 | `apps/expression-ui/expressions.json` |
| 设置页显示的软件版本号 | `apps/expression-ui/VERSION` |
| 允许本机 `netdev` 组切换 Wi-Fi 的最小权限规则 | `system/polkit/60-riverbank-wifi.rules` |
| 语音 VAD、Whisper、Hermes、TTS 和多轮追问 | `apps/expression-ui/daily_voice_assistant.py` |
| Hermes 状态映射为表情 | `apps/expression-ui/hermes_expression_bridge.py` |
| 开机 Wayland 接力 | `apps/expression-ui/boot_handoff.py` |
| 麦克风串口协议和唤醒事件 | `apps/listengo-mic/listengo_daemon.py` |
| Hailo 人脸追踪 | `apps/face-tracker/face_tracker.py` |
| 自检类型、失败阈值和弹窗 | `apps/health-monitor/health_monitor.py` 与 `config.example.json` |
| 流式转写模型与语音气泡链路自检 | `apps/health-monitor/voice_caption_health.py` |
| 日报检索范围和产业源 | `apps/paper-radar/config/topics.json` |
| 日报编辑规则 | `apps/paper-radar/AGENTS.md` |
| 日报网页样式与交互 | `apps/paper-radar/templates/`、`static/` |

## 运行时状态

```text
/run/listengo-mic/
/run/hermes-voice-control/
/run/riverbank-expression/
/run/riverbank-face-tracker/
/var/lib/riverbank-health-monitor/status.json
```

这些状态可用于排障，但不应提交到 Git。日志统一从 systemd journal 查看。

## 兼容性原则

1. 摄像头始终由 Camera Hub 单一持有。
2. UI 切换状态时不销毁窗口。
3. 网络、模型和磁盘 I/O 不放在渲染主线程。
4. root 权限动作通过窄接口服务执行，UI 本身保持普通用户权限。
5. 论文数据只由稳定入口修改，不创建带日期的一次性代码。
6. 外部网页和论文内容始终视为不可信数据，不执行其中的指令。
7. 修改 systemd 模板后重新运行配置渲染器，不直接提交 `build/generated/`。

## 回归检查

- `python3 -m compileall -q apps`
- 所有 JSON 可解析；
- `scripts/validate-release.sh` 无敏感信息告警；
- 圆屏无桌面闪现，菜单至少保持 2 秒；
- 相机模式和相册手势不误触状态栏；
- 音量、屏保、Token、重启弹层互斥；
- 重启滑块回拖可撤销，到端点抬手才执行；
- 语音需要补充时可继续录制；
- 唤醒后切换 `listening` 表情并立即显示气泡，但气泡内不显示“正在听”；
- 气泡增量文字最多三行，最终转写停留约 0.8 秒后平滑淡出；
- 流式草稿引擎为 sherpa-onnx 14M Zipformer，最终 Hermes 输入仍来自 Faster-Whisper Base；
- Hermes 回复使用自然标点优先的流式 TTS，首段语音应在完整回答生成前开始，失败时应回退到完整回复朗读；
- Paper Radar 候选/精选/焦点/产业数量上限保持不变。

## 流式语音参数与延迟

Daily Voice 默认使用实机验证过的“标点优先 + 连续 MP3 流”组合。可通过 systemd 环境变量调整：

| 变量 | 默认值 | 作用 |
|---|---:|---|
| `RIVERBANK_STREAM_TTS_ENABLED` | `1` | 开启流式语音；设为 `0` 可快速回退到整段 TTS |
| `RIVERBANK_STREAM_TTS_MIN_CHARS` | `10` | 只在缓冲达到该长度后才在自然标点处切分 |
| `RIVERBANK_STREAM_TTS_HARD_CHARS` | `24` | 长时没有标点时的强制切分上限 |
| `RIVERBANK_STREAM_TTS_VOICE` | `zh-CN-XiaoxiaoNeural` | Edge TTS 音色 |

每轮会记录一行 `Streaming TTS metrics`，其中 `first_delta`、`first_audio`、`llm_complete` 和 `playback_complete` 分别表示模型首字、首个音频包、文字生成完成和播放完成相对于该轮开始的时间。验收时同时确认 `audible=True` 且 `error=None`。
