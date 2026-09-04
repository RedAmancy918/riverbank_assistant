# RiverBank Mobile

SwiftUI iOS 客户端，支持与 `riverbank-tech` 进行持久化连续 Chat、双向 WebRTC 音视频通话，以及提交后台任务、处理 Agent 追问、取消任务、阅读和分享 Markdown 报告。Chat 采用会话列表、消息流、底部多行输入框、停止生成和失败重试式交互，并可上传照片、PDF、Markdown 和 TXT；照片在本机转为受支持的 JPEG 后再发送。关闭 App 不会丢失对话，也不会终止树莓派上的后台任务。

“新建任务”可选择文字、图片或图文三种交付成果。图片任务完成后可直接在任务详情预览并
保存 PNG；图文任务保留原始 Markdown，同时提供包含原创配图和完整排版的离线单文件报告，
可通过系统分享菜单保存、转发或打印成 PDF。

## 构建

1. 安装 Xcode 16+ 与 XcodeGen；
2. 在本目录运行 `xcodegen generate`；
3. 打开 `RiverBankMobile.xcodeproj`；
4. 在 Signing & Capabilities 选择自己的 Apple Development Team；
5. 连接 iPhone 后运行 `RiverBankMobile` scheme。

当前客户端版本显示为 `v0.25.1 beta`（build 2）。App Store 的 `CFBundleShortVersionString` 保持三段数字，beta 通道在界面中单独标记。客户端版本独立于 RiverBank Edge System，兼容关系由 `config/version-catalog.json` 管理。

仓库保存 Swift 源码、Asset Catalog、`project.yml` 和共享 Xcode 工程，不保存本机 `DerivedData`、开发证书、Provisioning Profile、`.app`、`.ipa` 或 `.xcarchive`。克隆后需要选择自己的 Apple Development Team；若要分发给其他设备，应通过 TestFlight、App Store 或签名后的独立 Release 流程，而不是提交本机编译目录。

## 连接

默认地址是：

```text
https://riverbank-tech.tail0acdab.ts.net/assistant
```

iPhone 需安装 Tailscale 并登录允许访问 RiverBank Edge 的 tailnet。App 首屏使用用户名和
密码登录；首次设置会额外要求一次性设备凭据并创建管理员。登录后签发的会话保存在
`kSecAttrAccessibleAfterFirstUnlockThisDeviceOnly` Keychain，密码不保存，也不进入
UserDefaults、工程文件或日志。每个用户只看到自己的 Chat 与附件。

账号密码和账号会话不允许通过远端明文 HTTP 使用。若要支持纯局域网，应先给 Edge Host
配置可信 HTTPS；正式使用推荐 Tailscale Serve HTTPS。

## 接口

- `GET /api/v1/auth/config`：首次设置状态；
- `POST /api/v1/auth/bootstrap`：创建首位管理员；
- `POST /api/v1/auth/login`、`GET /api/v1/auth/me`、`POST /api/v1/auth/logout`：账号会话；
- `GET/POST /api/v1/chats`：会话列表与新对话；
- `DELETE /api/v1/chats/{id}`：删除已停止的对话；
- `GET/POST /api/v1/chats/{id}/messages`：消息历史与新消息；
- `POST /api/v1/chats/{id}/messages/{message_id}/cancel`：停止生成；
- `POST /api/v1/tasks`：幂等提交；
- `GET /api/v1/tasks`：任务列表；
- `GET /api/v1/tasks/{id}`：状态；
- `GET /api/v1/tasks/{id}/artifact`：读取当前账号任务生成的图片或图文成果；
- `POST /api/v1/tasks/{id}/answer`：补充信息；
- `POST /api/v1/tasks/{id}/cancel`：取消；
- `GET /api/v1/reports`：报告库；
- `GET /api/v1/reports/{id}`：Markdown；
- `GET /api/v1/reports/{id}/download`：下载。
