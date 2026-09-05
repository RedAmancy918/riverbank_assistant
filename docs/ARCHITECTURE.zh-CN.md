# 架构与实现说明

当前架构基线：**RiverBank Edge OS v0.26.1 beta**，分发模式为 `managed-linux-system-layer`。可烧录系统镜像与 A/B OTA 是下一阶段交付能力，见 `docs/EDGE_OS.zh-CN.md`。

## 1. 服务边界

RiverBank Edge 采用多个小型 systemd 服务，而不是一个持有全部硬件和业务状态的单体程序。服务之间通过本机 HTTP、Unix Socket 和只读状态文件通信。

| 模块 | 入口 | 对外契约 |
|---|---|---|
| Camera Hub | `camera-hub.service` | `:19733/stream`、`/snapshot`、`/state` |
| ListenGo | `listengo-mic.service` | `/run/listengo-mic/control.sock`、`state.json` |
| Daily Voice | `hermes-voice.service` | `/run/hermes-voice-control/control.sock`、`state.json` |
| Expression UI | `expression-display.service` | `/run/riverbank-expression/control.sock`、`state.json` |
| Face Tracker | `riverbank-face-tracker.service` | `/run/riverbank-face-tracker/control.sock`、`state.json`、TTL 租约 |
| Health Monitor | `riverbank-health-monitor.service` | `/var/lib/riverbank-health-monitor/status.json` |
| Recovery Controller | `riverbank-recovery.service` | `/run/riverbank-recovery/control.sock`、`status.json` |
| Offline Provisioning | `riverbank-provisioning.service` | `:19735`、`/run/riverbank-provisioning/status.json` |
| Paper Radar | `paper-radar-web.service`、`paper-radar-catchup.timer` | `:19732` 网页与焦点输入 API、受控补跑 |
| Agent Runtime | `riverbank-agent-runtime.service` | `/run/riverbank-agent/runtime.sock`、`riverbank.agent/v1` |
| Background Tasks | `riverbank-task-worker.service` | `:19734/api/v1/tasks`、`/var/lib/riverbank-tasks/tasks.db`、私有成果目录 `artifacts/<task-id>/` |
| Workshop | `riverbank-workshop.service` | `/run/riverbank-workshop/control.sock`、`riverbank.workshop/v1`、`riverbank.app-host/v1`、`.rbapp` |

运行时文件只包含经过筛选的状态，不应写入密钥、完整语音、模型回复历史或摄像头原始流。

Chat、后台任务与工坊只依赖 `riverbank.agent/v1`，不直接调用 Hermes 命令或导入 Hermes 内部模块。当前 `hermes-cli` 与低延迟语音内嵌适配器都收口在 Agent Runtime；未来替换为其他 agent，只需实现同一协议的后端适配并通过契约、取消、并发和恢复测试。设备本地的 Profile、日报、对话、任务与工坊用户数据不因适配器替换而迁入 Git 或公司云端。

工坊有两类必须分开的资产：平台代码、协议、Schema、测试和官方示例属于 Edge OS；用户提案、生成或导入的 `.rbapp`、安装目录、注册表、授权、审计和应用私有数据属于设备用户。后者只写入 `RIVERBANK_DATA/workshop/`，不回写源码树，也不进入 GitHub Release、Edge OS 发布清单或 OTA。

## 2. 摄像头单一持有

USB 摄像头由 `ustreamer` 单独持有。其他服务读取本地 MJPEG 流或单帧接口，从而避免多个 OpenCV/GStreamer 进程同时打开 `/dev/video0`。

- 圆屏相机模式：按刷新节奏获取 `/snapshot`，退出后停止主动取帧。
- 视觉问答：Daily Voice 在识别到视觉意图时保存一张临时帧给 Hermes。
- Hailo 视觉运行时采用单一所有者架构：`face_tracker_supervisor.py` 作为轻量控制进程常驻，但不打开 Hailo；只有人脸跟踪、云台跟随或明确视觉任务持有有效 TTL 租约时才启动隔离的 SCRFD Worker。工坊 YOLO 与人脸 Worker 通过同一控制端互斥预约，切换时必须等旧 Worker 完整退出后才交出设备；空闲时不存在 Hailo 推理子进程或已加载 HEF。调用方失联后预约自动到期，协调器恢复空闲状态。
- 相册：拍照时保存高质量 JPEG 到 `RIVERBANK_DATA/camera/gallery`。

