# RiverBank GitHub CI/CD

本仓库采用“独立产物版本、发布列车联合验收”的流水线。CI 可以验证全部代码；CD 只生成可审计的发布候选，不直接登录或覆盖用户的 RiverBank Edge。

## CI

`.github/workflows/ci.yml` 在 Pull Request、`main` 分支推送和手动触发时执行：

- 校验敏感信息、私网地址、JSON、Python/Shell 语法、命名与版本清单；
- 在 Ubuntu 运行 Edge、Chat、日报、工坊和可靠性单元测试；
- 在 GitHub macOS Runner 编译 iOS App，但不使用发行签名；
- 在 macOS 与 Windows Runner 构建 RiverBank Call 的 DMG/ZIP、安装版 EXE 和便携版 EXE；
- CI 构建保留 14 天，只作为测试产物，不自动进入 OTA 或正式商店。

建议在 GitHub 的 `main` 分支保护中要求以下检查通过：

- `Version, naming and release checks`；
- `Edge Python unit tests`；
- `iOS client build`；
- `macOS client build`；
- `Windows client build`。

同时开启“必须通过 Pull Request”“分支必须为最新”和“禁止强推”。

## CD 与标签

`.github/workflows/release.yml` 只接受带产物作用域、且与 `config/version-catalog.json` 完全一致的标签：

```text
suite/v0.25.3-beta
edge-system/v0.25.3-beta
ios/v0.25.1-beta
call/v0.25.1-beta
```

`suite/` 构建当前发布列车中的全部产物；其余标签只构建指定产物。裸标签 `v0.25.3` 会被拒绝，因为无法判断它代表硬件、Edge System 还是客户端。

发布流程会生成 GitHub Release、目标平台附件和 SHA-256 文件。当前 macOS、Windows 与 iOS beta 没有商业发行签名，iOS 文件会明确带 `unsigned`，不可描述成可公开安装的正式包。Edge 附件是源码发布候选，不会自动执行安装。

## GitHub 发布保护

在仓库 Settings → Environments 建立 `release` 环境，并配置：

1. Required reviewers 至少一名；正式阶段建议两名，其中一名负责安全/发布；
2. 只允许受保护标签进入；
3. 发布审批人与代码提交人分离；
4. 开启 GitHub Actions 的只读默认权限，只有 Release workflow 获得 `contents: write`；
5. Dependabot 自动维护 GitHub Actions 与 Electron npm 依赖。

基础 CI/CD 不需要保存设备密码、账号、模型密钥或 Tailscale 凭据。以后接入商业签名时，应使用单独的 GitHub Environment Secrets；OTA 根签名私钥不建议直接存入普通 GitHub Secret，优先使用云 KMS/HSM 与 GitHub OIDC 获取短期签名权限。

## 正式 OTA 的下一层

正式 OTA 应在 GitHub Release 候选之后增加独立的 Promotion 流程：

1. CI 生成不可变包、SHA-256、SBOM 和来源证明；
2. 人工验收真实 RiverBank Edge、iPhone、Mac 和 Windows 设备；
3. 公司签名服务签署 OTA 清单；
4. OTA 服务只分发签名清单与对象存储地址；
5. 用户设备验证产品型号、硬件 Revision、当前版本、目标版本、兼容范围、哈希与签名；
6. A/B 分区或可恢复快照安装，失败自动回滚；
7. 先内部、再灰度、最后全量，并保留远程停止发布开关。

这样 GitHub 负责源码、测试和发布候选，公司 OTA 负责最终信任与分发，用户账号和私人数据仍只保存在自己的设备上。
