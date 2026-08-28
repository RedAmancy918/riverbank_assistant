# 部署说明

本说明面向与已验证原型相近的 Raspberry Pi 5。先在测试机验证，再部署到长期运行设备。

## 1. 生成本机配置

在仓库根目录运行：

```bash
python3 scripts/render_config.py \
  --user "$USER" \
  --data-dir /mnt/riverbank-data \
  --hailo-venv /mnt/riverbank-data/ai/apps/hailo-rpi5-examples/venv_hailo_rpi_examples
```

工具会读取当前用户 UID、主目录和仓库绝对路径，把 `config/` 中的 `@RIVERBANK_*@` 占位符渲染到 `build/generated/`，并生成 `manifest.json`。它不修改系统。

## 2. 准备运行目录和 Python 环境

```bash
mkdir -p apps/paper-radar/data/{generated,candidates,special-focus}
mkdir -p apps/paper-radar/{reports,public}
python3 -m venv apps/paper-radar/.venv
apps/paper-radar/.venv/bin/pip install -r apps/paper-radar/requirements.txt
```

圆屏 UI 的依赖可装入系统 Python 或专用虚拟环境；若 Daily Voice 需要直接导入 Hermes 模块，则应把 `apps/expression-ui/requirements.txt` 安装到 Hermes 使用的同一虚拟环境。

实时转写气泡需要 sherpa-onnx 的 `sherpa-onnx-streaming-zipformer-zh-14M-2023-02-23`。最终转写还需要 SenseVoice INT8 与 Zipformer CTC INT8，默认分别放在数据盘 `ai/models/asr-final/sensevoice-int8/` 和 `ai/models/asr-final/zipformer-ctc-int8/`，也可用 `RIVERBANK_FINAL_ASR_*` 环境变量改写。模型缺失时服务会明确报告最终 ASR 不可用，不会把低质量草稿直接提交给 Hermes。

将有权使用的 GIF 放入 `apps/expression-ui/assets/expressions/`；将可选唤醒回应放为 `apps/expression-ui/assets/audio/wake_ack.wav`。路径也可以通过环境变量或 `expressions.json` 改写。

## 3. 核对硬件参数

- `camera-hub.service` 的 `/dev/video0`、分辨率和帧率；
- `camera-usb-power.service` 与 udev 规则中的摄像头 VID/PID；
- `listengo-mic.service` 的串口稳定路径和 ALSA 卡名；
- `expressions.json` 的 `output`，以及 `labwc/rc.xml` 的触摸映射；
- Hailo 示例虚拟环境、HEF、后处理库和 JSON 路径；
- 数据盘是否在 systemd 启动服务前完成挂载。

如果 NVMe 与 Hailo 共用 PCIe 扩展板，先按 `HARDWARE.zh-CN.md` 的稳定性章节进行长时读取和重启验证；不要把仍然挂载但底层已经掉线的 NVMe 误判为正常。

## 4. 安装服务

先查看将要安装的内容：

```bash
sudo scripts/install.sh --generated build/generated
```

上述命令只打印计划。确认无误后显式执行：

```bash
sudo scripts/install.sh --generated build/generated --apply
```

安装器会复制核心 systemd 单元、健康监控配置、Plymouth 主题和 udev 规则，但不会安装 Hermes、Hailo、ViewTurbo、模型权重或 API 密钥。可选代理/VPN 单元不会自动启用。

若要启用圆屏设置页的 Wi-Fi 开关，确认运行用户属于 `netdev` 组后安装最小 Polkit 规则：

```bash
sudo install -m 0644 system/polkit/60-riverbank-wifi.rules \
  /etc/polkit-1/rules.d/60-riverbank-wifi.rules
```

该规则只允许 `netdev` 组启用或关闭 NetworkManager 的 Wi-Fi 无线电，不授予其他系统管理权限。

按实际已安装能力启用服务，例如：

```bash
sudo systemctl enable --now camera-hub.service
sudo systemctl enable --now listengo-mic.service
sudo systemctl enable --now expression-display.service
sudo systemctl enable --now riverbank-health-monitor.service
sudo systemctl enable --now paper-radar-web.service
```

只有在 Hermes 与 Hailo 依赖已经独立验证后，再启用相应服务。

Hailo 人脸追踪采用租约控制；服务启动不等于持续推理。可用下面的命令验证空闲、激活和自动到期三个状态：

