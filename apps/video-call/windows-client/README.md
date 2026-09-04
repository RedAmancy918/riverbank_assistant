# RiverBank Call 桌面客户端

这里是 macOS 与 Windows 共用的 Electron 桌面客户端源码。目录名 `windows-client` 是早期命名，macOS 构建并不是另一份未上传的工程。

当前客户端版本为 `v0.25.1 beta`，包版本 `0.25.1-beta.1`。它是独立的配套客户端，不属于 Raspberry Pi OS 或 RiverBank Edge System；兼容的 Edge 版本范围由仓库根目录 `config/version-catalog.json` 管理。

## 开发运行

1. 安装 Node.js 20 或更新版本。
2. 在本目录运行 `npm ci`。
3. 运行 `npm start`。

默认连接 `https://riverbank-tech.tail0acdab.ts.net/assistant`。使用 RiverBank 用户名和密码
登录，服务端按用户隔离 Chat 与附件；会话由 macOS Keychain 或 Windows DPAPI 保存，
密码不落盘。设备第一次启用账号时额外填写一次性设备凭据，创建首位管理员。摄像头和
麦克风权限只在进入视频通话时请求。

顶部可在 Chat、“视频通话”和“任务报告”之间切换。Chat 第一版允许随消息上传 JPG/JPEG、PNG、WebP、PDF、Markdown 和 TXT；报告页从树莓派指定归档目录读取 Markdown，支持列表、内置预览、打开来源链接与使用系统保存窗口下载到本机。客户端不会浏览报告目录以外的文件，也不提供远程删除和修改。

## 打包 Windows 软件

在 Windows PowerShell 中运行：

```powershell
.\build.ps1
```

`dist` 目录会生成 `RiverBank-Call-Setup-...exe` 安装包和 `RiverBank-Call-Portable-...exe` 便携版。Electron 使用 Chromium 原生 WebRTC；媒体采用 DTLS-SRTP，信令、媒体和报告读取都只在局域网或 Tailscale tailnet 内传输。

当前 beta 构建未使用商业代码签名证书，首次打开时 Windows SmartScreen 可能要求手动确认。请只使用自己构建或从可信位置取得的文件，并核对 SHA-256。

## 打包 macOS 软件

在 Apple Silicon Mac 上运行：

```bash
npm ci
npm run dist:mac
```

`dist` 会生成 Apple Silicon 的 DMG 和 ZIP。应用图标使用 RiverBank 3×3 方块标识，应用包带摄像头、麦克风和本地网络权限说明，并使用本机 ad-hoc 签名；当前 beta 未做 Apple Developer ID 签名与公证，复制到其他 Mac 后首次启动可能需要右键选择“打开”。

## 源码仓库与安装包

Git 仓库包含两端共享的 `main.js`、`preload.js`、`renderer.js`、界面样式、图标、`package-lock.json` 和平台构建脚本。`dist/` 中生成的 Windows EXE、macOS DMG/ZIP 和 blockmap 不进入源码仓库。正式对外提供安装包时，建议上传至 GitHub Release，并同时发布 SHA-256；当前 beta 未签名或公证的构建只适合受信任设备测试。