相机常驻的目的只是避免硬件抢占和冷启动；隐私指示灯只应表示用户可感知的主动视觉任务，而不是 Camera Hub 自身。

## 3. 圆屏渲染

`expression_display_persistent.py` 创建一次无边框窗口并持续复用。`animation_assets.py` 负责 GIF、抠色与视口准备，`system_status.py` 提供类型化只读状态快照；GIF 解码、背景处理、4× 超采样抗锯齿、状态栏组件和部分过渡帧采用预加载或有上限缓存，减少切换时桌面闪现和主线程卡顿。表情素材默认按 60 FPS 插值解码，交互合成跟随约 63 Hz DSI 刷新；动画缓存上限为 2048 MB，常用表情常驻，罕用表情按 LRU 回收。番茄钟与菜单仍使用稀疏预热和有界缓存，避免非视觉质量相关的内存增长。渲染器提供 `--self-test`，可在不开窗口时检查模块、素材、800×800 目标尺寸和版本格式。

主要状态包括：

- 表情待机与触摸表情；
- 长按后至少保留 2 秒的六项环形菜单；
- 左右两个状态胶囊；
- 从环形菜单进入的设置页，自动刷新 Wi-Fi、蓝牙、软件版本与系统自检信息，支持 Wi-Fi 开关；表情与设置、设置首页与详情之间统一使用缓存横向过场，并支持按钮或左向右滑动返回；
- 原“休眠”入口替换为番茄钟：默认 25 分钟专注、5 分钟短休、每完成 4 轮进入 15 分钟长休；倒计时以确定性圆弧呈现，专注使用番茄红、休息使用叶片绿，支持开始/暂停、重置、跳过和右滑返回；专注、短休和长休在待开始或暂停时都可长按右上角秒表表冠，主圆环收缩并展开 72 条高密度环形刻度，指针角度连续跟随手指，时间以 1 分钟步长吸附在 1–180 分钟，长按后的拖动死区仅保留 3 px 触摸防抖，手指附近约 20 条刻度使用余弦包络从最短连续过渡到最长；松手后为当前阶段保存一次性时长但不自动开始，阶段结束后清除并恢复默认值，休息阶段的自定义时长不得把状态切回专注；右上角统计按钮进入专注统计，今日环图与近 7 天柱形趋势分别渲染在两个预缓存页面，通过左右滑动切换；Daily 语音助手通过本地快速通道创建和控制番茄钟，不经过大模型，并支持 1–180 分钟的一次性专注时长；
- 应用二级菜单提供“性能”应用：`performance_monitor.py` 只从 procfs、sysfs、NVMe 文件系统和健康状态快照读取指标，CPU 与网络采用相邻采样差值计算；独立单线程执行器每秒异步采样，渲染线程只消费不可变快照，因此监控采样不得阻塞圆屏动画；页面关闭后停止周期采样；页面显示 CPU、温度、频率、系统负载、内存、NVMe、默认网卡吞吐、Hailo-8 READY/ACTIVE、整机健康、DSI 刷新率、进程 RSS 与系统运行时间，并复用统一返回按钮、圆角卡片、4× 抗锯齿和水平过场动画；CPU、内存和 NVMe 百分比使用统一字号，CPU 卡片只因额外承载温度、频率与负载而略高。
- 应用二级菜单提供“音乐”应用：`music_player.py` 负责递归扫描 `/mnt/nvme64/Music`、通过 FFprobe 读取标题/艺术家/专辑/时长，并以单个 VLC RC 子进程管理低延迟播放、暂停、上一首、下一首与自动续播；播放器状态机提供持久化的列表循环、单曲循环和乱序播放，自动换曲与手动运输控制遵循当前模式且乱序时避免立即重复当前曲；右上模式按钮单击循环切换三种模式，长按约 750 ms 手动刷新音乐库，进入音乐页也会发起异步扫描；扫描在线程池执行，渲染线程不接触磁盘探测；封面按“同名图片 → 目录中的 cover/folder/front/album → 音频内嵌图”选择，内嵌图由 FFmpeg 提取到 SSD 缓存，Pillow 在后台线程完成 EXIF 修正、居中裁切和圆形蒙版；中央默认显示封面，单击以 300 ms 交叉渐变切换到屏幕水平轴上的无框同步歌词，歌词层不继承封面圆环，并将曲名和艺术家随封面淡出；上一句、当前句和下一句以纵向位移动画更新，再次单击歌词带返回封面，没有素材时分别降级为粗体双音符或“暂无歌词”。歌曲开始播放或自动换曲后，歌词子系统先查找同名 `.lrc`，本地缺失时由独立单线程执行器顺序访问 LRCLIB：优先以标题、歌手、专辑和时长调用 `/api/get`，404 后节流约 300 ms 再以结构化关键词调用 `/api/search`，按标题/歌手相似度和时长差筛选同步歌词，原子保存到音频旁；每首歌每次服务运行最多联网一次，429 和网络错误不自动重试，所有请求与解析均不进入渲染线程。圆屏不再提供手动歌词开关，歌词显示始终启用；同名 `.lrc` 解析支持 offset、多时间戳和毫秒精度；圆屏页面显示曲目信息、进度、运输控制、播放模式与右侧边缘音量组件；待机图层仅保留细弧和位置点，触摸后在约 220 ms 内交叉渐变为缓存的宽滑轨，拖动通过 `wpctl` 修改默认系统音频输出，松手停留约 800 ms 后自动收回，拖到底部即可静音，因此播放器与语音播报保持同一音量源，同时不占用中央播放区；页面继续复用统一返回按钮和水平过场。播放模式持久化；离开音乐页后只用约 50 px 字号渲染当前句，不绘制边框或底板，显示范围根据歌词带上下边缘与 800 px 圆的交点计算为安全弦宽，短句居中，溢出句以固定速度在左右端点间连续横向滚动，滚动时间直接来自播放器 elapsed，因此暂停时自动冻结；播放器和表情页两处歌词带均在左右边缘应用平滑透明度遮罩；歌词层继续主动避让语音气泡、菜单及其他页面。离开页面后音乐继续播放，渲染服务退出时播放器子进程与歌词任务一并停止。
- 主环形菜单的“应用”进入二级应用环，提供番茄钟、性能、音乐、通话和工坊；原“开心”位置成为可持久化应用固定槽，默认显示“空位”。选中未固定应用后继续向外滑会把选中弧线展开为“固定到桌面”；同样操作当前已固定应用时外弧显示“取消固定”。只有指针进入与该可见外弧一致的环形扇区命中区域、保持满行程约 0.35 秒并在其上松手才确认；独立“清空”扇区已经移除。应用模型由 `app_menu.py` 管理，选择结果原子写入 `RIVERBANK_DATA/ui/app-pin.json`。工坊提供“创建应用、我的应用、导入应用”入口，并以 `riverbank.workshop/v1` 清单、`riverbank.app-host/v1` Host API、15 项能力目录、逐请求授权、`.rbapp` SHA-256/Ed25519 校验和事务化 disabled 注册构成安全基础层；清单永远不能申请 Shell、系统服务、凭据、宿主文件、软件包、内核和直接设备访问。语音创建只生成声明式候选，权限由可信代码反推；设备签名复检后仍须在圆屏逐项审核，批准后才由受信任解释器运行。Edge OS v0.26.1 beta 的审核主体是物理操作圆屏的人，系统尚未通过账户、声纹、PIN 或可信手机确认其是否为提案发起者或设备所有者；超级开发者负责在签名系统版本中定义 capability 和硬边界，不负责自动代审每个用户应用。目标权限模型采用“平台能力发布 + 设备所有者授权”双门，并把普通请求者、设备所有者和平台开发者分离。当前 Host Broker 已接通 UI、私有存储、限频通知、Camera Hub/Hailo、PipeWire 共享麦克风音量分析、后台任务和报告库；麦克风数据源带前台用户在场校验、1–300 秒租约、橙色隐私指示和异常自动回收，只向声明式应用提供 RMS、峰值与 dBFS，不保存原始 PCM。Python 运行时以及扬声器、云台工坊适配仍禁用。完整契约见 `docs/WORKSHOP_PROTOCOL.zh-CN.md`。Daily 在进入 Hermes 前解析明确的应用控制语句：现有四个运行型应用均可用语音进入，番茄钟支持完整计时控制，性能支持打开/关闭，音乐支持播放运输、显式播放模式与主页歌词开关，通话支持打开与挂断；明确的“创建/开发一个应用”进入工坊提案而不是开放式工具调用。音乐控制直接复用常驻渲染器与播放器实例，因此离开音乐页或播放中再次唤醒后仍能立即切换下一首；媒体库尚未扫描时，“播放音乐”设置一次性待播放标记，扫描完成后自动启动，避免首用命令丢失；
- 应用二级菜单中的“通话”连接 `riverbank-video-call.service`。服务以 WebRTC 交换 H.264/Opus，树莓派发送 Camera Hub 共享帧与 PipeWire 六麦阵列音频，并把桌面端音频送入默认 PipeWire 扬声器；对端视频只在内存保留最近帧，由 loopback 快照提供给圆屏。19734 端口同时承载本地账号、信令、任务队列、Chat 与只读报告 API。远端 macOS、Windows 和 iOS 客户端经 Tailscale Serve HTTPS 使用用户名密码换取可撤销会话；密码以 Argon2id 为首选算法保存，会话服务端只存摘要。旧设备凭据只用于首次管理员设置和受限内部服务，远端不能用它访问 Chat。Chat 会话、消息、附件与后台任务通过 `owner_user_id` 在服务端隔离。报告 API 仅暴露 Daily workspace 的 `reports/` 中非隐藏 Markdown，拒绝软链接、越界路径、上传、修改和删除。任务 API 只接收有限长度的标题、描述、类型和 `text|image|illustrated` 成果格式，使用幂等键防止网络重试产生重复任务；状态固定为 queued、running、waiting_input、completed、failed、cancelled。`riverbank-task-worker.service` 以独立进程认领任务，客户端退出、锁屏或断线不影响执行；文字和图文任务先由 Hermes 原子归档 Markdown，图片与图文配图复用 Qwen Image，二进制成果写入按任务 ID 隔离的私有目录并通过任务所有权鉴权下载；图文主成果为内嵌图片的离线单文件，可直接打印为 PDF。ICE 仅发布主机候选，媒体由 DTLS-SRTP 加密，不依赖公网 STUN/TURN。Tailscale Serve 保留论文站根路径，并将 `/assistant` 代理到 19734；完整身份协议见 `docs/AUTHENTICATION.zh-CN.md`；
- 音量、重启、屏保和 Token 弹层；
- 相机实时画面、拍照与相册；
- 相册左右滑动、长按删除/设为屏保；
- 边缘向中心滑动退出相机；再次选择“相机”也可退出。

