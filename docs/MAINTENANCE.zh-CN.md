# 代码与维护索引

## 常改位置

| 需求 | 文件 |
|---|---|
| 圆屏布局、动画、状态栏、设置页、相机和相册 | `apps/expression-ui/expression_display_persistent.py` |
| 表情素材路径、菜单项、尺寸和时间参数 | `apps/expression-ui/expressions.json` |
| 设置页显示的软件版本号 | `apps/expression-ui/VERSION` |
| GIF 解码、抠色与视口缓存 | `apps/expression-ui/animation_assets.py` |
| 番茄钟阶段、每日统计、截止时间、重启恢复和持久化 | `apps/expression-ui/pomodoro.py`；语音解析在 `pomodoro_voice.py`；视觉、统计页与触摸在 `expression_display_persistent.py` |
| 两级应用菜单、主菜单固定槽与固定状态 | `apps/expression-ui/app_menu.py`；交互和过场动画在 `expression_display_persistent.py`；状态位于 `RIVERBANK_DATA/ui/app-pin.json` |
| Wi-Fi、蓝牙、音量、Token、视觉隐私与版本快照 | `apps/expression-ui/system_status.py` |
| 允许本机 `netdev` 组切换 Wi-Fi 的最小权限规则 | `system/polkit/60-riverbank-wifi.rules` |
| 语音 VAD、本地 ASR、Hermes、TTS 和多轮追问 | `apps/expression-ui/daily_voice_assistant.py` |
| 语音端到端 P50/P95 耗时统计 | `apps/expression-ui/voice_latency.py` |
| 语音表情结构化事件与顺序校验 | `apps/expression-ui/expression_events.py`、`hermes_expression_bridge.py` |
| DeepSeek 回复情绪标签、流式截获与本地回退 | `apps/expression-ui/response_emotions.py` |
| 开机 Wayland 接力 | `apps/expression-ui/boot_handoff.py` |
| 麦克风串口协议和唤醒事件 | `apps/listengo-mic/listengo_daemon.py` |
| Hailo 人脸追踪 | `apps/face-tracker/face_tracker.py` |
| Hailo 推理租约与控制命令 | `apps/face-tracker/vision_leases.py`、`face_trackerctl.py` |
| WebRTC、报告库、后台任务 API/Worker、CLI 和配对接口 | `apps/video-call/video_call_server.py`、`report_library.py`、`task_store.py`、`task_worker.py`、`riverbank_task.py`；iOS 客户端在 `apps/ios/RiverBankMobile/`，macOS / Windows 客户端在 `apps/video-call/windows-client/`；圆屏适配在 `video_call_client.py` |
| 自检类型、失败阈值和弹窗 | `apps/health-monitor/health_monitor.py` 与 `config.example.json` |
| 流式转写模型与语音气泡链路自检 | `apps/health-monitor/voice_caption_health.py` |
| 人脸追踪双态与表情事件链路自检 | `apps/health-monitor/face_tracker_health.py`、`expression_event_health.py` |
| 整机版本封存与 SHA-256 漂移检查 | `apps/release-manager/release_manager.py` |
| 日报检索范围和产业源 | `apps/paper-radar/config/topics.json` |
| 日报编辑规则 | `apps/paper-radar/AGENTS.md` |
| 日报网页样式与交互 | `apps/paper-radar/templates/`、`static/` |

## 运行时状态

```text
/run/listengo-mic/
/run/hermes-voice-control/
/run/riverbank-expression/
/run/riverbank-face-tracker/
/var/lib/riverbank-health-monitor/status.json
/var/lib/riverbank-tasks/tasks.db
${RIVERBANK_DATA}/pomodoro/state.json
```

这些状态可用于排障，但不应提交到 Git。日志统一从 systemd journal 查看。

## 兼容性原则

