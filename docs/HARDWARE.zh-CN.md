# 硬件与第三方依赖

## 已验证目标

- Raspberry Pi 5，64 位 Raspberry Pi OS / Debian 系；
- 800×800 圆形 DSI 触摸屏，当前配置输出名为 `DSI-2`；
- USB RGB 摄像头；
- Hailo-8 M.2 推理模块；
- NVMe 数据盘，与 Hailo 共用 PCIe x1 扩展板的配置也可运行；
- ListenGo 类六麦阵列，ALSA 音频设备加 USB 串口控制协议；
- USB 或阵列扬声器；
- 可选 Tailscale。

设备名、USB VID/PID、串口稳定路径、DSI 输出名和触摸设备名可能随硬件批次变化，部署前必须在目标机确认。

## 不随仓库分发

| 依赖 | 原因 |
|---|---|
| Hermes Agent 与 Profile | 上游项目；Profile 含账号、密钥、数据库和私人数据 |
| HailoRT、TAPPAS、Hailo Pi 示例 | 由 Hailo 官方安装和授权 |
| SCRFD/YOLO HEF、后处理 `.so` | 体积大且可能受模型或 SDK 许可限制 |
| 表情 GIF | 原素材的再分发权尚未确认 |
| 唤醒回应 WAV | 语音素材应由部署者自行生成或授权 |
| 微软雅黑 | 专有字体；公开配置改用文泉驿正黑 |
| ViewTurbo | 专有 VPN；只保留可选服务示例，不包含程序或登录数据 |

## 系统软件

典型依赖包括 `python3`、`python3-venv`、`python3-pip`、`python3-pygame`、`python3-pil`、`python3-gi`、GStreamer、`ustreamer`、`labwc`、`lightdm`、`plymouth`、`swaybg`、`zenity`、PipeWire/PulseAudio 兼容层和 `fonts-wqy-zenhei`。

语音环境还需要 `faster-whisper`、`sounddevice`、`numpy` 与 `edge-tts`；麦克风控制服务需要 `pyserial`。Paper Radar 的 Python 依赖见各模块的 `requirements.txt`。

## 模型和数据目录约定

`scripts/render_config.py` 接受任意绝对数据目录。默认文档示例为 `/mnt/riverbank-data`：

```text
/mnt/riverbank-data/
  ai/apps/hailo-rpi5-examples/
  ai/models/face/
  ai/resources/so/
  camera/gallery/
```

模型、相册、论文数据库和生成网页属于运行数据，不应提交到 Git。