重启采用两级权限边界：UI 只在滑块完成且手指抬起时写入 `/run/riverbank-expression/reboot.request`；root 运行的 path unit 观察该文件并执行重启。滑块回拖可在抬手前撤销。

番茄钟的计时状态由 `pomodoro.py` 管理，并按墙上时钟截止时间原子写入 `RIVERBANK_DATA/pomodoro/state.json`。同一状态文件按本地自然日累计实际运行的专注秒数、休息秒数和完成轮次，保留最多约 180 天，统计页读取最近 7 天；升级前没有留下的历史阶段不会反向推算。语音意图由无外部依赖的 `pomodoro_voice.py` 解析，`daily_voice_assistant.py` 只把经过白名单验证的动作发送到本机显示控制 socket。离开计时页不会停止倒计时；渲染服务重启后会按截止时间恢复，已到期的专注阶段只切换到待开始的休息阶段，不会在无人确认时自动连续运行下一阶段。

## 4. 开机显示接力

```text
Kernel / Plymouth
    ↓ 黑底紧凑 Logo
LightDM + labwc
    ↓ boot_handoff.py 在 DSI Wayland 输出保持静态 Logo
Expression UI
    ↓ 3×3 方块逐个跳动并读取健康状态
静态 Logo 淡出
    ↓
表情待机
```

