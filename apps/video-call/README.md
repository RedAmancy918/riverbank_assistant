# RiverBank Video Call

面向同一局域网或同一 Tailscale tailnet 内 iOS、macOS 与 Windows 客户端的连续 Chat、一对一 WebRTC 音视频端点与只读任务报告接口。

- 树莓派视频：Camera Hub `http://127.0.0.1:19733/stream`
- 树莓派音频：PipeWire 中的 ListenGo 六麦阵列
- 对端音频：通过 `pw-cat` 输出到系统默认扬声器
- 对端视频：保存在内存中，由圆屏通过本机快照接口读取
- 信令端口：`19734/tcp`
- 媒体：WebRTC DTLS-SRTP；不配置公网 STUN/TURN
- 安全：远端客户端使用本地账号密码登录与可撤销会话；旧配对令牌只用于首次建管理员和本机服务
- 报告库：只读取 Daily workspace 下的 `reports/`，不提供上传、编辑或任意文件浏览
- Chat：独立 SQLite 历史库和独立 `riverbank-chat-worker.service`，通过命名 Hermes Daily 会话保持上下文，不阻塞长任务 Worker；支持受控图片和文档附件，以及 Qwen Image 文生图
- 后台任务：支持 Markdown 文字、PNG 图片和“Markdown 正文 + 原创配图”的离线单文件图文报告

## Chat

Chat 历史保存在 `/var/lib/riverbank-tasks/chat.db`，账号保存在
`/var/lib/riverbank-tasks/auth.db`。服务端以 `owner_user_id` 强制隔离会话、消息和附件，
不能依赖客户端隐藏。API 服务只负责鉴权、会话和消息读写；独立 Worker 从队列领取消息并
通过 `HERMES_HOME=/home/geo/.hermes/profiles/daily` 调用 Hermes。普通 Chat 仅启用浏览、
搜索和技能工具，不开放文件删除或系统修改；耗时调研与文件整理继续使用“任务”页面。

- `GET/POST /api/v1/chats`：列出或创建会话；
- `DELETE /api/v1/chats/{conversation_id}`：删除无活动回复的会话；
- `GET/POST /api/v1/chats/{conversation_id}/messages`：读取消息或提交一轮；
- `POST /api/v1/chats/{conversation_id}/messages/{message_id}/cancel`：请求停止当前回复。

三端 Chat 第一版统一支持 JPG/JPEG、PNG、WebP 图片、PDF、Markdown 和 TXT；不接收 CSV、
Office 文档、压缩包、程序或其他格式。
附件保存在 `/var/lib/riverbank-tasks/chat-attachments/`，仅能由附件所属用户的账号会话读取，
不进入 Web 静态目录，也不会作为程序执行。单条消息最多 4 个附件、最多 1 张图片，
单文件上限 15 MB、总上限 30 MB。图片以 Hermes 的 `--image` 参数交给已配置的
Qwen 辅助视觉模型；文本与可提取文本的 PDF 作为本轮参考材料交给 Daily Assistant。
删除对话时同步删除该对话的附件文件。

- `GET /api/v1/chats/{conversation_id}/attachments/{attachment_id}`：鉴权读取或下载附件。

明确的文生图请求（如“帮我生成一张……”）由 Chat Worker 直接路由到阿里云中国区
DashScope 原生多模态接口，默认模型为 `qwen-image-3.0-pro`，不再交给普通文字模型假装
完成。返回的临时 OSS 地址不会写进消息：Worker 会立即下载并校验图片，将其保存为该账号
私有的 assistant 附件，再把消息标为完成。因此临时链接过期、App 重开或换设备登录同一
账号后，图片仍可显示和下载；其他账号仍无法读取。运行环境需要
`DASHSCOPE_API_KEY`，可通过 `DASHSCOPE_IMAGE_BASE_URL`、
`RIVERBANK_IMAGE_GENERATION_MODEL` 和 `RIVERBANK_IMAGE_GENERATION_SIZE` 覆盖端点、模型与
尺寸。视觉理解继续走 Hermes 配置的 Qwen VLM，文生图与看图是两条独立能力。

