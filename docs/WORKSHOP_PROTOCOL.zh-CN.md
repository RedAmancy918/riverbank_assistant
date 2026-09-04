# RiverBank 工坊：能力边界与应用接口协议

状态：`v1 beta`

清单版本：`riverbank.workshop/v1`

宿主协议：`riverbank.app-host/v1`

当前宿主基线：`RiverBank Edge OS v0.25.5 beta`

本文是工坊应用的安全与兼容性契约。清单校验、能力目录、逐请求授权、`.rbapp` 包完整性检查和事务化注册均由 `apps/workshop/` 中的实现执行；文档中的“必须”和“禁止”是实现约束，不是建议。

> **适用范围**：本协议是 RiverBank 设备上所有用户自定义应用的统一平台契约，适用于现在和未来由自然语言生成、开发者编写、本地导入或官方分发的应用。协议不绑定“小猫观察员”或任何具体业务；文中的小猫应用只是帮助理解摄像头与视觉能力的示例。任何新应用都必须经过同一套清单、签名、授权、资源和逐请求校验，不能因为生成来源或用途不同而绕过规则。

> **数据归属与 Git 边界**：工坊平台实现、协议、Schema、测试和明确标记的官方示例属于 RiverBank Edge OS 源码；用户通过语音或界面创建、导入、修改和授权的应用属于用户行为与设备数据。提案、`.rbapp` 包、安装目录、注册表、授权、审计记录及 `app-data` 必须保存在 `RIVERBANK_DATA/workshop/`，不得自动写回源码树，也不得进入 Git、GitHub CI/CD 附件、Edge OS 发布清单或 OTA。用户可以显式导出自己的应用用于备份或分享，但导出不等于提交到 RiverBank 官方仓库。

## 1. 目标与非目标

工坊允许用户用自然语言提出功能，再由系统生成、检查、安装和运行一个上层应用。应用可以组合 RiverBank 已有的圆屏、摄像头、麦克风、扬声器、Hailo 推理、云台、Daily、后台任务和报告库能力。

工坊不允许应用直接获得 Linux 主机控制权。自然语言中的“删除系统”“关闭安全检查”“执行 sudo”“读取所有文件”等表达不会扩大应用权限，也不能覆盖本文协议。

v1 的原则是：

1. 默认拒绝，应用只能调用已声明且已授权的宿主方法；
2. 身份由宿主绑定，应用不能在消息里伪造 `app_id`；
3. 硬件只有一个宿主持有者，应用通过代理和租约使用；
4. 声明、授权、运行是三个独立步骤；
5. 安装成功不等于允许运行，新应用默认是 `installed_disabled`；
6. 系统服务、凭据、宿主文件、Shell、内核和软件包管理永不开放；
7. 所有高风险调用都可以被宿主立即撤销、限速、暂停或急停。

## 2. 信任边界

```text
自然语言需求
    ↓ 只生成候选工程和权限理由
工坊生成器
    ↓ 只提交有限声明式计划；权限由可信代码反推
设备签名打包器
    ↓ manifest + app/ + checksums + Ed25519 signature
包检查器 / 策略检查器
    ↓ 验证通过，但仍是 disabled
用户授权页
    ↓ 生成设备侧 grant，应用不能写 grant
受限运行时
    ↓ 私有 Unix Socket；身份由进程凭据绑定
Host Broker
    ↓ 每一个请求重新做 declaration + grant + session 校验
Camera Hub / Hailo / PipeWire / Daily / Tasks / Reports / UI / Motor Broker
```

受信任计算基包括工坊验证器、Host Broker、系统服务、设备信任库和授权存储。应用包、应用代码、自然语言输入、网页内容、模型输出和外部 API 响应都按不可信输入处理。

## 3. 永不开放的能力

下列能力既不能写入清单，也不会出现在 Host API 中：

| 禁止项 | 说明 |
|---|---|
| `shell.execute` / `process.spawn` | 不提供 Shell、任意命令或子进程入口 |
| `service.manage` | 不能启停、修改或屏蔽 systemd 服务 |
| `system.power` | 不能直接关机、重启或修改电源策略 |
| `credentials.read/write` | 不能读取或覆盖 DeepSeek、DashScope、Tailscale 等密钥 |
| `filesystem.host` | 不能浏览用户主目录、SSD 任意路径或系统目录 |
| `packages.manage` | 不能调用 apt、pip、npm 或固件升级工具 |
| `kernel.configure` | 不能修改内核参数、驱动、设备树、网络栈或防火墙 |
| `network.raw` | 不能开监听端口、发原始包、扫描内网或绕过宿主代理 |
| 原始设备节点 | 不能打开 `/dev/video*`、ALSA、GPIO、串口、I²C、SPI 或 Hailo 设备节点 |