Plymouth 负责最早可见阶段；Wayland 接力层消除显示管理器接管期间的桌面或黑屏；常驻渲染器只有在窗口建立后才通知接力层退出。`boot_handoff.py` 优先使用 `swaybg`，GTK 仅作回退。

## 5. 语音与模型路由

Chat、后台任务和工坊不再直接调用 Hermes 命令行，而是提交 `riverbank.agent/v1` 结构化请求给本机 Agent Runtime。Runtime 统一校验用途、逻辑工作区、工具集、自治权限、超时和附件根目录，再由当前 Hermes 适配器执行；生产服务依赖该 Runtime，显式直连只作为测试和人工应急。语音为保证首字延迟继续复用常驻实例，但 Hermes Python 内部导入已经封装在同一模块的低延迟适配器内。未来更换 Agent 框架时，Chat、任务、工坊、ASR/TTS、表情和 UI 不需要随之重写。完整协议见 `docs/AGENT_RUNTIME_PROTOCOL.zh-CN.md`。

ListenGo 控制口报告硬件唤醒事件和声源方向。Daily Voice 随后执行：

1. 播放可选的本地唤醒回应；
2. 唤醒后切换“聆听”表情，并仅在录音窗口显示左下角转写气泡；气泡本身代表正在收音，不再显示额外的“正在听”文字；
3. VAD 录音并保留短预卷，同时由 sherpa-onnx 流式 Zipformer 生成不超过三行的本地增量草稿；
4. 录音结束后由常驻 SenseVoice INT8 与 Zipformer CTC INT8 并行生成候选；路由器结合信号强度、双模型一致度、语言特征和异常重复检测决定最终文本，弱信号或高分歧结果不会提交给 Hermes；
5. 先匹配音量、相机等本地设备命令；
6. 其他问题交给常驻复用的 Hermes Daily Profile；
7. Hermes 每产生一段新文字就交给自适应分句器：优先在自然标点处切分，短句尽量整句朗读，长无标点片段才使用长度上限；
8. 各语音块由 Edge TTS 顺序合成，并写入同一个持续的 MP3 播放流，使扬声器在模型生成完整回答前开始播放；
9. 若流式 TTS 在首段出声前失败，自动回退到完整回复合成；若回复要求补充信息，则播放完后继续录制下一轮，最多由配置限制轮数。