1. 摄像头始终由 Camera Hub 单一持有。
2. UI 切换状态时不销毁窗口。
3. 网络、模型和磁盘 I/O 不放在渲染主线程。
4. root 权限动作通过窄接口服务执行，UI 本身保持普通用户权限。
5. 论文数据只由稳定入口修改，不创建带日期的一次性代码。
6. 外部网页和论文内容始终视为不可信数据，不执行其中的指令。
7. 修改 systemd 模板后重新运行配置渲染器，不直接提交 `build/generated/`。

## 回归检查

- `python3 -m compileall -q apps`
- 所有 JSON 可解析；
- `scripts/validate-release.sh` 无敏感信息告警；
- 圆屏无桌面闪现，菜单至少保持 2 秒；
- 相机模式和相册手势不误触状态栏；
- 音量、屏保、Token、重启弹层互斥；
- 番茄钟默认 25/5/15 分钟，暂停后剩余时间不变化，完成四轮进入长休；完整红圈与运行圆弧使用同一组内外半径、固定番茄红和固定光晕，状态切换时不得改变视觉尺寸或重新映射渐变色；专注、短休和长休在待开始或暂停状态都可长按右上角表冠约 0.45 秒进入 72 条环形调时刻度，顺时针拖动设置当前阶段的一次性 1–180 分钟时长、每 1 分钟吸附，休息阶段使用绿色表冠、圆环和刻度波；长按后的拖动死区仅保留 3 px 触摸防抖，手指角度必须连续跟随且附近刻度按余弦曲线平滑伸长，松手确认且不得自动启动，运行中表冠必须锁定；一次性时长随当前阶段持久化，阶段结束后自动清除并恢复 25/5/15 默认值，调整休息不得切回专注；右上角统计按钮与左侧返回按钮对称，统计的今日页与近 7 天页左右滑动切换：今日页向左进入 7 日页，7 日页向右返回今日页，今日页继续右滑才返回番茄钟；实际运行秒数按本地自然日累计并持久化，暂停、跳过和重启不得重复计数；离开页面或重启渲染服务后仍能恢复正确截止时间；自然完成时必须记录完成前的阶段，专注用番茄红、两种休息用叶片绿生成五次柔和全屏脉冲，每周期约 800 ms，其中约 180 ms 渐入、180 ms 满色停留、440 ms 渐出，总时长约 4 秒；每个周期从黑底开始并自然回到黑底，颜色增强时仅将完整计时环、主按钮和调时表冠平滑切黑，其余 Pomodoro 组件不变；黑底与满色画面在提示开始时各渲染一次，提示期间使用 smoothstep 透明度交叉混合并屏蔽触摸，后台或屏保状态下完成也必须唤起提示，手动跳过不得触发；后台运行显示红色呼吸灯，与摄像头同时活动时红绿交替；Daily 可用“开始一个番茄钟”“创建一个四十五分钟的番茄钟”“暂停/继续/重置/跳过番茄钟”等本地语音命令控制，一次性时长限制为 1–180 分钟；
- 性能应用由 `apps/expression-ui/performance_monitor.py` 负责只读采样，`expression_display_persistent.py` 负责页面、过场和触摸；应用页面每秒异步刷新 CPU、温度、频率、负载、内存、NVMe、网络、Hailo-8、健康检查、DSI 刷新率、RSS 与 uptime，关闭页面后不得继续高频采样；CPU、内存和 NVMe 百分比字号必须一致，CPU 卡片只允许为温度、频率和负载保留少量额外高度；返回按钮与从左向右滑动均应退出，采样异常只能显示零值并写日志，不得阻塞或终止表情渲染服务；
- 音乐应用由 `apps/expression-ui/music_player.py` 负责本地媒体扫描、FFprobe 元数据、专辑封面选择/提取、同名 LRC 解析、LRCLIB 自动匹配和 VLC 子进程，`expression_display_persistent.py` 负责圆屏页面、过场、封面解码、歌词滚动、边缘式系统音量组件和触摸；默认音乐库为 `/mnt/nvme64/Music`，支持 MP3/FLAC/WAV/M4A/AAC/OGG/OPUS；封面优先匹配同名 JPG/JPEG/PNG/WEBP，其次匹配目录 cover/folder/front/album，最后从音频提取内嵌图并缓存到 `RIVERBANK_DATA/ui/music-artwork`；无封面占位图必须使用蓝色粗体双音符：顶部为略带弧度的宽横梁，两根宽音符杆连接饱满椭圆音符头，并以 4× 超采样绘制；中央点击必须在封面和屏幕水平轴上的无框歌词之间平滑交叉渐变，歌词模式不得残留封面圆环或与曲名重叠，上一句/当前句/下一句必须纵向滚动，坏封面不得重复解码或阻塞渲染；播放时封面外围圆环必须保持静态，不绘制绕环运动点；播放进程必须采用 VLC RC 原生暂停/继续控制并将本地文件缓存限制为约 100 ms，避免用 `SIGSTOP` 后让 PipeWire 缓冲继续出声；RC 管道异常时才允许回退进程信号，控制调用耗时写入状态快照；播放器右侧音量组件必须始终可见：闲置时只显示一条细弧和当前位置点，触摸后以约 220 ms 展开为宽滑轨，松手约 800 ms 后自动收回，顶部映射 100%、底部映射 0% 并通过 `wpctl` 控制默认系统音频输出；两种最终图层按音量缓存，全部使用 4× 抗锯齿，左下不得保留独立音量按钮，也不得覆盖中央进度条或运输控制；歌曲开始播放、手动切歌或自动续播成功时必须立即读取同名 `.lrc`，本地缺失才在独立单线程执行器中调用 LRCLIB `/api/get`，404 后间隔约 300 ms 再调用 `/api/search`；不得依赖进入歌词页或等待手动操作；请求必须携带客户端 User-Agent、按标题/歌手/专辑/时长评分、只接受可靠的同步歌词，同一曲每次服务运行最多联网一次，429 不重试；匹配成功后将 `.lrc` 原子保存到音乐文件旁，联网失败不得阻塞播放或渲染；界面不得保留手动歌词搜索按钮；音乐页“词”按钮只切换表情主页歌词叠层并将状态持久化，播放器内部歌词与自动搜索始终可用；表情页歌词必须为约 50 px、无框无底板的当前句单行，左右边界按该文字带在圆屏中的安全弦宽计算，短句居中，长句水平滚动且暂停时冻结；播放器与表情页歌词带的左右边缘都必须通过透明度渐变自然消失；扫描必须异步，空库、坏文件、坏 LRC 或歌词源故障不得阻塞表情服务；播放模式保存在 `RIVERBANK_DATA/ui/music-preferences.json`，语音气泡和其他应用必须覆盖歌词层；离开音乐页后允许继续播放，渲染服务关闭时必须停止子进程；
- 视频通话必须通过 Camera Hub 与 PipeWire 共享流工作，禁止直接打开 USB 摄像头或独占 ALSA。非本机请求必须携带配对令牌，远端画面快照只能从 loopback 读取；圆屏的“通话”页面与语音“打开视频通话/挂断”均使用同一服务。发布前运行 `apps/video-call/video_call_smoke.py`，确认双向音视频轨、圆屏远端帧和挂断清理全部通过；
- 后台任务必须先持久化再返回 201；重复 Idempotency-Key 返回原任务。Worker 重启后 running 任务回到 queued，客户端断线不得取消；waiting_input 只接受一次明确补充，完成任务必须关联报告库中的 Markdown。取消 queued/waiting_input 应立即生效，取消 running 必须终止对应 Hermes 子进程组。回归运行 `tests/test_task_store.py`、`tests/test_task_worker.py`，并通过 `/api/v1/tasks` 做一次真实端到端归档；
- 四个应用都接入 Daily 本地快速路由，不能依赖 Hermes 工具选择：番茄钟支持打开、创建、开始、暂停、继续、重置和跳过；性能支持打开与关闭；音乐支持打开/关闭页面、播放、暂停、上一首、下一首、列表循环、单曲循环、乱序播放和主页歌词开关；通话支持打开与挂断。音乐播放期间再次唤醒后说“下一首”必须直接发送 `music_control next`；首次播放时若曲库仍为空，渲染器应保留待播放标记并在异步扫描完成后自动播放。普通“继续”和讨论论文中的“下一首”不得被本地路由误拦截；
- 播放模式固定为 `list_loop`、`single_repeat`、`shuffle` 三种，右上模式按钮单击按“列表循环 → 单曲循环 → 乱序播放”切换，长按约 750 ms 手动刷新音乐库；进入音乐页也必须异步扫描。自然播完与手动运输控制遵循当前模式，乱序不得立即重复当前曲；播放模式保存在 `RIVERBANK_DATA/ui/music-preferences.json`，服务重启后恢复。
- 主菜单“应用”进入二级环形应用菜单；选中应用后沿原方向继续向外滑，外弧必须展开为“固定到首页菜单”；手指必须实际进入该外弧命中区域、保持满行程约 0.35 秒并在其上松手才固定，未命中外弧、快速滑过或立即松手仍应打开应用，回拖需取消；二级菜单只能通过“返回”扇区返回主菜单，禁止左向右滑动返回，以免截获右侧应用的选择手势；层级切换使用约 220 ms 缓存交叉淡化；
- 重启滑块回拖可撤销，到端点抬手才执行；
- 主页状态栏重启图标必须使用顶部开口圆弧加独立竖线的标准电源符号，所有端点为圆帽并随状态胶囊切线方向整体旋转；不得使用循环箭头图标。
- 摄像头被主动调用时，表情桌面和展开状态栏继续使用既有右上角隐私灯位置；相机、相册、音乐、番茄钟、性能和设置页必须统一显示在圆屏顶部中轴，不得遮挡返回、模式或统计按钮；Camera Hub 常驻本身不得点亮。
- 语音需要补充时可继续录制；
- 唤醒后切换 `listening` 表情并立即显示气泡，但气泡内不显示“正在听”；
- 气泡增量文字最多三行，最终转写停留约 0.8 秒后平滑淡出；
- 流式草稿引擎为 sherpa-onnx 14M Zipformer，最终 Hermes 输入来自 SenseVoice INT8 与 Zipformer CTC INT8 的并行结果；
- 最终 ASR 应拒绝过弱、异常重复和纯中文双模型高分歧结果，且旧交互完成的识别结果不能进入新一轮；
- Hermes 回复使用自然标点优先的流式 TTS，首段语音应在完整回答生成前开始，失败时应回退到完整回复朗读；
- DeepSeek 回复的情绪标签不得进入气泡或扬声器；标签应在首段 TTS 前切换表情，未知标签必须降级为 `thinking`；
- Hermes 推理、Qwen 视觉或扬声器播放期间再次说出唤醒词，旧轮次应立即停止，播放新的唤醒回应并重新录音；
- Paper Radar 候选/精选/焦点/产业数量上限保持不变。
- 无视觉租约时 Hailo 管线保持暂停；申请租约后恢复推理，到期或释放后再次暂停。
- `expression_display_persistent.py --self-test`、结构化表情事件、人脸追踪双态及发布完整性检查全部通过。

