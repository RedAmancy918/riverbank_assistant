# RiverBank Video Call

面向同一局域网或同一 Tailscale tailnet 内 iOS、macOS 与 Windows 客户端的连续 Chat、一对一 WebRTC 音视频端点与只读任务报告接口。

- 树莓派视频：Camera Hub `http://127.0.0.1:19733/stream`
- 树莓派音频：PipeWire 中的 ListenGo 六麦阵列
- 对端音频：通过 `pw-cat` 输出到系统默认扬声器
- 对端视频：保存在内存中，由圆屏通过本机快照接口读取
- 信令端口：`19734/tcp`
- 媒体：WebRTC DTLS-SRTP；不配置公网 STUN/TURN
- 安全：所有客户端必须提供至少 16 字符的配对令牌
- 报告库：只读取 Daily workspace 下的 `reports/`，不提供上传、编辑或任意文件浏览
- Chat：独立 SQLite 历史库和独立 `riverbank-chat-worker.service`，通过命名 Hermes Daily 会话保持上下文，不阻塞长任务 Worker；支持受控图片和文档附件

## Chat

Chat 历史保存在 `/var/lib/riverbank-tasks/chat.db`。API 服务只负责鉴权、会话和消息读写；独立 Worker 从队列领取消息并通过 `HERMES_HOME=/home/geo/.hermes/profiles/daily` 调用 Hermes。普通 Chat 仅启用浏览、搜索和技能工具，不开放文件删除或系统修改；耗时调研与文件整理继续使用“任务”页面。

- `GET/POST /api/v1/chats`：列出或创建会话；
- `DELETE /api/v1/chats/{conversation_id}`：删除无活动回复的会话；
- `GET/POST /api/v1/chats/{conversation_id}/messages`：读取消息或提交一轮；
- `POST /api/v1/chats/{conversation_id}/messages/{message_id}/cancel`：请求停止当前回复。

桌面 Chat 可随消息上传 JPG/JPEG、PNG、WebP、PDF、Markdown、TXT 和 CSV。
附件保存在 `/var/lib/riverbank-tasks/chat-attachments/`，仅能通过配对鉴权接口读取，
不进入 Web 静态目录，也不会作为程序执行。单条消息最多 4 个附件、最多 1 张图片，
单文件上限 15 MB、总上限 30 MB。图片以 Hermes 的 `--image` 参数交给已配置的
Qwen 辅助视觉模型；文本与可提取文本的 PDF 作为本轮参考材料交给 Daily Assistant。
删除对话时同步删除该对话的附件文件。

- `GET /api/v1/chats/{conversation_id}/attachments/{attachment_id}`：鉴权读取或下载附件。

Paper Radar 的逐篇文章问询复用同一个 Worker 和数据库，但通过
`source=paper-radar-internal` 隔离。Worker 会按会话的稳定论文 ID 读取
`/home/geo/paper-radar/data/paper-qa/current.json`，只检索当天缓存中的证据，
并禁用联网检索；证据不足时必须明确说明。普通 `GET /api/v1/chats` 默认
隐藏这些内部会话，只有诊断调用显式添加 `include_internal=1` 才会返回。

## 客户端源码

| 平台 | 源码位置 | 本机构建产物 |
|---|---|---|
| iOS | `apps/ios/RiverBankMobile/` | `.app`、`.ipa`、`.xcarchive` |
| macOS | `apps/video-call/windows-client/` | `.dmg`、`.zip` |
| Windows | `apps/video-call/windows-client/` | 安装版和便携版 `.exe` |

macOS 与 Windows 使用同一套 Electron 源码。`windows-client` 是早期沿用的目录名，并不表示只有 Windows 版本。仓库只保存源码、锁定依赖、图标和构建脚本；`dist/`、Xcode `DerivedData/`、签名文件及平台安装包均被忽略。需要分发时，应由对应平台重新构建，或把校验过的二进制作为 GitHub Release 附件发布。

桌面客户端每次启动先显示设备登录页，使用 `RiverBank Edge Host` 与配对令牌
调用鉴权状态接口；成功后隐藏登录凭据并进入主界面。右上角“登出”会结束当前
通话、清除本机保存的配对令牌并返回登录页。摄像头和麦克风不在应用启动时探测，
只有用户首次进入“视频通话”时才请求权限和枚举设备，避免纯 Chat 场景被无关的
`Requested device not found` 错误干扰。

## 本地控制

```bash
python3 video_callctl.py status
python3 video_callctl.py activate
python3 video_callctl.py hangup
```

`/healthz` 只返回最少健康状态；完整状态和信令接口需要配对令牌，远端视频快照仅允许 `127.0.0.1` 访问。

## 任务报告库

Daily Assistant 把用户明确要求整理、研究并保存的 Markdown 任务写入：

```text
/home/geo/.hermes/profiles/daily/workspace/reports
```

服务只收录该目录中的 `.md` 和 `.markdown`，忽略隐藏文件、软链接和其他格式，并拒绝越界路径。桌面端通过以下配对接口读取：

- `GET /api/v1/reports`：报告清单；
- `GET /api/v1/reports/{id}`：Markdown 正文；
- `GET /api/v1/reports/{id}/download`：下载原文件。

可以通过 `--reports-dir` 或 `RIVERBANK_REPORTS_DIR` 改变根目录。接口保持只读，报告创建仍由 Daily Assistant 的文件工具负责。