DeepSeek 的最终回复以一个受控情绪标签开头。`response_emotions.py` 能跨多个流式 delta 缓冲并截获标签，在第一个可朗读文本进入 TTS 前发布对应表情；最终文本还会进行第二次清理，确保标签不会被朗读或显示。可选状态严格限制为现有表情白名单，漏标时使用保守的本地内容规则，无法判断则维持 `thinking`。标签只表达本次回复对用户的态度，不因引用文本里出现情绪词而直接切换。

唤醒事件由独立监视线程持续读取，不会因主交互线程正在等待最终 ASR、Hermes、Qwen 或 TTS 而停止。忙碌时出现新的硬件唤醒事件，系统会停止旧音频、对 Hermes 发出硬中断、取消进行中的视觉任务，并把最新的唤醒事件作为下一轮录音。仍在结束中的本地 ASR 线程可以自行收尾，但旧结果不会进入气泡或 Hermes；多次连续唤醒时只保留最新一次。

气泡中的原始文字只保存在渲染器内存中；运行时状态只记录长度和阶段。14M 中文流式模型只承担低延迟视觉反馈，交给 Hermes 的文本来自完整音频上的最终双模型 ASR。流式模型路径可用 `RIVERBANK_STREAMING_ASR_MODEL_DIR` 覆盖；最终模型根目录可用 `RIVERBANK_FINAL_ASR_MODEL_ROOT` 覆盖。

流式语音默认以 10 个可见字符作为自然分句的最小缓冲，24 个字符作为无标点长段的上限；它们是实机延迟与听感的平衡值，而不是固定“等够某个字就开读”。工具调用轮次、`<think>` 内容和语音跟进控制标记不会进入扬声器。

为了避免后台服务造成首字抖动，Daily Voice 在 systemd 中使用更高的 CPU/IO 调度权重；Hailo 推理按有效视觉租约启动，空闲时仅保留不打开设备的轻量协调器。Daily Agent 默认仅暴露口语场景需要的记忆、任务、文件整理、视觉、网页搜索和本地浏览器等工具；浏览器用于机票、酒店等必须打开动态页面才能核实的实时查询，不开放代码执行等无关工具。`file` 只用于用户明确指定范围内的查看、分类、建目录、重命名和移动，删除、覆盖及系统目录操作必须再次确认。机票“未来一周最低价”另走独立只读限时查询，并发比较未来 7 个自然日的 Google Flights 可见报价，不进入开放式网页操作循环；普通 Hermes 工具调用有 75 秒硬超时。

