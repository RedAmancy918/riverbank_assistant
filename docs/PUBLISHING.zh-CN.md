# GitHub 公开发布清单

## 许可证

源码已采用 Apache License 2.0，完整条款位于仓库根目录的 `LICENSE`。该许可证包含明确的版权许可、专利授权与专利终止条款。

RiverBank 名称、公司名称与 Logo 等品牌资产不随源码许可证开放一般使用，具体说明见 `LICENSE-NOTICE.md`。第三方组件、模型和素材仍分别受其原许可证约束。

## 发布前检查

```bash
scripts/validate-release.sh
git status --short
git diff --check
```

若该提交同时作为设备正式基线，还应先用 `apps/release-manager/release_manager.py` 封存并验证整机清单。Git 提交记录用于源码追踪，设备侧 SHA-256 清单用于发现已部署文件漂移，两者不能互相替代。

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