设备重启、系统恢复、OTA、配网和软件卸载继续由各自的受信任控制器处理。工坊应用只能在将来获得非常窄的业务级接口，不会拿到这些控制器的底层权限。

## 4. 可申请能力目录

风险级别只决定授权体验，不能替代逐请求校验。

| Capability | 风险 | 授权范围 | Host 方法 | 关键约束 |
|---|---:|---|---|---|
| `ui.surface` | 低 | 安装期 | `ui.present`、`ui.dismiss` | 只能提交受控视图树，不能创建任意桌面窗口 |
| `storage.app` | 低 | 安装期 | `storage.get/put/list/delete` | 路径被固定在应用私有目录，拒绝绝对路径和 `..` |
| `events.subscribe` | 低 | 安装期 | `events.subscribe/unsubscribe` | 只能订阅白名单、脱敏事件 |
| `notifications.local` | 中 | 安装期 | `notifications.show` | 宿主限频；不能伪装系统安全提示 |
| `camera.snapshot` | 中 | 会话期 | `camera.snapshot` | 需要近期用户操作；获取时亮隐私指示 |
| `camera.stream` | 高 | 会话期 | `camera.stream.open/close` | 前台、用户在场、TTL 租约、持续隐私指示 |
| `microphone.stream` | 高 | 会话期 | `microphone.stream.open/read/close` | 前台、用户在场、TTL 租约、持续录音指示、原始音频不落盘 |
| `speaker.playback` | 中 | 安装期 | `speaker.play/stop` | 只能经 PipeWire；清单限制最大音量 |
| `vision.inference` | 中 | 会话期 | `vision.detect` | 只调用宿主白名单模型和类别；不能上传帧 |
| `motor.pan_tilt` | 高 | 会话期 | `motor.move/stop` | 限角、限速、看门狗和宿主急停必须同时成立 |
| `assistant.query` | 高 | 会话期 | `assistant.query` | 使用 Daily 安全工具集、预算和速率限制 |
| `tasks.submit` | 中 | 安装期 | `tasks.create/status` | 复用持久任务队列；限制并发数 |
| `reports.read` | 中 | 安装期 | `reports.list/read` | 只读 Markdown 报告库；拒绝软链接和越界 |
| `network.outbound` | 高 | 安装期 | `network.fetch` | 仅 HTTPS；域名和 HTTP 方法双白名单；无原始 Socket |
| `lifecycle.autostart` | 高 | 安装期 | 无直接方法 | 必须单独声明和授权；仍受整机资源预算控制 |

能力目录的机器可读来源是 `workshop_contract.py`，`host-api-methods.json` 用于客户端代码生成和一致性检查。

## 5. 应用清单

每个应用包根目录必须包含 `manifest.json`。不含具体业务的起始模板位于 `apps/workshop/examples/minimal-app/`；下面的小猫清单仅用于展示多种硬件能力如何组合：

```json
{
  "apiVersion": "riverbank.workshop/v1",
  "kind": "RiverBankApp",
  "metadata": {
    "id": "tech.riverbank.catwatcher",
    "name": "小猫观察员",
    "version": "0.1.0",
    "description": "前台识别经过镜头的小猫。",
    "vendor": "RiverBank Local Workshop"
  },
  "spec": {
    "runtime": {
      "kind": "declarative-v1",
      "entrypoint": "app/main.json",
      "protocol": "riverbank.app-host/v1"
    },
    "permissions": [
      {
        "capability": "camera.stream",
        "reason": "在应用前台读取 Camera Hub 帧流。",
        "constraints": {"privacyIndicator": true}
      },
      {
        "capability": "vision.inference",
        "reason": "调用宿主目标检测模型识别 cat。",
        "constraints": {}
      }
    ],
    "resources": {
      "cpuPercent": 12,
      "memoryMB": 192,
      "storageMB": 128,
      "maxProcesses": 2
    },
    "lifecycle": {
      "autostart": false,
      "startTimeoutSeconds": 10,
      "stopTimeoutSeconds": 5
    },
    "ui": {"menuLabel": "小猫", "glyph": "猫"}
  }
}
```