每轮只持久化阶段耗时，不保存转写文字。滑动统计包括 STT、LLM 首字、LLM 完成、TTS 首帧和从说完到首次出声的 P50/P95。

圆屏渲染器独立常驻，语音服务重启不会重建窗口。语音服务通过 `riverbank.expression.event/v1` 结构化事件发布表情状态；事件包含发布器、交互轮次和递增顺序号，桥接器会拒绝过期、重复或倒序事件，并用原子快照处理服务重启和启动竞态。语音和屏幕文字任务的所有正常结束路径都会显式恢复 `idle`。

模型名称、供应商、密钥和 Profile 数据均由外部 Hermes 配置提供，本仓库不保存。视觉意图会携带当前摄像头帧进入 Hermes；具体路由到 Qwen 或其他 VLM 应在 Daily Profile 中配置。

## 6. 论文日报

Paper Radar 的数据面与呈现面分开：

```text
collect.py → candidate JSON → Hermes 按 AGENTS.md 精读/撰写
           → latest-report.json → finalize_run.py
           → Markdown 归档 + 原子化静态网页 + candidates 页面
```

默认策略为 Asia/Shanghai 每日 08:00：

- 检索具身智能、上游生成建模、机器人交叉应用、分类最新、VLM、空间智能/VGGT 类方法和 JEPA/预测世界模型；
- 近 72 小时优先，空余位置从 183 天内未展示内容回填；
- 候选阈值 10，最多 30；精选阈值 40，最多 10；产业动态最多 9；一次性焦点最多额外 5；
- 候选保存原始摘要并生成忠实中文概述；高相关项目阅读全文；
- 对尚无机器人实验的迁移价值必须明确标记为“分析与推断”。

网页只默认展示最新日报，但保留历史归档入口。“明日焦点”请求写入原子化队列，只有成功完成下一次渲染后才标记为已消费。

每次成功日报还会原子替换 `data/paper-qa/current.json`，保存当日精选文章的结构化精读笔记
与正文分块。逐篇文章对话首先检索这份缓存；当问题要求具体型号、参数、实验设置或明确要求
进一步核实时，Chat Worker 不让通用模型自由上网，而是调用 `paper_enrich.py` 受控读取该文
自身的 arXiv PDF，PDF 不可用时才降级到 arXiv HTML。新证据写回同一个当日缓存并供后续
问答复用，不产生按日期归档的全文或向量库；次日成功日报整体替换缓存，长期对话仍保存在
独立 Chat 数据库中。这样日常回答保留精读成果，缓存缺口也能回到一手原文继续核对。

## 7. 健康监控

检查器由 JSON 配置扩展，支持：

- `systemd_service`
- `http` / `http_json`
- `tcp`
- `mount`
- `path` / `fresh_path`
- `command`

连续失败达到阈值后才显示桌面告警；检查结果同步写入状态文件，圆屏开机自检和“设置”页面都可读取。专有 VPN 和代理检查在公开示例中默认关闭。

SYSTEM 详情页提供“恢复全部服务”。UI 仅向权限受限的 Unix Socket 发送 `recover_all`，root 控制器只允许重启固定白名单服务；显示服务最后重启。恢复事务带互斥锁，并比较 Asia/Shanghai 当天应有日报与 `latest-report.json` 日期：发现缺口时通过独立的 `riverbank-paper-catchup` transient unit 投递现有 Hermes 定时任务。Hermes 的 durable execution 与 300 秒 fire-claim 共同防止重复执行；意外断电留下新鲜但失主的 claim 时，控制器建立延迟 timer，在 lease 安全过期后补跑。

交互与自动化服务统一进入 `riverbank-apps.slice`：CPU 总上限为 320%，`MemoryHigh=70%`、`MemoryMax=82%`，给内核、SSH、健康监控、恢复和配网预留约 18% 内存；这些保底服务留在 `system.slice`。网络相关常驻单元最多每分钟重启 5 次，避免断网时形成无限重启风暴。Paper Radar 连续两条 arXiv 流确认网络不可用后打开当次熔断，不再把相同离线错误乘以剩余检索流。

