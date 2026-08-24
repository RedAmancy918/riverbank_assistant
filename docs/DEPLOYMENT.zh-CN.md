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

将有权使用的 GIF 放入 `apps/expression-ui/assets/expressions/`；将可选唤醒回应放为 `apps/expression-ui/assets/audio/wake_ack.wav`。路径也可以通过环境变量或 `expressions.json` 改写。

## 3. 核对硬件参数

- `camera-hub.service` 的 `/dev/video0`、分辨率和帧率；
- `camera-usb-power.service` 与 udev 规则中的摄像头 VID/PID；
- `listengo-mic.service` 的串口稳定路径和 ALSA 卡名；
- `expressions.json` 的 `output`，以及 `labwc/rc.xml` 的触摸映射；
- Hailo 示例虚拟环境、HEF、后处理库和 JSON 路径；
- 数据盘是否在 systemd 启动服务前完成挂载。

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
systemctl --failed
curl -fsS http://127.0.0.1:19732/healthz
curl -fsS http://127.0.0.1:19733/state
```

再分别验证唤醒、扬声器、触摸、相机、拍照、相册、屏保、重启确认、Hailo 状态和第二天 08:00 的日报生成。
