# RiverBank Edge OS 本机部署与状态说明

同步日期：2026-09-07（Asia/Shanghai）

设备：RiverBank Edge

设备操作系统：**RiverBank Edge OS v0.26.6 beta**

分发模式：`managed-linux-system-layer`
上游基础发行版：Debian GNU/Linux 12 (bookworm)

> 本文只记录程序、配置、版本与维护入口，不记录密码、注册通行码、会话令牌、模型密钥或私人数据。

## 正式访问入口

| 功能 | 地址 | 说明 |
| --- | --- | --- |
| 具身智讯日报 | `https://riverbank-tech.tail0acdab.ts.net/` | Tailnet 内 HTTPS |
| RiverBank Edge Host | `https://riverbank-tech.tail0acdab.ts.net/assistant` | iOS、macOS、Windows 登录与业务 API |
| 管理员网站 | `https://riverbank-tech.tail0acdab.ts.net/assistant/admin/` | 仅 RiverBank 管理员账号可登录 |
| 管理员网站（本机） | `http://127.0.0.1:19734/admin/` | 只用于设备本机维护 |
| Camera Hub（本机） | `http://127.0.0.1:19733/` | 摄像头共享、状态、单帧和 MJPEG 流 |
| 离线配网服务（本机） | `http://127.0.0.1:19735/api/status` | 断网时进入配置流程 |

远程正式入口必须保留 `/assistant` 路径。管理员入口必须保留 `/assistant/admin/`；不要把本机端口直接暴露到公网。

## 当前版本组合

| 产物 | 版本 |
| --- | --- |
| RiverBank 发布列车 | `v0.26.6 beta` |
| RiverBank Edge OS | `v0.26.6 beta` |
| RiverBank iOS | `v0.25.2 beta`，build 3 |
| RiverBank Call（macOS/Windows） | `v0.25.1 beta` |
| 上游 Debian/Raspberry Pi OS/Linux | 按设备原版本单独报告 |

Edge OS、手机端和桌面端是独立产物；版本号不要求相同，通过 `config/version-catalog.json` 的兼容清单组成发布列车。

## 关键服务

| 能力 | 服务/入口 |
| --- | --- |
| 圆屏、菜单、表情和系统页 | `expression-display.service` |
| 语音助手 | `hermes-voice.service` |
| 回复语义表情桥 | `hermes-expression-bridge.service` |
| 六麦与硬件唤醒 | `listengo-mic.service` |
| 摄像头共享 | `camera-hub.service` |
| Hailo 人脸/视觉协调 | `riverbank-face-tracker.service` |
| 统一智能体协议与后端适配 | `riverbank-agent-runtime.service`、`riverbank.agent/v1` |
| 视频通话、Chat、账号与管理员网站 | `riverbank-video-call.service` |
| Chat 回复 | `riverbank-chat-worker.service` |
| 后台任务 | `riverbank-task-worker.service` |
| 具身智讯网页与受控补跑 | `paper-radar-web.service`、`paper-radar-catchup.timer` |
| 工坊 | `riverbank-workshop.service` |
| 健康守护 | `riverbank-health-monitor.service` |
| 一键恢复与日报补跑 | `riverbank-recovery.service` |
| 断网扫码配置 | `riverbank-provisioning.service` |

## 发布与完整性

正式修改完成后先运行仓库测试，再封存 Edge OS：

```bash
sudo riverbank-release seal \
  --version 0.26.6 \
  --channel beta \
  --notes "verified RiverBank Edge OS release"

sudo riverbank-release verify --json
```

当前 `v0.26.6 beta` 已于 2026-09-11 10:41（Asia/Shanghai）完成实机部署与封存：10 个组件、185 个关键文件，`drift_count: 0`。设备源码统一位于 `/home/geo/riverbank-edge-os`；表情与提示音等本地授权资产位于 `/home/geo/.local/share/riverbank/assets/`；日报状态位于 `RIVERBANK_DATA/paper-radar/`。封存后直接修改被清单覆盖的文件会产生发布漂移；不要用重新封存掩盖来源不明的改动。

## 当前正式能力摘要

- 圆屏表情、两级应用菜单、固定/取消固定、番茄钟、性能、音乐、通话、工坊和 SYSTEM 状态；
- 本地唤醒、双模型最终 ASR、RiverBank Agent Runtime（当前 Hermes/DeepSeek 适配器）、流式 TTS、语义表情和视觉路由；
- 多用户账号隔离的 Chat、附件、图片生成、后台任务、报告下载和管理员网站；
- 具身智讯通过 OAI-PMH 增量与统一 arXiv 访问闸门采集；元数据、HTML、PDF 共用单连接、4 秒最短间隔、当天缓存与持久化 429 冷却，每日最多初次运行加两次受控补跑；论文源异常时沿用最近成功论文区，产业资讯仍可独立更新；
- Camera Hub 单一摄像头持有、PipeWire 共享音频、WebRTC 双向音视频；
- 日报当天精读缓存优先，证据不足时按需补读原论文并回写当天缓存；
- 工坊使用有限声明式生成、可信验证、权限反推、物理审核、受控 Host API 和 `riverbank.surface/v1`；
- 工坊麦克风仅开放共享输入的音量指标，不开放原始 PCM 或直接 ALSA；
- 用户工坊应用、提案、授权、审计和私有数据不进入 GitHub 更新、Edge OS 发布或 OTA；
- 断网资源保护、扫码配网、一键恢复、日报补跑和发布完整性检查。

## 日常检查

```bash
systemctl --failed
sudo riverbank-release verify --json
systemctl is-active \
  expression-display.service \
  hermes-voice.service \
  riverbank-agent-runtime.service \
  riverbank-video-call.service \
  riverbank-workshop.service \
  riverbank-health-monitor.service
```

发生异常时先读取 SYSTEM 页和健康状态，再看对应服务日志；不要先重置用户数据或重新封存版本。

## 文档对应关系

| 桌面文件 | 仓库正式来源 |
| --- | --- |
| `RiverBank工坊能力边界与接口协议.md` | `docs/WORKSHOP_PROTOCOL.zh-CN.md` |
| `RiverBank系统维护说明.md` | `docs/MAINTENANCE.zh-CN.md` |
| `RiverBank-Tech-代码与维护索引.md` | `docs/MAINTENANCE.zh-CN.md` |
| `RiverBank-Tech-架构与实现说明.md` | `docs/ARCHITECTURE.zh-CN.md` |
| `RiverBank-Tech-标准部署说明.md` | `docs/DEPLOYMENT.zh-CN.md` |
| `RiverBank-Tech-系统部署说明.md` | `docs/DEVICE_STATUS.zh-CN.md` |
| `RiverBank批量部署待办.md` | `docs/FLEET-DEPLOYMENT-TODO.zh-CN.md` |
| `RiverBank Edge OS.md` | `docs/EDGE_OS.zh-CN.md` |
| `RiverBank-Agent-Runtime-协议.md` | `docs/AGENT_RUNTIME_PROTOCOL.zh-CN.md` |

桌面文件是便于现场查看的镜像副本；后续修改以仓库文件为准，再同步到设备桌面。