离线配网服务只在连续 60 秒无法访问探测端点后激活。已有局域网地址时，圆屏显示带一次性 token 的本机设置页二维码；完全没有默认路由时，NetworkManager 创建随机 WPA2 临时热点并显示 Wi-Fi 二维码，同时在 80 端口提供 captive-portal 入口。手机页面可写入 Wi-Fi、设备名、所有者标识、DeepSeek Key 和中国区 DashScope Key。模型密钥不进入二维码、状态文件、页面回显或日志，只以 `0600` 原子写入 Hermes 环境文件；网络恢复后热点、二维码和设置画面自动退出。

## 8. 多端 Chat

iOS、macOS 和 Windows 客户端共用 `19734` 上的账号鉴权 API。用户名与密码只用于换取可撤销会话，Chat、消息与附件查询始终在服务端匹配 `owner_user_id`；一次性设备凭据只负责首次管理员建立和本机内部服务。第一版附件白名单统一为 JPG/JPEG、PNG、WebP、PDF、Markdown 和 TXT：服务端依据扩展名与真实魔数/解码结果双重校验，拒绝 CSV、Office、压缩包和程序；单文件最多 15 MB、单条消息总计 30 MB、最多 4 个文件且最多 1 张图片。iOS 相册素材在端侧归一化为 JPEG，以兼容 HEIC 照片且限制像素和上传体积。`video_call_server.py` 只负责鉴权、会话与消息入队、读取和停止请求；Chat 历史使用独立 `/var/lib/riverbank-tasks/chat.db`，避免与后台任务库的 SQLite 模式互相锁定。`riverbank-chat-worker.service` 独立领取回复，显式绑定 Daily profile，并以 `riverbank-chat-<conversation_id>` 命名 Hermes 会话保持多轮上下文。Worker 会持续写入当前输出，客户端活动回复期间约每 550 ms 刷新，因此重开 App 或短时断网后仍可恢复当前对话。停止生成通过数据库取消位驱动子进程终止，不依赖客户端保持长连接。

文字 Chat 默认只开放浏览、搜索和技能工具，不开放任意文件或系统修改；长时间调研、文件整理和报告生成继续进入持久化后台任务队列。两个 Worker 使用不同数据库和进程，Chat 不得阻塞既有任务。

Worker 在进入 Hermes 之前先做窄范围能力路由：明确的文生图请求调用阿里云中国区
DashScope 原生 Qwen Image API，默认使用 `qwen-image-3.0-pro`；上传图片的理解请求仍由
Hermes 配置的 Qwen VLM 处理。Qwen Image 返回的 OSS URL 仅用于即时下载，图片经过格式、
尺寸和大小校验后进入 `/var/lib/riverbank-tasks/chat-attachments/`，再作为 assistant 附件
写回消息。客户端只通过带账号会话的附件 API 展示或下载，不直接持有供应商临时地址。

## 9. 版本与发布完整性

版本边界分为 RiverBank 产品发布列车、RiverBank Edge 硬件、RiverBank Edge OS、Debian/Raspberry Pi OS 上游基础发行版、iOS 客户端、macOS/Windows 客户端和独立工坊应用。客户端属于 RiverBank 产品套件，但不是 Edge OS 的一部分，也不继承 Edge OS 的版本号。硬件用产品代次与 Revision，RiverBank 自有软件统一显示 `vMAJOR.MINOR.PATCH beta|stable`，上游系统保留原版本。

`config/version-catalog.json` 是当前版本组合与兼容范围的唯一清单；`scripts/versionctl.py check` 校验圆屏、Xcode 和 Electron 工程中的原生版本入口。发布列车只是联合验收后的 BOM 映射，各独立产物可以使用不同版本号。`apps/release-manager/release_manager.py` 只封存 RiverBank Edge OS：先确认全部输入存在，再写入 Edge OS 版本、基础发行版信息，以及包含组件、服务、文件大小与 SHA-256 的原子清单。健康守护器周期验证清单；未重新封存的直接修改会被报告为发布漂移，而不会被静默接受为正式版本。当前 Edge OS 是安装在受支持 Debian/Raspberry Pi OS 上的受管系统层，未来系统镜像、分区和原子 OTA 边界见 `docs/EDGE_OS.zh-CN.md`。完整命名规则见 `apps/expression-ui/VERSIONING.md`。