```bash
python3 apps/face-tracker/face_trackerctl.py status
python3 apps/face-tracker/face_trackerctl.py acquire --source deploy-test --ttl 15
python3 apps/face-tracker/face_trackerctl.py status
```

正式基线可在所有测试通过后封存。版本必须使用 `vMAJOR.MINOR.PATCH beta|stable`：

```bash
sudo python3 apps/release-manager/release_manager.py seal \
  --version v0.7.1 --channel beta --notes "initial verified deployment"
python3 apps/release-manager/release_manager.py verify --json
```

## 5. Hermes Daily Profile

本仓库不生成 Hermes 私人配置。部署者需要自行建立独立的 Daily Profile，并配置：

- 普通对话模型；
- 视觉请求使用的 VLM；
- STT/TTS 和语言；
- API 密钥的安全存储；
- `AGENTS.md` 驱动的 Asia/Shanghai 08:00 日报任务。

请勿把 `.hermes/`、Profile `.env`、`auth.json`、SQLite 数据库或日志复制到公共仓库。

## 6. Tailscale 与 HTTPS

Paper Radar 默认监听 `0.0.0.0:19732`。可直接通过 Tailscale 地址在 tailnet 内访问，或使用 Tailscale 提供的 HTTPS/Serve 能力。是否收费取决于 Tailscale 当时的套餐和组织规模；部署前以官方条款为准。

Camera Hub 默认只监听 `127.0.0.1:19733`，不应直接暴露到局域网或公网。

## 7. 验证

```bash
scripts/validate-release.sh
python3 apps/expression-ui/expression_display_persistent.py --self-test
systemctl --failed
curl -fsS http://127.0.0.1:19732/healthz
curl -fsS http://127.0.0.1:19733/state
```

再分别验证唤醒、扬声器、触摸、相机、拍照、相册、屏保、重启确认、Hailo 状态和第二天 08:00 的日报生成。

## 8. 可选视频通话

为 `apps/video-call/` 创建独立 Python 3.11 虚拟环境并安装 `requirements.txt`，生成至少 16 字符的随机配对令牌到 `~/.config/riverbank-video-call/token`，权限设为仅设备用户可读。创建 `/home/geo/.hermes/profiles/daily/workspace/reports` 并保持 `geo` 可写。安装并启用 `riverbank-video-call.service` 与 `riverbank-task-worker.service`；systemd 的 `StateDirectory=riverbank-tasks` 会创建持久化队列目录。先访问 `http://127.0.0.1:19734/healthz`，确认 `reports_available` 与 `tasks` 都为 `true`，再运行 `video_call_smoke.py` 完成一次静音双向回环验证，并提交一项短任务验证报告归档。

Windows 端进入 `apps/video-call/windows-client/`，在 PowerShell 运行 `build.ps1` 生成安装版与便携版；Apple Silicon Mac 使用 `npm run dist:mac`。客户端默认通过 Tailscale MagicDNS 主机名 `http://riverbank-tech:19734` 连接；也可以手动填写树莓派的局域网或 Tailscale IP。报告下载使用系统保存窗口，服务端不接收文件写入。不要把 19734 映射到公网，Tailscale 使用 ACL 限制可访问设备。

iOS 端进入 `apps/ios/RiverBankMobile/` 运行 `xcodegen generate`，用 Xcode 选择自己的 Apple Development Team 后安装到 iPhone。App 使用 RiverBank 3×3 图标，配对令牌只写入 iOS Keychain；默认服务器为 `https://riverbank-tech.tail0acdab.ts.net/assistant`。设备需要安装并登录同一 tailnet 的 Tailscale。树莓派使用 `sudo tailscale serve --bg --set-path /assistant 19734` 增加 HTTPS 路由，不替换根路径的论文站。

终端工具可直接运行 `apps/video-call/riverbank_task.py`：先执行 `configure --server https://riverbank-tech.tail0acdab.ts.net/assistant --token TOKEN`，再用 `submit --wait '任务描述'`、`list`、`status TASK_ID`、`answer TASK_ID '补充内容'`、`cancel TASK_ID` 和 `download --task TASK_ID`。配置文件权限在 Unix 上自动设为 0600；CI 可改用 `RIVERBANK_SERVER_URL` 与 `RIVERBANK_PAIRING_TOKEN` 环境变量。