### 5.1 标识与版本

- `metadata.id` 使用小写反向域名形式，安装后不可改变；
- `metadata.version` 使用 SemVer；
- `apiVersion` 和 `runtime.protocol` 必须精确匹配，不做静默降级；
- 菜单文字最多 6 个字符，图标文字最多 2 个字符；
- 清单编码后最大 64 KiB。

### 5.2 运行时

清单验证器识别：

- `declarative-v1`：入口为 `app/*.json`，用于常见传感器—推理—UI 流程；
- `python-sandbox-v1`：为未来兼容预留，当前运行器始终拒绝启动。

清单不能出现 `command` 或 `arguments`。应用不能决定解释器路径、宿主命令或 systemd 参数。

### 5.3 资源上限

单个应用清单允许的范围：CPU `1–50%`、内存 `16–512 MiB`、私有存储 `1–2048 MiB`、进程数 `1–8`。这是申请上限，宿主可以依据整机 20% 保留空间进一步下调。达到上限时先节流或终止应用，不能挤压语音、显示、健康监控、SSH 和恢复控制器。

### 5.4 意图编译与受控界面

工坊不把模型返回的 `view` 字符串当成已经实现的页面。生成器先把自然语言拆成“数据源、处理逻辑、输出行为、界面语义”，再交给可信验证器编译。`ui.present` 只能使用宿主公布的预置视图，或携带 `riverbank.surface/v1` 受控组件树；未知视图在打包前直接拒绝，禁止静默显示成通用“应用状态”页。

`riverbank.surface/v1` 当前提供 `hero`、`dashboard`、`list` 三种圆屏自适应布局，强调色只能从 RiverBank 设计令牌中选择；组件限制为最多 6 个，首批包括 `clock`、`metric`、`progress`、`status` 和 `text`。组件只能绑定声明式运行时上下文中的安全值，不能嵌入脚本、HTML、CSS、URL、绝对坐标或宿主路径。

例如，桌面时钟表达的是界面语义，而不是一段模型自行排版的文字：

```json
{
  "sink": "ui.present",
  "view": "clock",
  "title": "桌面时钟",
  "presentation": {
    "schema": "riverbank.surface/v1",
    "layout": "hero",
    "accent": "cyan",
    "components": [
      {
        "id": "local-time",
        "type": "clock",
        "format": "24h",
        "showSeconds": true,
        "showDate": true,
        "showWeekday": true
      }
    ]
  }
}
```

时间值由宿主注入并按设备时区更新；模型不需要也不能通过文本模板伪造系统时间。后续新增相机取景、图表、列表等组件时，必须同时交付协议验证、宿主渲染、生成器词表和回归测试，四者版本一致后才算平台能力。

## 6. 能力约束

### 6.1 摄像头和麦克风

连续媒体能力的清单必须包含 `privacyIndicator: true`。运行时还要求：

- 应用处于前台；
- 最近存在用户触摸或语音唤醒；
- Host Broker 已建立短 TTL 租约；
- 圆屏隐私点由宿主绘制，应用不能覆盖；
- 退出页面、锁屏、授权撤销、应用崩溃或租约超时立即关闭流；
- Camera Hub 常驻持有摄像头不等于应用正在使用摄像头，也不会点亮隐私指示。

麦克风适配器只通过用户 PipeWire 会话读取共享输入，不直接打开 ALSA，因此可以与常驻唤醒和视频通话共存。`declarative-v1` 当前开放 `microphone.stream` 数据源和 `audio.level` 运算节点，应用只会收到归一化 RMS、峰值、dBFS、采样率、窗口长度和序号；不会收到原始 PCM，也不能要求 `retainAudio: true`。麦克风单次租约为 1–300 秒，支持 16 kHz 或 48 kHz 单声道、20–500 ms 分析窗口。打开、持续读取和关闭均写入工坊审计，宿主看门狗在租约到期后独立回收采集进程。

### 6.2 云台

`motor.pan_tilt` 必须声明 `panDegrees`、`tiltDegrees`、`maxDegreesPerSecond` 和 `emergencyStop: true`。宿主同时执行机械限位、速度斜坡、命令超时、人物安全区和急停。应用发送越界目标时直接拒绝，不自动扩大范围。

