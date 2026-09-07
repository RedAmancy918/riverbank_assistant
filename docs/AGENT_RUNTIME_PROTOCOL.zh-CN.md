# RiverBank Agent Runtime v1

本文定义 RiverBank Edge OS 中上层产品调用智能体的稳定边界。目标不是隐藏模型名称，而是让 Chat、后台任务、工坊、日报与语音交互不再依赖某一个 Agent 框架的命令行参数、Python 内部模块或会话存储格式。

当前协议版本为 `riverbank.agent/v1`，属于 Edge OS `v0.26.3 beta` 的系统接口。当前后端适配器是 Hermes；替换后端时，上层产品协议不变。

## 1. 架构边界

```text
Chat / Task / Workshop
          │  riverbank.agent/v1
          ▼
riverbank-agent-runtime.service
          │  受控适配器
          ▼
       Hermes CLI（当前）

Voice ASR/TTS ── EmbeddedAgent 接口 ── Hermes Python（当前低延迟适配器）
```

- `apps/agent-runtime/riverbank_agent/protocol.py`：稳定请求、结果、错误与校验规则；
- `client.py`：Unix Socket 客户端与显式应急直连传输；
- `hermes.py`：唯一允许把通用请求翻译成 Hermes CLI 参数的位置；
- `embedded_hermes.py`：语音低延迟常驻适配器，唯一允许导入 Hermes Python 内部模块的位置；
- `agent_runtime_service.py`：本机代理、并发控制、流式增量和取消；
- `/run/riverbank-agent/runtime.sock`：不对局域网或公网开放的本机入口。

语音暂时使用同一抽象的内嵌传输，以保留常驻模型、首字延迟和硬中断体验；它不经过 Socket 再启动 CLI。ASR、TTS、唤醒词、表情路由和本地设备命令都不接触后端实现。

## 2. 请求模型

客户端只能提交结构化字段，不能提交任意命令或额外命令行参数。`run` 请求包含：

| 字段 | 约束 |
|---|---|
| `schema` | 固定为 `riverbank.agent/v1` |
| `operation` | `run` |
| `requestId` | 服务内唯一安全标识 |
| `purpose` | `voice`、`chat`、`paper-qa`、`task`、`workshop`、`daily-report` |
| `workspace` | 服务启动时登记的逻辑名称，不接受任意路径 |
| `prompt` | 非空，最多 180,000 字符 |
| `toolsets` | 工具集名称列表；必须属于服务端白名单 |
| `reasoning` | `none` 至 `xhigh` 的受控枚举 |
| `maxTurns` | 1–64 |
| `timeoutSeconds` | 10–3,600 秒 |
| `source`、`session` | 受限安全标识；会话是否创建由布尔字段控制 |
| `imagePath` | 可选绝对路径，必须位于服务端登记的私有附件根目录 |
| `autonomy` | 只允许 `task` 与 `daily-report`；交互 Chat 和工坊禁止 |

服务按行返回 JSON 事件：`accepted`、`snapshot`、`final` 或 `error`。取消使用独立的 `cancel` 操作和同一个 `requestId`。错误码至少包括 `invalid_request`、`policy_denied`、`runtime_unavailable`、`backend_unavailable`、`backend_failed`、`timeout` 与 `cancelled`。

## 3. 安全策略

- Socket 只允许 root 或运行服务的同一 Unix UID；
- 逻辑工作区由服务端映射，客户端不能用 `../` 或绝对路径改变工作区；
- 工具集取服务端白名单与请求值的严格交集，出现未授权项直接拒绝；
- 图片必须真实存在，并位于 Chat 私有附件目录或 Daily Profile 的受信根目录；
- 只有受信后台任务能开启自治执行；
- 工坊仍只接收模型提出的声明式计划，最终权限由宿主反推并由设备所有者审批；Agent Runtime 不扩大工坊权限；
- 密钥仍只来自设备私有环境文件，不经过协议，也不写入日志、状态文件或 Git；
- Agent Runtime 不读取或提交 `RIVERBANK_DATA/workshop` 中的用户应用到源码仓库或 OTA。

## 4. 生命周期与降级

生产服务必须使用 `socket` 传输，并声明依赖 `riverbank-agent-runtime.service`。`direct` 只用于测试、迁移和人工应急，仍经过同一请求校验与 Hermes 适配器，不允许上层恢复自行拼命令。

Runtime 退出时会取消其所有子进程；客户端断线不会自动把一个自治任务变成未知命令。Task Store 与 Chat Store 继续负责持久状态，服务重启后由 Worker 按原有恢复规则处理。Runtime 自身不保存 Chat 历史、用户附件、任务正文或工坊应用。

健康检查：

```bash
python3 apps/agent-runtime/agentctl.py health
```

返回必须同时满足 `ok=true`、`backendAvailable=true`、协议为 `riverbank.agent/v1`。SYSTEM 页通过健康守护器显示服务和后端两项状态。

## 5. 替换 Hermes 的规则

新增后端时实现与 `HermesCLIAdapter.run()` 等价的适配器：接收已经校验的 `AgentRunRequest`，流式发布完整快照，响应取消并返回纯文本。之后只在 Agent Runtime 启动配置中选择后端，不修改 Chat、Task、Workshop、ASR/TTS 或 UI。

替换前必须通过以下兼容验证：协议往返、工具白名单、工作区隔离、图片根目录拒绝、流式快照、超时、远端取消、Chat 连续会话、Task 自治报告、工坊 JSON 修复和语音打断。计划任务调度仍由设备恢复控制器管理，当前使用 Hermes Cron 的持久账本；它是下一阶段独立的 `riverbank.scheduler/v1` 迁移对象，不与对话执行协议混在一起。
