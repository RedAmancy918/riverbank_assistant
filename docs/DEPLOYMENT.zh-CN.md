# 部署说明

当前设备软件正式名称为 **RiverBank Edge OS**，本文对应 `v0.26.0 beta` 的受管 Linux 系统层安装方式。它不是可直接烧录的 `.img.xz`；镜像路线与分发边界见 `docs/EDGE_OS.zh-CN.md`。

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
若在开发机为另一台设备交叉渲染，使用 `--repo /目标设备/上的/riverbank-edge-os` 明确目标路径；不得把开发机绝对路径写入设备服务。

## 2. 准备运行目录和 Python 环境

```bash
mkdir -p /mnt/riverbank-data/paper-radar/data/{generated,candidates,special-focus}
mkdir -p /mnt/riverbank-data/paper-radar/{reports,public}
python3 -m venv apps/paper-radar/.venv
apps/paper-radar/.venv/bin/pip install -r apps/paper-radar/requirements.txt
sudo apt-get install python3-qrcode
```

圆屏 UI 的依赖可装入系统 Python 或专用虚拟环境；若 Daily Voice 需要直接导入 Hermes 模块，则应把 `apps/expression-ui/requirements.txt` 安装到 Hermes 使用的同一虚拟环境。

实时转写气泡需要 sherpa-onnx 的 `sherpa-onnx-streaming-zipformer-zh-14M-2023-02-23`。最终转写还需要 SenseVoice INT8 与 Zipformer CTC INT8，默认分别放在数据盘 `ai/models/asr-final/sensevoice-int8/` 和 `ai/models/asr-final/zipformer-ctc-int8/`，也可用 `RIVERBANK_FINAL_ASR_*` 环境变量改写。模型缺失时服务会明确报告最终 ASR 不可用，不会把低质量草稿直接提交给 Hermes。

将有权使用的 GIF 放入 `$HOME/.local/share/riverbank/assets/expressions/`，使用 `expressions.json` 中的 15 个标准文件名；将可选唤醒回应放为 `$HOME/.local/share/riverbank/assets/audio/wake_ack.wav`。这些设备资产不进入 Git 或公开 OTA，升级代码时也不会覆盖。路径可以通过环境变量或 `expressions.json` 改写。

日报的 `data/`、`reports/` 和生成后的 `public/` 属于设备状态，不属于应用源码。既有设备升级时先复制到 `RIVERBANK_DATA/paper-radar/` 并核对 `latest-report.json` 摘要，再让正式源码树中的同名目录指向该状态目录；不得用空仓库目录覆盖现有日报或问询缓存。

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

安装器会复制核心 systemd 单元、健康监控配置、Plymouth 主题和 udev 规则，并在首次安装时为工坊创建不可导出的设备 Ed25519 私钥及对应的本机信任公钥。已有私钥或公钥只存在一侧时安装器会停止，不会静默重建身份。安装器不会安装 Hermes、Hailo、ViewTurbo、模型权重或 API 密钥；可选代理/VPN 单元不会自动启用。
安装后 `riverbank-release` 与 `riverbank-agentctl` 是固定指向本机正式 Edge OS 基线的入口，不再依赖脚本被复制到哪个目录。

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
sudo systemctl enable --now riverbank-recovery.service
sudo systemctl enable --now riverbank-provisioning.service
sudo systemctl enable --now riverbank-agent-runtime.service
sudo systemctl enable --now riverbank-workshop.service
```

只有在 Hermes 与 Hailo 依赖已经独立验证后，再启用相应服务。

Hailo 采用单一所有者协调器与进程隔离的租约控制；服务启动不等于已加载模型。空闲时应只看到协调器，申请租约后才出现 SCRFD Worker，到期后 Worker 必须完整退出。可用下面的命令验证空闲、激活和自动到期三个状态：

```bash
python3 apps/face-tracker/face_trackerctl.py status
python3 apps/face-tracker/face_trackerctl.py acquire --source deploy-test --ttl 15
python3 apps/face-tracker/face_trackerctl.py status
```

正式基线可在所有测试通过后封存。版本必须使用 `vMAJOR.MINOR.PATCH beta|stable`：

```bash
sudo riverbank-release seal \
  --version 0.26.0 --channel beta --notes "verified RiverBank Edge OS release"
riverbank-release verify --json
```

这里封存的是 RiverBank Edge OS，不是 iOS/macOS/Windows App，也不是未经 RiverBank 管理的上游 Debian/Raspberry Pi OS。当前交付形态仍是在受支持基础系统上安装受管系统层；可烧录镜像路线见 `docs/EDGE_OS.zh-CN.md`。跨端发布组合以 `config/version-catalog.json` 为准；GitHub CI/CD 与作用域标签见 `docs/CI_CD.zh-CN.md`。

## 5. Hermes Daily Profile

本仓库不生成 Hermes 私人配置。部署者需要自行建立独立的 Daily Profile，并配置：

- 普通对话模型；
- 视觉请求使用的 VLM；
- 文生图使用的阿里云中国区 Qwen Image 权限；
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
python3 apps/agent-runtime/agentctl.py health
python3 apps/expression-ui/expression_display_persistent.py --self-test
python3 -m unittest tests.test_workshop_contract tests.test_workshop_pipeline -v
python3 apps/workshop/workshopctl.py service-health
systemctl --failed
curl -fsS http://127.0.0.1:19732/healthz
curl -fsS http://127.0.0.1:19733/state
curl -fsS http://127.0.0.1:19735/api/status
```