### 6.3 网络

`network.outbound` 必须列出确切域名，不接受 `*`、IP、URL 或 CIDR。每次 `network.fetch` 只允许无内嵌凭据的 HTTPS URL，并再次核对目标域名和 `GET/HEAD/POST` 方法。DNS 解析、重定向和响应体大小仍由代理二次约束，避免 DNS rebinding、跳转到内网或无限下载。

### 6.4 存储

应用看到的是虚拟相对路径。宿主将路径映射到 `RIVERBANK_DATA/workshop/app-data/<app-id>/`，拒绝绝对路径、`..`、软链接和设备文件。安装包、授权记录、其他应用目录、照片库、音乐库和报告库不属于私有存储。

### 6.5 Daily 和后台任务

`assistant.query` 和 `tasks.submit` 当前都进入 RiverBank 的持久化后台任务服务，不在渲染线程或语音线程中同步运行 Hermes。任务服务使用 Daily Profile 的受控工具集，模型不能借此提升应用权限；模型回复里的工具建议仍要通过既有任务边界。工坊只收到任务 ID、状态、进度和报告标识，不会获得配对令牌。

### 6.6 圆屏语音创建

点击“创建应用”属于明确的物理交互：设备播放本地提示音后立即打开工坊录音窗，不再先播放联网 TTS 提问。工坊显式录音仍使用同一套 SenseVoice INT8 与 Zipformer CTC INT8 双模型路由，但采用独立的近场弱信号门槛；唤醒词入口继续保留更严格的弱信号拒绝策略，避免扩大误触发面。圆屏请求携带设备时间和请求 ID，超过 3 秒才到达语音主循环的积压请求会被丢弃，连续点击不能在上一轮结束后自动启动多轮录音。

工坊页面继续使用 4× 超采样抗锯齿。徽标和按钮渲染结果使用有上限的内存缓存，静态状态文件只有内容真正变化时才触发重绘；缓存只包含 UI 图形，不包含录音、转写文本或用户应用内容。

## 7. `.rbapp` 包格式

`.rbapp` 是 ZIP 容器，但只允许以下安全子集：

```text
example.rbapp
├── manifest.json
├── checksums.json
├── signature.json
├── app/
│   └── main.json 或 main.py
└── assets/
    └── ...
```

限制：压缩包不超过 64 MiB，解压后不超过 256 MiB，最多 512 个文件；不接受加密 ZIP、重复路径、绝对路径、`..`、软链接和特殊文件。

`checksums.json` 使用 `riverbank.workshop-checksums/v1`，必须用 SHA-256 完整覆盖除自身和 `signature.json` 外的全部普通文件。少一个、多一个或摘要不同都拒绝安装。

外部导入默认要求 `signature.json`：

```json
{
  "schema": "riverbank.workshop-signature/v1",
  "algorithm": "ed25519",
  "keyId": "riverbank-official-2026",
  "signature": "BASE64_ED25519_SIGNATURE"
}
```

签名对象是规范化后的 `checksums.json` 字节；公钥来自 `/etc/riverbank/workshop/trusted-keys/<keyId>.pem`。设备本地生成的开发包只有在用户显式进入开发流程时才可用 `--allow-unsigned-local` 导入，并仍保持 disabled；该开关不能由应用自身设置。

## 8. 安装事务和授权状态

导入顺序固定为：

1. 限制包大小和文件数；
2. 检查路径、软链接、重复项和压缩后总量；
3. 验证 SHA-256 完整覆盖；
4. 验证 Ed25519 和设备信任链；
5. 验证清单、入口文件、能力约束和资源上限；
6. 解压到 SSD 同一文件系统的随机 staging 目录；
7. 原子移动到 `packages/<app-id>/<version>/`；
8. 原子更新 `registry.json`；
9. 记录为 `installed_disabled`，所有权限 `granted: false`；
10. 用户逐项确认权限后，才允许 Host Runner 创建会话。

安装失败不能留下半安装记录。v1 不提供应用自删、覆盖安装或回滚系统组件的接口。应用升级必须使用新版本目录；授权变化由设备策略决定，不能由新包静默继承高风险权限。

### 8.1 当前审核主体（Edge OS v0.25.5 beta）

当前实现采用“设备本地、物理在场审核”，不是“提案发起者自动审核”，也不是“平台超级开发者逐个审核”：

