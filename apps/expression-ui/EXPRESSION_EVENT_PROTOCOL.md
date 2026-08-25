# RiverBank 语音表情事件协议

当前协议版本：`riverbank.expression.event/v1`
系统版本：`v0.7.1 beta`

## 目标

语音服务是语音交互状态的唯一生产者，表情桥接器是语音表情的唯一消费者。链路不再从日志文本推断状态，避免迟到日志、重复日志和多轮交互并发导致表情倒退。

## 链路

1. `daily_voice_assistant.py` 为每次服务启动创建唯一 `publisher_id`。
2. 每次唤醒、屏幕文字请求或测试交互创建新的 `interaction_id` 和递增的 `interaction_index`。
3. 每次状态变化发布递增 `sequence` 的结构化事件。
4. 事件通过 `/run/riverbank-expression/voice-events.sock` 发送，同时原子保存最新快照到 `/run/hermes-voice-control/expression-event.json`。
5. `hermes_expression_bridge.py` 校验事件后，才向 `/run/riverbank-expression/control.sock` 转发表情。

## 关键字段

- `schema`：固定为 `riverbank.expression.event/v1`
- `source`：固定为 `hermes-voice`
- `publisher_id`、`publisher_started_at`：区分服务进程及其新旧关系
- `event_id`、`sequence`：去重并阻止事件倒序
- `interaction_id`、`interaction_index`：阻止上一轮问答覆盖下一轮
- `sent_at`、`monotonic_ns`：判断过期事件并辅助诊断
- `stage`：录音、转写、模型调用、结束等语义阶段
- `state`、`ttl`：目标表情和可选持续时间

## 拒绝规则

桥接器会拒绝协议或来源错误、身份字段缺失、重复/倒序、旧交互、旧服务进程、超过 30 秒以及时间超前超过 5 秒的事件。桥接器重启时允许恢复旧的 `idle` 快照；旧的非待机快照会被安全降级为 `idle`。

## 诊断

- 语音发布状态：`/run/hermes-voice-control/state.json` 的 `expression_events`
- 最新事件快照：`/run/hermes-voice-control/expression-event.json`
- 桥接消费状态：`/run/riverbank-expression/voice-event-bridge.json`
- 服务日志：`journalctl -u hermes-expression-bridge.service -f`
