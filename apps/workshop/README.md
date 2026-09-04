# RiverBank Workshop

工坊 v1 的受控应用生成与运行层：

- `workshop_contract.py`：清单、能力目录、Host API 消息和逐请求授权；
- `workshop_manager.py`：`.rbapp` 完整性/签名检查、事务化解包和 disabled 注册；
- `workshop_generator.py`：把自然语言约束为有限声明式候选计划；
- `workshop_declarative.py`：白名单 source/operator/sink 语法与权限反推；
- `workshop_package.py`：设备 Ed25519 签名打包；
- `workshop_service.py`：提案队列、审批、安装、启动与审计；
- `workshop_runtime.py`：不执行第三方代码的声明式解释器；
- `workshop_host.py`：逐请求授权后的 UI、存储、相机、PipeWire 麦克风、Hailo、任务和报告适配；
- `workshopctl.py`：验证、检查、安装、提案、审批、运行与自检；
- `schemas/`：清单和 Host API JSON Schema；
- `host-api-methods.json`：方法到能力的机器可读映射；
- `examples/minimal-app/`：不含具体业务的通用起始模板。
- `examples/cat-watcher/`：前台小猫识别应用示例，仅用于演示能力组合。
- `examples/sound-meter/`：前台 PipeWire 麦克风音量分析与声音事件计数示例。

本目录定义所有用户自定义应用共用的平台契约，不属于任何单一应用。任何由自然语言生成、手工开发、本地导入或官方分发的应用都必须遵守同一套规则。

完整规范见 [`docs/WORKSHOP_PROTOCOL.zh-CN.md`](../../docs/WORKSHOP_PROTOCOL.zh-CN.md)。

当前只执行通过签名复检、明确审批的 `declarative-v1` 应用；它没有 Shell、导入、表达式、任意路径、直接网络或设备节点。`python-sandbox-v1` 继续保持禁用。语音生成不会直接安装：圆屏审核页批准与清单完全一致的权限集合后才启用。

界面由 `riverbank.surface/v1` 受控组件协议表达。生成模型选择功能与界面语义，宿主负责自适应圆屏排版；未知 `view` 会在打包前拒绝，不能再静默退化成“应用状态”。当前组件包括时钟、指标、进度、状态和文本，布局及颜色只能使用宿主白名单。

麦克风型应用使用 `microphone.stream -> audio.level` 声明式链路。宿主通过 PipeWire 共享默认输入，不抢占 ALSA；应用只能得到 RMS、峰值和 dBFS 等实时指标，原始 PCM 不进入应用状态、不写磁盘。会话仅在前台和近期用户操作后启动，带 1–300 秒租约，运行期间圆屏显示橙色麦克风隐私点，退出、超时、异常或服务停止都会关闭采集进程。

`v0.24.2 beta` 的批准者是物理操作本机圆屏的人；当前没有账户、声纹、PIN 或可信手机验证，不能证明其是提案发起者或设备所有者。目标方案采用普通请求者、设备所有者、平台开发者三层角色与“平台发布能力 + 所有者授权应用”双门。详细现状、风险级别和目标审核要求见 `docs/WORKSHOP_PROTOCOL.zh-CN.md` 的 8.1–8.2 节。