- 语音或触摸发起者只能创建提案，生成完成后仍保持 `installed_disabled`，不能自行批准；
- 批准只能在设备圆屏的工坊审核页完成，远程任务提交、语音回复和生成模型都不能调用批准接口；
- 当前没有声纹、账户、PIN 或手机二次确认，因此系统不能证明圆屏操作者就是语音发起者、设备所有者或平台开发者；
- 在现阶段安全假设下，任何能够物理操作已解锁圆屏的人都可以批准应用，但只能授予清单展示且平台本身已经开放的能力；
- 审计日志记录提案、权限集合、时间和批准/拒绝结果，但当前不记录经过验证的自然人身份；
- Shell、系统服务、凭据、宿主文件、软件包、内核和直接设备访问属于平台禁区，不能通过圆屏批准，也不能因审核者是超级开发者而解锁。

因此，共享设备在引入身份系统前应被视为“物理接触即拥有应用审批权”。这适合单设备开发和受控家庭测试，不适合作为多用户设备的最终权限模型。

### 8.2 目标角色模型（尚未实现）

后续身份系统应把“谁提出需求”“谁允许这台设备使用能力”和“谁定义平台能力边界”拆成三个独立角色：

| 角色 | 可以做什么 | 不可以做什么 |
|---|---|---|
| 请求者 / 普通用户 | 创建提案、查看生成结果、运行已获授权的应用 | 不能批准中高风险权限、不能修改平台策略 |
| 设备所有者 / 管理员 | 通过本地 PIN、可信手机或账户批准具体应用；撤销授权、停用和卸载用户应用 | 不能开放平台禁区或绕过签名、清单和 Host Broker |
| 平台开发者 / 超级开发者 | 在正式系统版本中定义 capability、适配器、限额和威胁测试，并签署平台发布 | 不应代替设备所有者逐个批准家庭设备上的普通应用；不能通过应用审批绕过平台禁区 |

建议采用双门模型：平台开发者先在正式版本中决定某项能力是否存在及其硬边界，设备所有者再决定某个具体应用能否在自己的设备上获得该能力。两道门都通过才可运行。

| 风险级别 | 目标审核要求 |
|---|---|
| 低风险：前台 UI、应用私有存储、只读状态 | 圆屏确认；登录用户可与请求者相同 |
| 中风险：摄像头、联网、通知、后台任务、报告读取 | 设备所有者身份验证；建议圆屏确认后再用 PIN 或可信手机确认 |
| 高风险：麦克风、扬声器、云台、后台常驻、自启动 | 设备所有者强验证，并且相关 capability 已由平台开发者在签名版本中正式开放 |
| 平台禁区：Shell、系统服务、凭据、宿主文件、软件包、内核、直接设备节点 | 永不进入应用审批；只能通过受审查的系统升级改变平台本身 |

在角色模型正式实现前，界面和文档不得把“圆屏批准”描述为已验证的所有者审批，也不得允许远程客户端直接调用 approve。

## 9. Host API 传输与身份

当前启用的 `declarative-v1` 没有第三方进程：受信任解释器在进程内把当前应用 ID 绑定到每一次 Host Broker 调用，声明式 JSON 无法构造 Socket、进程或身份。工坊控制面使用本机私有 Unix Socket，并校验 peer credentials；它不暴露到局域网或 Tailscale。

未来若启用独立代码运行器，每个应用必须使用独立私有 Unix Socket 和 Linux peer credentials 延续同一身份规则。无论哪种运行时，请求中都禁止出现 `app_id` 或 `identity`；Host Broker 只信任运行会话绑定的真实身份。

### 9.1 请求

```json
{
  "protocol": "riverbank.app-host/v1",
  "type": "request",
  "id": "4d4c6d88-bc56-4a9c-a078-f10cbfdb87d8",
  "method": "vision.detect",
  "params": {
    "classes": ["cat"],
    "minimumConfidence": 0.55
  }
}
```

### 9.2 成功响应

```json
{
  "protocol": "riverbank.app-host/v1",
  "type": "response",
  "id": "4d4c6d88-bc56-4a9c-a078-f10cbfdb87d8",
  "result": {
    "detections": [{"class": "cat", "confidence": 0.91}]
  }
}
```

### 9.3 错误响应

