# RiverBank Video Call

一对一 WebRTC 音视频端点与只读任务报告接口，面向同一局域网或同一 Tailscale tailnet 内的 iOS、macOS 与 Windows 客户端。

- 树莓派视频：Camera Hub `http://127.0.0.1:19733/stream`
- 树莓派音频：PipeWire 中的 ListenGo 六麦阵列
- 对端音频：通过 `pw-cat` 输出到系统默认扬声器
- 对端视频：保存在内存中，由圆屏通过本机快照接口读取
- 信令端口：`19734/tcp`
- 媒体：WebRTC DTLS-SRTP；不配置公网 STUN/TURN
- 安全：所有客户端必须提供至少 16 字符的配对令牌
- 报告库：只读取 Daily workspace 下的 `reports/`，不提供上传、编辑或任意文件浏览

## 客户端源码

| 平台 | 源码位置 | 本机构建产物 |
|---|---|---|
| iOS | `apps/ios/RiverBankMobile/` | `.app`、`.ipa`、`.xcarchive` |
| macOS | `apps/video-call/windows-client/` | `.dmg`、`.zip` |
| Windows | `apps/video-call/windows-client/` | 安装版和便携版 `.exe` |

macOS 与 Windows 使用同一套 Electron 源码。`windows-client` 是早期沿用的目录名，并不表示只有 Windows 版本。仓库只保存源码、锁定依赖、图标和构建脚本；`dist/`、Xcode `DerivedData/`、签名文件及平台安装包均被忽略。需要分发时，应由对应平台重新构建，或把校验过的二进制作为 GitHub Release 附件发布。

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
