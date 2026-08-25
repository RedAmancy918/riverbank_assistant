# 架构与实现说明

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
| Paper Radar | `paper-radar-web.service` | `:19732` 网页与焦点输入 API |

运行时文件只包含经过筛选的状态，不应写入密钥、完整语音、模型回复历史或摄像头原始流。

## 2. 摄像头单一持有

USB 摄像头由 `ustreamer` 单独持有。其他服务读取本地 MJPEG 流或单帧接口，从而避免多个 OpenCV/GStreamer 进程同时打开 `/dev/video0`。

- 圆屏相机模式：按刷新节奏获取 `/snapshot`，退出后停止主动取帧。
- 视觉问答：Daily Voice 在识别到视觉意图时保存一张临时帧给 Hermes。
- Hailo 人脸追踪：进程和模型常驻；只有人脸跟踪、云台跟随或明确视觉任务持有有效 TTL 租约时才恢复推理管线。空闲时暂停 GStreamer/Hailo 出帧，但不卸载模型。
- 相册：拍照时保存高质量 JPEG 到 `RIVERBANK_DATA/camera/gallery`。

相机常驻的目的只是避免硬件抢占和冷启动；隐私指示灯只应表示用户可感知的主动视觉任务，而不是 Camera Hub 自身。

## 3. 圆屏渲染

`expression_display_persistent.py` 创建一次无边框窗口并持续复用。`animation_assets.py` 负责 GIF、抠色与视口准备，`system_status.py` 提供类型化只读状态快照；GIF 解码、背景处理、4× 超采样抗锯齿、状态栏组件和部分过渡帧采用预加载或缓存，减少切换时桌面闪现和主线程卡顿。渲染器提供 `--self-test`，可在不开窗口时检查模块、素材、800×800 目标尺寸和版本格式。

主要状态包括：

- 表情待机与触摸表情；
- 长按后至少保留 2 秒的六项环形菜单；
- 左右两个状态胶囊；
- 从环形菜单进入的设置页，自动刷新 Wi-Fi、蓝牙、软件版本与系统自检信息，支持 Wi-Fi 开关；表情与设置、设置首页与详情之间统一使用缓存横向过场，并支持按钮或左向右滑动返回；
- 音量、重启、屏保和 Token 弹层；
- 相机实时画面、拍照与相册；
- 相册左右滑动、长按删除/设为屏保；
- 边缘向中心滑动退出相机；再次选择“相机”也可退出。

重启采用两级权限边界：UI 只在滑块完成且手指抬起时写入 `/run/riverbank-expression/reboot.request`；root 运行的 path unit 观察该文件并执行重启。滑块回拖可在抬手前撤销。

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

唤醒事件由独立监视线程持续读取，不会因主交互线程正在等待最终 ASR、Hermes、Qwen 或 TTS 而停止。忙碌时出现新的硬件唤醒事件，系统会停止旧音频、对 Hermes 发出硬中断、取消进行中的视觉任务，并把最新的唤醒事件作为下一轮录音。仍在结束中的本地 ASR 线程可以自行收尾，但旧结果不会进入气泡或 Hermes；多次连续唤醒时只保留最新一次。

气泡中的原始文字只保存在渲染器内存中；运行时状态只记录长度和阶段。14M 中文流式模型只承担低延迟视觉反馈，交给 Hermes 的文本来自完整音频上的最终双模型 ASR。流式模型路径可用 `RIVERBANK_STREAMING_ASR_MODEL_DIR` 覆盖；最终模型根目录可用 `RIVERBANK_FINAL_ASR_MODEL_ROOT` 覆盖。

流式语音默认以 10 个可见字符作为自然分句的最小缓冲，24 个字符作为无标点长段的上限；它们是实机延迟与听感的平衡值，而不是固定“等够某个字就开读”。工具调用轮次、`<think>` 内容和语音跟进控制标记不会进入扬声器。

为了避免后台服务造成首字抖动，Daily Voice 在 systemd 中使用更高的 CPU/IO 调度权重；Hailo 模型常驻内存，但只有有效视觉租约才运行推理。Daily Agent 默认仅暴露口语场景需要的记忆、任务、文件整理、视觉、网页搜索和本地浏览器等工具；浏览器用于机票、酒店等必须打开动态页面才能核实的实时查询，不开放代码执行等无关工具。`file` 只用于用户明确指定范围内的查看、分类、建目录、重命名和移动，删除、覆盖及系统目录操作必须再次确认。机票“未来一周最低价”另走独立只读限时查询，并发比较未来 7 个自然日的 Google Flights 可见报价，不进入开放式网页操作循环；普通 Hermes 工具调用有 75 秒硬超时。

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

## 7. 健康监控

检查器由 JSON 配置扩展，支持：

- `systemd_service`
- `http` / `http_json`
- `tcp`
- `mount`
- `path` / `fresh_path`
- `command`

连续失败达到阈值后才显示桌面告警；检查结果同步写入状态文件，圆屏开机自检和“设置”页面都可读取。专有 VPN 和代理检查在公开示例中默认关闭。

## 8. 版本与发布完整性

RiverBank 自有组件统一显示为 `vMAJOR.MINOR.PATCH beta|stable`。`apps/release-manager/release_manager.py` 在封存时先确认全部输入存在，再写入整机版本和包含组件、服务、文件大小与 SHA-256 的原子清单。健康守护器周期验证清单；未重新封存的直接修改会被报告为发布漂移，而不会被静默接受为正式版本。