```json
{
  "protocol": "riverbank.app-host/v1",
  "type": "response",
  "id": "4d4c6d88-bc56-4a9c-a078-f10cbfdb87d8",
  "error": {
    "code": "capability_not_granted",
    "message": "用户或设备策略尚未授予该能力。",
    "retryable": false
  }
}
```

### 9.4 事件

```json
{
  "protocol": "riverbank.app-host/v1",
  "type": "event",
  "id": "7bbaf271-1424-47bc-9ff1-236495365f4c",
  "event": "lifecycle.suspend",
  "data": {"reason": "permission-revoked"}
}
```

事件不代表授权。收到 `camera.available` 后，应用仍须发请求并通过本轮授权检查。

## 10. 每次调用的授权算法

Host Broker 对每个请求依次执行：

1. 协议、消息类型、ID、方法和参数结构正确；
2. 方法不属于永不开放前缀；
3. Socket 身份与清单应用 ID 相同；
4. 方法能映射到一个已知 capability；
5. 应用清单声明了 capability；
6. 设备授权记录授予了 capability；
7. 会话满足 foreground、user presence 和 TTL；
8. 参数满足清单约束与设备策略；
9. 资源配额和调用速率仍有余量；
10. 才把净化后的参数交给具体系统代理。

任一步失败都返回稳定错误码，且不能因为模型声称“用户已经同意”而跳过。

常用错误码：

| code | 含义 |
|---|---|
| `unsupported_protocol` | 协议版本不受支持 |
| `forbidden_capability` / `forbidden_method` | 永不开放的系统能力 |
| `capability_not_declared` | 清单未声明 |
| `capability_not_granted` | 用户或设备策略未授权 |
| `foreground_required` | 应用不在前台 |
| `user_presence_required` | 没有近期用户交互 |
| `identity_mismatch` / `identity_spoofing` | 运行身份不符或试图伪造身份 |
| `network_domain_denied` | 域名不在白名单 |
| `motor_limit_exceeded` | 云台目标超过限角 |
| `unsafe_path` | 路径越界 |
| `signature_required` / `untrusted_signature` | 缺少可信签名或验签失败 |
| `checksum_mismatch` | 包内容被修改 |

## 11. 生命周期

标准状态：

```text
candidate
  → validated
  → installed_disabled
  → permission_review
  → ready
  → starting
  → running
  ↔ suspended
  → stopping
  → stopped
```

崩溃、超限、撤权或协议违规进入 `quarantined`，不能自动重启。普通应用退出后可以重新进入；连续崩溃必须由用户检查。只有显式获得 `lifecycle.autostart` 的应用才可在开机后启动，而且必须晚于核心健康服务。

## 12. 运行器的系统级沙箱要求

`python-sandbox-v1` 真正启用前，运行器至少必须具备：

- 独立低权限 UID 或 `DynamicUser=yes`；
- `NoNewPrivileges=yes`；
- `ProtectSystem=strict`、只读根文件系统；
- 私有 `/tmp`、私有设备视图和受限 `/proc`；
- 默认无网络命名空间出口，联网只经 Host Broker；
- 不挂载 Docker Socket、systemd Socket、SSH Agent、用户 Home 或密钥目录；
- `DevicePolicy=closed`，不暴露摄像头、音频、串口、GPIO、I²C、SPI、Hailo；
- 按清单设置 CPU、内存、进程数、文件数和运行时间上限；
- stdout/stderr 限速、截断并禁止记录密钥和媒体内容；
- Host Broker 断开时应用自动停止。

Python 隔离运行器目前没有启用，所以 `python-sandbox-v1` 包即便通过清单检查也不能启动。`declarative-v1` 已由受信任的有限状态解释器执行；解释器只识别白名单 source/operator/sink，不执行导入、表达式、URL、路径、命令或脚本，也不会把 JSON 字段拼接成 Shell。

## 13. “小猫观察员”示例的实际边界

示例位于 `apps/workshop/examples/cat-watcher/`。它可以：

- 前台获取 Camera Hub 租约；
- 点亮绿色隐私指示；
- 调用宿主默认目标检测器且只请求 `cat` 类别；
- 在应用私有目录保存计数；
- 在圆屏显示状态和限频通知。

它不可以：

