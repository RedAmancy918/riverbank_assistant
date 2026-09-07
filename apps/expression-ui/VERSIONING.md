# RiverBank 命名与版本规范

RiverBank 不再用一个名称或版本号同时代表硬件、树莓派基础系统、设备软件和客户端。每个可独立安装、回滚或发布的产物都有固定名称与自己的版本；`config/version-catalog.json` 是当前发布组合的唯一清单。

## 正式名称

| 对象 | 正式对外名称 | 中文表述 | 代码/包标识原则 |
| --- | --- | --- | --- |
| 产品与品牌 | RiverBank | RiverBank 产品套件 | `riverbank` |
| 整机硬件 | RiverBank Edge | RiverBank Edge 整机/设备 | `riverbank-edge` |
| 设备操作系统 | RiverBank Edge OS | Edge OS | `riverbank-edge-os` |
| 上游基础发行版 | Debian / Raspberry Pi OS | 基础发行版 | 保留设备报告中的上游名称和版本 |
| iOS 客户端 | RiverBank | RiverBank iOS 客户端 | Bundle ID `com.riverbank.assistant`（保留已发布身份） |
| macOS/Windows 客户端 | RiverBank Call | RiverBank 桌面客户端 | 包名 `riverbank-call` |
| 设备内 AI 产品 | 小灰 | RiverBank 旗下智能产品“小灰” | 人格名称，不作为软件包版本主体 |
| 用户应用平台 | RiverBank Workshop | 工坊 | Host API `riverbank.app-host/v1` |
| 用户自定义应用 | 清单中的应用名 | 工坊应用 | 稳定、全局唯一的 application ID |

名称使用规则：

- `RiverBank Edge` 只指实体整机或具体设备，不指 iOS/macOS/Windows App；
- `RiverBank Edge OS` 指 RiverBank 维护、验证、签名并运行在整机上的 Linux 发行版/设备软件栈；
- Debian、Raspberry Pi OS、Linux 内核以及 Raspberry Pi 固件是 Edge OS 的上游组成，不用 RiverBank 版本号覆盖其来源版本；
- 当前 `v0.26.x beta` 的交付形态是安装在受支持上游系统上的受管系统层，不能称为“可直接烧录镜像”；具备可复现镜像、首启配置、签名和回滚后再发布镜像产物；
- “整机版本”在界面中拆成“硬件型号/Revision”和“Edge OS 版本”，不得只显示一个含混的版本；
- `RiverBank Assistant` 仅保留在历史记录中，新界面与新文档不再把它作为产品或客户端正式名称；
- 安装包使用 `产品名-版本-平台-架构`，例如 `RiverBank-Call-0.25.1-beta.1-mac-arm64.dmg`；
- systemd 服务和内部 Python 模块继续采用小写连字符或下划线技术名，不直接作为用户可见产品名。

机器可读命名统一为：产品/包 ID 使用小写 kebab-case，Python 模块使用 snake_case，Apple Bundle ID 使用反向域名且保留已发布身份，API Schema 使用 `riverbank.<domain>/vN`。Git 标签按独立产物加前缀，例如 `edge-os/v0.26.4-beta`、`ios/v0.25.2-beta`、`call/v0.25.1-beta`；禁止再使用无法判断目标产物的裸标签。历史 `edge-system/` 标签不改写，但不再用于新发布。量产设备型号建议使用 `RB-EDGE-G1`，硬件改版记为 `Rev A/B/...`，单机设备名使用 `RB-EDGE-<序列号后缀>`，不把主机名当产品型号。

## 版本层级

| 层级 | 如何编号 | 是否独立发布 |
| --- | --- | --- |
| RiverBank 产品套件 | 发布列车 `v0.26.4 beta` | 否，只描述一组经过验证的版本组合 |
| RiverBank Edge 硬件 | 产品代次 + 硬件 Revision | 是硬件身份，不使用软件 SemVer |
| RiverBank Edge OS | `vMAJOR.MINOR.PATCH beta|stable` | 是，当前可安装与封存；镜像发布后可整盘烧录与 A/B 回滚 |
| Debian / Raspberry Pi OS / Linux 内核 | 保留上游版本 | 是上游依赖，不纳入 RiverBank 版本号 |
| RiverBank iOS 客户端 | `vMAJOR.MINOR.PATCH beta|stable` + build | 是 |
| RiverBank Call 桌面客户端 | `vMAJOR.MINOR.PATCH beta|stable` + 构建号 | 是 |
| 工坊应用 | 每个应用独立 SemVer | 是，另声明 Host API 兼容性 |

因此，App 属于 RiverBank 产品套件中的“配套客户端”，不是 RiverBank Edge OS 的组成部分。App 随整机交付只是销售与分发关系，不改变它是独立软件产物这一事实。

## 什么叫版本对齐

对齐不是强迫各端使用同一个数字，而是固定一份不可变的兼容组合。例如，一个发布列车可以由 Edge OS `v0.26.4 beta`、iOS `v0.25.3 beta` 和桌面端 `v0.25.2 beta` 组成，只要清单声明兼容并通过联合验收。

当前清单记录：

- RiverBank 发布列车：`v0.26.4 beta`；
- RiverBank Edge OS：`v0.26.4 beta`；
- iOS 客户端：`v0.25.2 beta`，build 3；
- macOS/Windows 客户端：`v0.25.1 beta`；
- iOS 与桌面客户端兼容 Edge OS `>=0.25.0 <0.27.0`。

## 版本递增

- 主版本：存在不兼容的 API、数据或升级路径变化；
- 次版本：新增向后兼容能力；
- 修订号：兼容修复与优化；
- build：同一客户端版本的重新打包、签名或商店构建；
- `beta`：仍在迭代或硬件验证阶段；
- `stable`：联合回归、自检、升级和实际硬件验收均通过。

Edge OS 内部随整机发布的服务采用 lockstep：Hermes 接入、圆屏、日报、通话服务和工坊都记录为某个 Edge OS 版本的组成部分，不再对外伪装成彼此独立的产品版本。真正独立发布的工坊应用和客户端继续使用独立版本。

## 发布操作

1. 先修改 `config/version-catalog.json` 中目标产物的版本、通道、构建号和兼容范围。
2. 同步目标平台自己的原生版本入口。
3. 运行 `python3 scripts/versionctl.py check`，禁止清单和工程版本不一致。
4. Edge OS 运行完整测试后使用 release manager 封存 SHA-256 清单；客户端分别构建、签名并记录校验值。
5. 联合验收通过后，才把这组不可变版本标记为同一发布列车。

GitHub 标签与构建发布由 `.github/workflows/ci.yml`、`.github/workflows/release.yml` 自动执行，操作和审批要求见 `docs/CI_CD.zh-CN.md`。

所有 RiverBank 自有软件对外显示统一使用 `v主版本.次版本.修订号 beta|stable`。iOS、Windows 等平台要求的纯数字或预发布内部格式只用于包元数据，界面仍显示标准格式。读取旧格式时可兼容迁移；读取非法格式时显示 `v0.0.0 beta`。Linux 内核、Debian、Python、模型和其他外部依赖保留上游原始版本。