再分别验证唤醒、扬声器、触摸、相机、拍照、相册、屏保、重启确认、Hailo 状态和第二天 08:00 的日报生成。工坊需要额外说一次明确的“创建一个麦克风声音计数应用”：确认邀请语结束后橙色录音隐私点亮起，需求能完成转写，系统只生成提案，圆屏明确显示麦克风高风险权限且未批准前不能运行；批准后打开应用，确认 dBFS 会随拍手变化、声音事件只按上升沿计数，退出应用后隐私点与 `pw-cat` 采集进程立即消失。测试完成后可卸载该测试应用。

Paper Radar 文章问询的按需补证依赖其虚拟环境中的 `requests`、`beautifulsoup4`，以及系统
`/usr/bin/pdftotext`。部署后可先用缓存能回答的概述问题确认不会下载，再询问一个报告未写明
的具体型号或参数；界面应先显示正在补充核对原文，随后
`data/paper-qa/current.json` 中对应文章的 `source_state` 应变为
`arxiv_pdf_on_demand`（PDF 不可用时为 `arxiv_html`）。不得把该缓存复制为日期归档。

还应在维护窗口验证一次断网恢复：网络正常时 19735 仅返回 `inactive`；断网超过配置宽限期后圆屏应显示二维码，完全无默认路由时还应出现唯一的 `RiverBank-Setup-*` 热点。不要在无人值守设备上直接做破坏性断网测试。SYSTEM 页点击“恢复全部服务”后，恢复状态应在数秒内变为 `complete`；若日报日期落后，`riverbank-paper-catchup.service` 或其延迟 timer 应进入 active，且重复点击不得产生第二个日报执行。

## 8. 可选视频通话

为 `apps/video-call/` 创建独立 Python 3.11 虚拟环境并安装 `requirements.txt`（包括
`argon2-cffi`），生成至少 16 字符的随机一次性设备凭据到
`~/.config/riverbank-video-call/token`，权限设为仅设备用户可读。它不再是远端客户端的日常
登录密钥，只用于首次创建管理员和树莓派本机服务。创建
`/home/geo/.hermes/profiles/daily/workspace/reports` 并保持 `geo` 可写。安装并启用
`riverbank-video-call.service`、`riverbank-task-worker.service` 与
`riverbank-chat-worker.service`；systemd 的 `StateDirectory=riverbank-tasks` 会创建持久化
目录。账号库为 `/var/lib/riverbank-tasks/auth.db`，Chat 库为同目录的 `chat.db`。先访问
`http://127.0.0.1:19734/healthz`，再运行账号和 Chat 单元测试、视频回环测试与一项短任务。

Chat Worker 通过 `EnvironmentFile=-/home/geo/.hermes/.env` 读取中国区
`DASHSCOPE_API_KEY`，以便处理文生图；不要把密钥写入 service 文件或仓库。默认原生端点为
`https://dashscope.aliyuncs.com/api/v1`、模型为 `qwen-image-3.0-pro`。如账号暂未开通该
模型，可在同一私有环境文件中用 `RIVERBANK_IMAGE_GENERATION_MODEL` 选择已获授权的 Qwen
Image 模型。部署后用一条明确的生成请求验证消息包含图片附件，同时确认第二个账号无法读取
该附件；测试会实际产生模型调用费用。

macOS 与 Windows 共用 `apps/video-call/windows-client/` 中的 Electron 源码；目录名只是历史
命名。Windows 在 PowerShell 运行 `build.ps1` 生成安装版与便携版，Apple Silicon Mac
运行 `npm run dist:mac` 生成 DMG 与 ZIP。客户端默认通过 Tailscale Serve HTTPS 地址连接，
以用户名和密码换取可撤销会话。首次管理员在登录页使用一次性设备凭据建立。管理员可在
账号面板创建和停用其他用户。报告下载使用系统保存窗口。不要把 19734 映射到公网，
Tailscale 使用 ACL 限制可访问设备。

iOS 端进入 `apps/ios/RiverBankMobile/` 运行 `xcodegen generate`，用 Xcode 选择自己的 Apple
Development Team 后安装到 iPhone。App 使用 RiverBank 3×3 图标，账号会话只写入 iOS
Keychain，密码不保存；默认服务器为
`https://riverbank-tech.tail0acdab.ts.net/assistant`。设备需要安装并登录同一 tailnet 的
Tailscale。树莓派使用 `sudo tailscale serve --bg --set-path /assistant 19734` 增加 HTTPS
路由，不替换根路径的论文站。完整账号契约见 `docs/AUTHENTICATION.zh-CN.md`。

上述三端的安装包均为本机构建产物：Windows EXE、macOS DMG/ZIP、iOS APP/IPA/XCArchive 和签名描述文件不会进入 Git。需要共享二进制时使用 GitHub Release 或正式商店分发，并记录版本、目标架构、签名状态和 SHA-256。

终端工具可直接运行 `apps/video-call/riverbank_task.py`：先执行 `configure --server https://riverbank-tech.tail0acdab.ts.net/assistant --token TOKEN`，再用 `submit --wait '任务描述'`、`list`、`status TASK_ID`、`answer TASK_ID '补充内容'`、`cancel TASK_ID` 和 `download --task TASK_ID`。配置文件权限在 Unix 上自动设为 0600；CI 可改用 `RIVERBANK_SERVER_URL` 与 `RIVERBANK_PAIRING_TOKEN` 环境变量。
