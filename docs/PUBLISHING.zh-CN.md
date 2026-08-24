# GitHub 公开发布清单

## 必须先决定：许可证

当前包没有 `LICENSE`，因为源码权利人尚未指定许可条款。没有许可证的 GitHub 仓库默认并不允许他人复制、修改和再分发，因此在设为 Public 前必须处理。

常见选择：

- MIT：非常简短、宽松，保留版权与免责条款。
- Apache-2.0：同样宽松，并明确包含专利授权与专利终止条款。
- GPL-3.0：要求分发修改版本时继续以相同许可证开放源码。

如果代码归公司所有，应由公司确认版权主体、贡献者协议和许可证。源码许可证不必自动授权 RiverBank 名称与 Logo。

## 发布前检查

```bash
scripts/validate-release.sh
git status --short
git diff --check
```

确认下列内容不存在：

- `.env`、API Key、access token、密码或 Cookie；
- `.hermes/`、`auth.json`、Profile、聊天历史和 SQLite 数据库；
- 摄像头照片、录音、声纹、日志与设备唯一标识；
- 私有 IP、Tailscale 地址、SSH 配置和 VPN 登录数据；
- HEF、ONNX、PyTorch 权重、第三方 `.so` 与未知授权素材；
- 微软雅黑等不可再分发字体；
- 生成的论文数据库、历史报告与抓取缓存。

## 建议的首次提交

```bash
git init
git add .
git commit -m "Initial open-source release"
git branch -M main
git remote add origin git@github.com:YOUR_ORG/riverbank-edge.git
git push -u origin main
```

上述命令中的组织和仓库名由所有者自行替换。第一次推送前，再用 GitHub 的 secret scanning 或同类工具复核完整 Git 历史。
