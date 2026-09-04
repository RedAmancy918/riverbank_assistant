# RiverBank Edge 多用户账号与 Chat 隔离

## 目标与边界

RiverBank Edge 使用设备本地账号系统，不依赖公网账号中心。macOS、Windows 和 iOS
客户端日常只显示 `用户名 + 密码`；已保存的 RiverBank Edge Host 收在“连接设置”中，仅在
换设备或排障时修改。密码仅在 HTTPS 登录请求中出现，
服务端验证成功后签发随机、可撤销、最长 30 天的设备会话。客户端之后只携带会话令牌，
不会反复发送密码。

当前隔离范围包括 Chat 会话、消息、附件和后台任务。任务报告文件库与视频通话仍是设备级
能力；报告所有权将在报告索引数据库化时继续收紧。所有隔离都由服务端查询强制执行，
不得仅靠客户端隐藏。

## 身份与权限

- `admin`：使用全部客户端能力，可以创建普通用户或管理员、调整权限、停用和重新启用账号、
  撤销会话，以及清理指定用户的缓存与已结束任务；
- `user`：使用 Chat、任务、报告和视频通话，只能访问自己的 Chat、附件和后台任务；
- `service`：旧设备凭据对应的内部服务身份，只供树莓派本机 Paper Radar 等过渡集成使用，
  远端客户端不能用它访问 Chat。

管理员不能停用自己，服务端也禁止停用或降级最后一名可用管理员。账号停用和密码修改会撤销相关会话；
修改密码时只保留当前设备会话，其他设备需重新登录。

## 首次设置与旧数据迁移

1. 客户端访问 `GET /api/v1/auth/config`，发现设备尚无账号；
2. 选择“注册”，填写用户名、密码和设备私有的注册通行码；
3. 首个账号用户名固定为 `Geo`，服务端原子创建它并授予 `admin`；
4. 旧版普通 Chat 与后台任务自动归属 Geo，Paper Radar 内部文章问询不迁移；
5. 之后持有同一注册通行码的人可以注册普通用户；管理员可在后台决定是否提升权限。

注册通行码仅以 Argon2id/scrypt/PBKDF2 哈希保存在
`/home/geo/.config/riverbank-video-call/registration-passcode.hash`，不写入源码、Git、网页或
账号数据库。它只授权“创建普通账号”，不能登录、读取数据或成为管理员。

首次设置与账号密码登录只允许经 HTTPS，或由树莓派本机 loopback 调用。正式客户端默认
使用 Tailscale Serve 地址 `https://riverbank-tech.tail0acdab.ts.net/assistant`。不要把
`19734/tcp` 直接映射到公网。

## 密码与会话安全

- 首选 Argon2id：19 MiB、2 次迭代、1 lane；
- 若运行环境尚无 Argon2，则兼容 scrypt `N=2^17, r=8, p=1`；极简 Python 构建最终回退
  到 PBKDF2-HMAC-SHA256 600,000 次；已有哈希会按自身算法继续验证；
- 每个密码使用独立随机盐，数据库从不保存明文密码；
- 会话令牌包含 256 bit 随机量，服务端只保存 SHA-256 摘要；
- macOS/Windows Electron 客户端通过 Electron `safeStorage` 使用 Keychain/DPAPI 保存会话；
- iOS 使用 `kSecAttrAccessibleAfterFirstUnlockThisDeviceOnly` Keychain；
- 密码不会进入 UserDefaults、localStorage、日志、Git 或客户端配置文件；
- 同一用户名 10 分钟内连续失败 5 次后临时限流，错误文案不区分“用户不存在”和“密码错”。

账号库位于 `/var/lib/riverbank-tasks/auth.db`，权限为 `0600`；聊天库仍位于
`/var/lib/riverbank-tasks/chat.db`。两者都应进入设备加密备份，但禁止提交到 Git。

## 服务端强制隔离

`chat_conversations.owner_user_id` 与 `tasks.owner_user_id` 是隔离的可信依据。所有 Chat 列表、详情、发消息、
停止、删除和附件下载查询都同时匹配会话 ID 与当前用户 ID。访问其他用户的资源返回 404，
任务列表、详情、补充信息与取消接口也做同样匹配。客户端即使修改请求或猜到 ID 也不能越权。`activeChatID` 也按用户 ID 分别保存在客户端，
避免切换账号时沿用另一个账号的界面状态。

## API

- `GET /api/v1/auth/config`：是否需要首次设置；
- `POST /api/v1/auth/bootstrap`：使用一次性设备凭据创建首位管理员；
- `POST /api/v1/auth/login`：账号密码登录；
- `POST /api/v1/auth/register`：通过注册通行码创建账号；首个账号必须是 Geo；
- `GET /api/v1/auth/me`：恢复并验证当前会话；
- `POST /api/v1/auth/logout`：撤销当前会话；
- `POST /api/v1/auth/change-password`：修改密码并撤销其他设备会话；
- `GET/POST /api/v1/auth/users`：管理员查看和创建用户；
- `PATCH /api/v1/auth/users/{id}`：管理员调整权限、停用或启用用户；
- `GET /api/v1/admin/users`：管理员读取账号、会话数、缓存量和任务统计；
- `POST /api/v1/admin/users/{id}/revoke-sessions`：让指定用户的所有设备退出；
- `DELETE /api/v1/admin/users/{id}/cache`：清理该用户 Chat 与附件；
- `DELETE /api/v1/admin/users/{id}/tasks`：清理该用户已结束的任务。

管理员网页的远程正式地址为
`https://riverbank-tech.tail0acdab.ts.net/assistant/admin/`；树莓派本机排障地址为
`http://127.0.0.1:19734/admin/`。网页本身只显示登录壳，所有真实数据接口均会
再次校验 `admin` 角色；会话令牌只放在浏览器当前标签页的 `sessionStorage`，关闭标签即清除。
清缓存和清任务需要二次确认，存在正在生成的回答或活动任务时服务端拒绝操作。

## 运维与恢复

服务升级必须同时部署 `auth_store.py`、`video_call_server.py`、`chat_store.py` 与
`argon2-cffi` 依赖。回归至少运行：

```bash
python3 -m unittest tests.test_auth_store tests.test_chat_store tests.test_task_store -v
```

忘记管理员密码时不提供远程“找回密码”接口，避免它成为绕过认证的后门。应通过受信任的
树莓派本机维护会话执行账号恢复工具，或从加密备份恢复 `auth.db`。直接删除 `auth.db` 会
进入首次设置状态，但这是破坏性恢复操作，必须先备份数据库并由设备所有者明确授权。