- 直接打开 `/dev/video0`；
- 把帧上传网络，因为没有 `network.outbound`；
- 保存照片，因为清单没有相册写入接口；
- 在退出页面后继续监控，因为没有 autostart 且要求前台；
- 控制云台，因为没有 `motor.pan_tilt`；
- 修改检测模型、安装依赖或停止 Camera Hub。

这正是工坊的目标：功能可以灵活组合，但权限和物理边界不会随自然语言一起扩张。

## 14. 当前正式链路与保留边界

设备端现已启用以下完整链路：

1. 语音中的明确“创建/开发一个应用”意图在进入开放式 Hermes 前被本地路由截获；
2. 工坊生成器只能使用 `clarify` 工具集，输出有限声明式计划，不能使用文件、终端或代码工具；
3. 可信验证器拒绝未知字段，并从节点反推唯一权限集合，模型不能自行扩权；
4. 设备使用本地 Ed25519 身份签包，随后按外部包同样的路径复检签名、摘要、清单和声明式入口；
5. 包先事务化安装为 `installed_disabled`，圆屏显示权限、理由与风险；当前由物理在场的圆屏操作者批准完全一致的权限集合后才写入 grant 并启用，尚未验证该操作者的账户角色；
6. 受信任声明式解释器只运行前台单应用；退出、租约到期、服务停止或错误都会回收视觉资源；
7. Host Broker 对每次调用重新执行 declaration + grant + session 校验，并写入追加式审计日志；
8. UI、私有存储、限频通知、Camera Hub 租约、PipeWire 麦克风音量分析、Hailo 白名单检测、后台任务与只读报告适配器已接通；Hailo 检测必须先向单一视觉协调器取得带 TTL 的互斥预约，不能与人脸 Worker 并行占用设备；
9. `riverbank-workshop.service`、运行状态与契约自检均纳入 SYSTEM 健康监控和整机发布清单。

仍然明确关闭：

- `python-sandbox-v1` 的代码执行；
- 自定义应用直接访问 Shell、systemd、凭据、宿主文件、软件包、网络 Socket 或设备节点；
- 扬声器和云台的工坊适配器，直到各自的音量租约、机械租约、隐私与物理急停验收完成；
- 自动后台常驻；生成应用默认只在用户打开的前台会话运行；
- 客户端上传第三方包后的圆屏授权流程，目前外部包只可由维护 CLI 验证和登记为 disabled。

因此“用户可以说出想法生成应用”现在指受控能力组合，不等于让模型在树莓派上任意写代码。扩展新的 source/operator/sink 或 Host 适配器必须先修改协议、授权器和威胁测试，再进入生成器词表。

## 15. 开发与验收

```bash
python3 apps/workshop/workshopctl.py self-test
python3 apps/workshop/workshopctl.py service-health
python3 apps/workshop/workshopctl.py microphone-smoke --seconds 1.5
python3 apps/workshop/workshopctl.py capabilities
python3 apps/workshop/workshopctl.py validate-manifest \
  apps/workshop/examples/cat-watcher/manifest.json
python3 -m unittest tests.test_workshop_contract tests.test_workshop_pipeline -v
```

设备上可提交一个提案并在圆屏审核：

```bash
python3 apps/workshop/workshopctl.py create \
  '创建一个识别路过小猫并计数的相机应用'
python3 apps/workshop/workshopctl.py proposals
```

外部包默认必须验签：

```bash
python3 apps/workshop/workshopctl.py inspect-package example.rbapp
python3 apps/workshop/workshopctl.py install example.rbapp
```

只有设备本地开发流程可显式使用：

```bash
python3 apps/workshop/workshopctl.py inspect-package \
  --allow-unsigned-local example.rbapp
```

测试必须覆盖拒绝系统能力、模型夹带未知字段、身份伪造、路径越界、通配域名、无隐私指示、云台越界、缺少授权、批准集合不一致、包摘要不匹配、入口声明无效、未知签名和半安装回滚。

## 16. 兼容性规则

- `apiVersion` 或 Host Protocol 的破坏性变化必须发布新主版本；
- 能力的约束只能收紧，放宽必须新增 capability 或协议版本；
- 未知 capability、方法、运行时或签名算法一律拒绝，不能忽略；
- 应用升级不能静默获得新权限；
- beta 到 stable 前要冻结错误码、消息帧和 `.rbapp` 签名对象；
- 旧应用可继续在旧协议运行时中运行，但不能自动迁移到更高权限协议。