Paper Radar 的逐篇文章问询复用同一个 Worker 和数据库，但通过
`source=paper-radar-internal` 隔离。Worker 会按会话的稳定论文 ID 读取
`/home/geo/riverbank-edge-os/apps/paper-radar/data/paper-qa/current.json`，优先检索当天精读缓存。具体型号、参数、
实验设置或“进一步确认”这类问题若未被缓存覆盖，Worker 会调用受控补证器读取该论文自身的
arXiv PDF，失败时降级为 arXiv HTML，并将新增分块原子写回同一个当日缓存；不会开放普通
网页搜索给论文回答，也不会把模型常识冒充论文证据。补证仅在同一篇论文当天首次需要时
执行，后续问题直接复用；下一次成功日报会整体替换知识缓存，对话记录继续保留。普通
`GET /api/v1/chats` 默认
隐藏这些内部会话，只有诊断调用显式添加 `include_internal=1` 才会返回。

## 客户端源码

| 平台 | 源码位置 | 本机构建产物 |
|---|---|---|
| iOS | `apps/ios/RiverBankMobile/` | `.app`、`.ipa`、`.xcarchive` |
| macOS | `apps/video-call/windows-client/` | `.dmg`、`.zip` |
| Windows | `apps/video-call/windows-client/` | 安装版和便携版 `.exe` |

macOS 与 Windows 使用同一套 Electron 源码。`windows-client` 是早期沿用的目录名，并不表示只有 Windows 版本。仓库只保存源码、锁定依赖、图标和构建脚本；`dist/`、Xcode `DerivedData/`、签名文件及平台安装包均被忽略。需要分发时，应由对应平台重新构建，或把校验过的二进制作为 GitHub Release 附件发布。

桌面客户端日常只显示用户名和密码，并提供登录/注册切换；已保存的 `RiverBank Edge Host` 收在折叠的“连接设置”
中。首次注册用户名固定为 `Geo`，使用设备注册通行码后成为管理员并接管旧 Chat 与任务；后续注册账号默认为普通用户。会话由 macOS Keychain 或 Windows DPAPI 安全保存；密码
不保存。管理员可在账号面板创建和停用用户。右上角“登出”会结束当前通话、撤销并清除
本机会话。摄像头和麦克风不在应用启动时探测，
只有用户首次进入“视频通话”时才请求权限和枚举设备，避免纯 Chat 场景被无关的
`Requested device not found` 错误干扰。

## 本地控制

```bash
python3 video_callctl.py status
python3 video_callctl.py activate
python3 video_callctl.py hangup
```

`/healthz` 只返回最少健康状态；完整状态和信令接口需要有效会话或受限的内部服务凭据，
远端视频快照仅允许 `127.0.0.1` 访问。账号与隔离契约详见
`docs/AUTHENTICATION.zh-CN.md`。

## 管理后台

管理员远程访问
`https://riverbank-tech.tail0acdab.ts.net/assistant/admin/`，树莓派本机排障可访问
`http://127.0.0.1:19734/admin/`。后台可以查看用户的登录设备数、Chat、
附件占用和任务统计，设置管理员权限、停用/恢复账户、撤销全部会话，以及分别清理 Chat
缓存或已结束任务。普通用户即使直接访问管理 API 也会收到 403；最后一名可用管理员不能被
停用或降级。后台令牌只保存在当前浏览器标签页，不持久化到磁盘。

## 任务报告库

Daily Assistant 把用户明确要求整理、研究并保存的 Markdown 任务写入：

```text
/home/geo/.hermes/profiles/daily/workspace/reports
```

服务只收录该目录中的 `.md` 和 `.markdown`，忽略隐藏文件、软链接和其他格式，并拒绝越界路径。桌面端通过以下鉴权接口读取：

- `GET /api/v1/reports`：报告清单；
- `GET /api/v1/reports/{id}`：Markdown 正文；
- `GET /api/v1/reports/{id}/download`：下载原文件。

可以通过 `--reports-dir` 或 `RIVERBANK_REPORTS_DIR` 改变根目录。接口保持只读，报告创建仍由 Daily Assistant 的文件工具负责。

任务创建接口的 `output_format` 接受 `text`、`image`、`illustrated`。`text` 生成
Markdown；`image` 通过 Qwen Image 生成 PNG；`illustrated` 先由 Hermes 完成并核实
Markdown 正文，再生成配图并封装成可离线打开、可打印为 PDF 的单文件 HTML。原始
Markdown 始终保留在报告库，方便继续编辑和检索。图片与图文成果保存在
`/var/lib/riverbank-tasks/artifacts/<task-id>/`，不进入静态目录，且只有任务所属账号能通过
`GET /api/v1/tasks/{id}/artifact` 读取。终端可用
`riverbank_task.py submit --output-format text|image|illustrated` 选择成果类型，并以
`download --task` 下载主要成果文件。
