# RiverBank Mobile

SwiftUI iOS 客户端，支持与 `riverbank-tech` 进行双向 WebRTC 音视频通话，以及提交持久化后台任务、处理 Agent 追问、取消任务、阅读和分享 Markdown 报告。关闭 App 不会终止树莓派上的后台任务。

## 构建

1. 安装 Xcode 16+ 与 XcodeGen；
2. 在本目录运行 `xcodegen generate`；
3. 打开 `RiverBankMobile.xcodeproj`；
4. 在 Signing & Capabilities 选择自己的 Apple Development Team；
5. 连接 iPhone 后运行 `RiverBankMobile` scheme。

版本显示为 `v0.20.0 beta`。App Store 的 `CFBundleShortVersionString` 保持三段数字，beta 通道在界面中单独标记。

仓库保存 Swift 源码、Asset Catalog、`project.yml` 和共享 Xcode 工程，不保存本机 `DerivedData`、开发证书、Provisioning Profile、`.app`、`.ipa` 或 `.xcarchive`。克隆后需要选择自己的 Apple Development Team；若要分发给其他设备，应通过 TestFlight、App Store 或签名后的独立 Release 流程，而不是提交本机编译目录。

## 连接

默认地址是：

```text
https://riverbank-tech.tail0acdab.ts.net/assistant
```

iPhone 需安装 Tailscale 并登录允许访问树莓派的 tailnet。首次进入“设置”输入视频通话服务使用的同一个配对令牌；令牌保存到 `kSecAttrAccessibleAfterFirstUnlockThisDeviceOnly` Keychain，不进入 UserDefaults、工程文件或日志。

如果 MagicDNS 暂时受本机 VPN 影响，也可以在可信局域网内临时改为 `http://树莓派局域网IP:19734`；正式使用仍推荐 Tailscale HTTPS。

## 接口

- `POST /api/v1/tasks`：幂等提交；
- `GET /api/v1/tasks`：任务列表；
- `GET /api/v1/tasks/{id}`：状态；
- `POST /api/v1/tasks/{id}/answer`：补充信息；
- `POST /api/v1/tasks/{id}/cancel`：取消；
- `GET /api/v1/reports`：报告库；
- `GET /api/v1/reports/{id}`：Markdown；
- `GET /api/v1/reports/{id}/download`：下载。