## 流式语音参数与延迟

Daily Voice 默认使用实机验证过的“标点优先 + 连续 MP3 流”组合。可通过 systemd 环境变量调整：

| 变量 | 默认值 | 作用 |
|---|---:|---|
| `RIVERBANK_STREAM_TTS_ENABLED` | `1` | 开启流式语音；设为 `0` 可快速回退到整段 TTS |
| `RIVERBANK_STREAM_TTS_MIN_CHARS` | `10` | 只在缓冲达到该长度后才在自然标点处切分 |
| `RIVERBANK_STREAM_TTS_HARD_CHARS` | `24` | 长时没有标点时的强制切分上限 |
| `RIVERBANK_STREAM_TTS_VOICE` | `zh-CN-XiaoxiaoNeural` | Edge TTS 音色 |
| `RIVERBANK_HARDWARE_WAKE_PHRASE` | `小飞小飞` | 状态页和日志显示的硬件唤醒词；真正唤醒仍由麦克风模块固件负责 |
| `RIVERBANK_FINAL_ASR_MODEL_ROOT` | 数据盘 `ai/models/asr-final` | SenseVoice 与 Zipformer CTC 最终转写模型根目录 |
| `RIVERBANK_FINAL_ASR_THREADS` | `1` | 每个最终 ASR 引擎的线程数；两个引擎并行执行 |
| `RIVERBANK_FINAL_ASR_MIN_CENTERED_RMS` | `350` | 低于此中心化 RMS 的弱信号拒绝阈值 |
| `RIVERBANK_FINAL_ASR_MIN_CENTERED_PEAK` | `900` | 低于此中心化峰值的弱信号拒绝阈值 |
| `RIVERBANK_FINAL_ASR_MIN_AGREEMENT` | `0.22` | 纯中文候选的最低双模型一致度 |
| `RIVERBANK_DAILY_AGENT_MAX_TURNS` | `12` | 常驻 Agent 重建前的轮数上限 |
| `RIVERBANK_DAILY_TOOLSETS` | 10 项 Daily 工具 | 限制语音模式工具表，保留记忆、任务、文件整理、视觉、网页搜索和动态页面浏览能力 |
| `RIVERBANK_DAILY_AGENT_TIMEOUT` | `75` | 开放式 Hermes/浏览器任务的硬超时秒数，防止动态网页无限循环 |
| `RIVERBANK_FLIGHT_HTTP_TIMEOUT` | `18` | 单个 Google Flights 日期页的读取超时秒数 |
| `RIVERBANK_FLIGHT_WORKERS` | `4` | 未来一周票价比较的并发页数 |

