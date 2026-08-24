# 架构与实现说明

## 1. 服务边界

RiverBank Edge 采用多个小型 systemd 服务，而不是一个持有全部硬件和业务状态的单体程序。服务之间通过本机 HTTP、Unix Socket 和只读状态文件通信。

| 模块 | 入口 | 对外契约 |
|---|---|---|
| Camera Hub | `camera-hub.service` | `:19733/stream`、`/snapshot`、`/state` |
| ListenGo | `listengo-mic.service` | `/run/listengo-mic/control.sock`、`state.json` |
| Daily Voice | `hermes-voice.service` | `/run/hermes-voice-control/control.sock`、`state.json` |
| Expression UI | `expression-display.service` | `/run/riverbank-expression/control.sock`、`state.json` |
| Face Tracker | `riverbank-face-tracker.service` | `/run/riverbank-face-tracker/control.sock`、`state.json` |
| Health Monitor | `riverbank-health-monitor.service` | `/var/lib/riverbank-health-monitor/status.json` |
| Paper Radar | `paper-radar-web.service` | `:19732` 网页与焦点输入 API |

运行时文件只包含经过筛选的状态，不应写入密钥、完整语音、模型回复历史或摄像头原始流。

## 2. 摄像头单一持有

USB 摄像头由 `ustreamer` 单独持有。其他服务读取本地 MJPEG 流或单帧接口，从而避免多个 OpenCV/GStreamer 进程同时打开 `/dev/video0`。

- 圆屏相机模式：按刷新节奏获取 `/snapshot`，退出后停止主动取帧。
- 视觉问答：Daily Voice 在识别到视觉意图时保存一张临时帧给 Hermes。
- Hailo 人脸追踪：读取 `/stream` 并输出结构化人脸位置；这不改变 UI 上“主动视觉调用”指示灯的语义。
- 相册：拍照时保存高质量 JPEG 到 `RIVERBANK_DATA/camera/gallery`。

相机常驻的目的只是避免硬件抢占和冷启动；隐私指示灯只应表示用户可感知的主动视觉任务，而不是 Camera Hub 自身。

## 3. 圆屏渲染

`expression_display_persistent.py` 创建一次无边框窗口并持续复用。GIF 解码、背景处理、4× 超采样抗锯齿、状态栏组件和部分过渡帧采用预加载或缓存，减少切换时桌面闪现和主线程卡顿。

主要状态包括：

- 表情待机与触摸表情；
- 长按后至少保留 2 秒的六项环形菜单；
- 左右两个状态胶囊；
- 从环形菜单进入的设置页，自动刷新 Wi-Fi、蓝牙、软件版本与系统自检信息，支持 Wi-Fi 开关；表情与设置、设置首页与详情之间统一使用缓存横向过场，并支持按钮或左向右滑动返回；
- 音量、重启、屏保和 Token 弹层；
- 相机实时画面、拍照与相册；
- 相册左右滑动、长按删除/设为屏保；
- 边缘向中心滑动退出相机；再次选择“相机”也可退出。

重启采用两级权限边界：UI 只在滑块完成且手指抬起时写入 `/run/riverbank-expression/reboot.request`；root 运行的 path unit 观察该文件并执行重启。滑块回拖可在抬手前撤销。

## 4. 开机显示接力

```text
Kernel / Plymouth
    ↓ 黑底紧凑 Logo
LightDM + labwc
    ↓ boot_handoff.py 在 DSI Wayland 输出保持静态 Logo
Expression UI
    ↓ 3×3 方块逐个跳动并读取健康状态
静态 Logo 淡出
    ↓
表情待机
```

Plymouth 负责最早可见阶段；Wayland 接力层消除显示管理器接管期间的桌面或黑屏；常驻渲染器只有在窗口建立后才通知接力层退出。`boot_handoff.py` 优先使用 `swaybg`，GTK 仅作回退。

## 5. 语音与模型路由

ListenGo 控制口报告硬件唤醒事件和声源方向。Daily Voice 随后执行：

1. 播放可选的本地唤醒回应；
2. 唤醒后切换“聆听”表情，并仅在录音窗口显示左下角转写气泡；气泡本身代表正在收音，不再显示额外的“正在听”文字；
3. VAD 录音并保留短预卷，同时由 sherpa-onnx 流式 Zipformer 生成不超过三行的本地增量草稿；
4. 录音结束后再由 Faster-Whisper Base 以完整音频生成最终转写，短暂停留并淡出；
5. 先匹配音量、相机等本地设备命令；
6. 其他问题交给常驻复用的 Hermes Daily Profile；
7. Edge TTS 合成并从扬声器播放；
8. 若回复要求补充信息，继续录制下一轮，最多由配置限制轮数。

气泡中的原始文字只保存在渲染器内存中；运行时状态只记录长度和阶段。14M 中文流式模型只承担低延迟视觉反馈，交给 Hermes 的仍是 Faster-Whisper 对完整音频生成的最终识别结果。流式模型默认位于 `/mnt/nvme64/ai/models/sherpa-onnx-streaming-zipformer-zh-14M-2023-02-23`，可用 `RIVERBANK_STREAMING_ASR_MODEL_DIR` 覆盖。

模型名称、供应商、密钥和 Profile 数据均由外部 Hermes 配置提供，本仓库不保存。视觉意图会携带当前摄像头帧进入 Hermes；具体路由到 Qwen 或其他 VLM 应在 Daily Profile 中配置。

## 6. 论文日报

Paper Radar 的数据面与呈现面分开：

```text
collect.py → candidate JSON → Hermes 按 AGENTS.md 精读/撰写
           → latest-report.json → finalize_run.py
           → Markdown 归档 + 原子化静态网页 + candidates 页面
```

默认策略为 Asia/Shanghai 每日 08:00：

- 检索具身智能、上游生成建模、机器人交叉应用、分类最新、VLM、空间智能/VGGT 类方法和 JEPA/预测世界模型；
- 近 72 小时优先，空余位置从 183 天内未展示内容回填；
- 候选阈值 10，最多 30；精选阈值 40，最多 10；产业动态最多 9；一次性焦点最多额外 5；
- 候选保存原始摘要并生成忠实中文概述；高相关项目阅读全文；
- 对尚无机器人实验的迁移价值必须明确标记为“分析与推断”。

网页只默认展示最新日报，但保留历史归档入口。“明日焦点”请求写入原子化队列，只有成功完成下一次渲染后才标记为已消费。

## 7. 健康监控

检查器由 JSON 配置扩展，支持：

- `systemd_service`
- `http` / `http_json`
- `tcp`
- `mount`
- `path` / `fresh_path`
- `command`

连续失败达到阈值后才显示桌面告警；检查结果同步写入状态文件，圆屏开机自检和“设置”页面都可读取。专有 VPN 和代理检查在公开示例中默认关闭。