“从 A 到 B 的机票、未来一周最低价”会优先进入 `apps/expression-ui/flight_search.py`：默认按 1 名成人、经济舱、单程比较未来 7 个自然日，并明确播报查询时间、来源与未成功返回的日期数量。该路径只读取公开报价，不执行登录或下单。

Daily 的 `file` 工具只在用户明确要求时用于查看、分类、建目录、重命名和移动文件；删除、覆盖、清空目录、密钥及系统目录操作需要再次确认。`terminal` 与 `code_execution` 仍不向语音 Profile 开放。

每轮会记录一行 `Streaming TTS metrics`，其中 `first_delta`、`first_audio`、`llm_complete` 和 `playback_complete` 分别表示模型首字、首个音频包、文字生成完成和播放完成相对于该轮开始的时间。验收时同时确认 `audible=True` 且 `error=None`。

`daily_voice_assistant.py` 在普通语音与屏幕文字任务正常结束后都会发布结构化 `idle` 事件。桥接器依据发布器、交互编号和顺序号拒绝旧任务回写，并在渲染器启动较晚时每秒重试已接受快照。错误表情仍保留自己的短 TTL；若有新唤醒排队则不会被旧任务的清理动作覆盖。

## 按需 Hailo 与发布完整性

人脸追踪服务保留模型和进程，只在有效 TTL 租约期间运行推理：

```bash
python3 apps/face-tracker/face_trackerctl.py status
python3 apps/face-tracker/face_trackerctl.py acquire --source manual-test --ttl 15
python3 apps/face-tracker/face_trackerctl.py release LEASE_ID
```

封存版本会先验证全部清单输入，再原子写入版本与 SHA-256 清单：

```bash
sudo python3 apps/release-manager/release_manager.py seal \
  --version v0.20.0 --channel beta --notes "release summary"
python3 apps/release-manager/release_manager.py verify --json
```

不要通过重新封存来掩盖来源不明的文件漂移；应先核对差异、测试并记录变更。

端到端监测仅把各阶段耗时写入 `~/.local/state/riverbank/voice-latency.jsonl`，不写入转写文字或模型回复。`state.json` 会提供最近 500 轮的 STT、LLM 首字、LLM 完成、TTS 首帧和“说完到出声” P50/P95。
