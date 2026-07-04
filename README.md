# 群管理助手 v2.1 — LLM 自主管理 QQ 群插件

**v2.1 | 18 个管理 Tool + 15 条快捷命令 + 5 个 HookHandler（Replyer 双路注入 + Planner 注入 + 守门 + 缓存）**，让 Bot 自主监控群聊、识别违规并执行管理操作（禁言、踢人、撤回、设精华、公告、审批入群等），同时提供人类管理员的命令控制台。

---

## ⚠️ 免责声明

1. **本插件为娱乐性质的自动化工具**，不保证管理决策的准确性和适当性。LLM 的判断可能存在误判（将正常对话误认为违规）或漏判（未识别真正的违规内容）。
2. **使用者需自行承担风险**。因本插件自动执行的管理操作（禁言、踢人等）引发的任何纠纷、损失或账号风险，插件开发者不承担任何责任。
3. **不建议在严肃的管理场景中完全依赖本插件**。建议保持人类管理员对关键决策的监督和干预能力。
4. **请遵守 QQ 平台的使用规范**，合理设置禁言时长和操作频率，避免因频繁操作导致 Bot 账号被限制。
5. **Bot 必须是群管理员或群主**才能执行管理操作。如果 Bot 是普通成员，所有管理 Tool 将无法使用。
6. 本插件基于 MaiBot Plugin SDK v2 和 NapCat 适配器开发，不保证与其他适配器或 SDK 版本的兼容性。

---

## 📌 版本兼容性

- **稳定运行版本**：MaiBot **1.0.0 ~ 1.0.7**
- **≥1.0.8 概率不适配**：MaiBot 核心 `hook_dispatcher` 的 `kwargs` 替换逻辑变更（`= dict(...)` 完全替换而非合并 `update`），导致多插件共用同一 Hook 点时后注册的插件拿不到 `session_id` 等关键参数，表现为提示词注入失效。此问题由 MaiBot 核心修改引起，与本插件代码无关。
- **本插件 v2.1 理论上已修复该问题**：`cache_session_group` 从 `message.session_id` 字段直接提取会话 ID，不依赖 `kwargs` 传递，降低对核心变更的耦合。

---

## 定位说明

本插件设计为**轻量娱乐向**的群管理辅助工具，核心理念是：

- **LLM 自主判断**：Bot 根据上下文自行决定何时操作，无需人工逐一指令
- **人类兜底**：通过 `/admin` 命令和 `exempt_users` 等机制，管理员可随时纠正或阻止 Bot 的操作
- **安全优先**：默认配置保守（`daily_mute_limit=10`、`max_mute_duration=3600s`、`auto_exempt_admins=true`），建议先在测试群试用

**如需用于正式群管理**，建议：

1. 将 `auto_moderate.enabled` 设为 `false`，仅通过管理员命令手动操作
2. 或设置 `default_action = "ignore"`（自动审批关闭）
3. 将 `protected_users` 配置所有不应被操作的用户
4. 定期通过 `/admin log` 审查操作记录
5. 保持至少一名人类管理员在线监督

---

## 快速开始

### 安装

将 `plugins/maimai_group_admin/` 目录放入 MaiBot 的 `plugins/` 下，确保包含以下文件：

```
plugins/maimai_group_admin/
  _manifest.json    # 插件声明
  plugin.py         # 插件入口，组合所有模块
  plugin_core.py    # 核心生命周期、后台任务、辅助方法
  config_model.py   # 配置模型（10 个配置分区 + 2 个默认提示词）
  tools.py          # 18 个管理 Tool
  commands.py       # 15 个管理员命令
  handlers.py       # 1 个 EventHandler + 5 个 HookHandler
  config.toml       # 配置文件
  __init__.py       # 包初始化
  README.md         # 本说明
```

### 最小配置

编辑 `config.toml`：

```toml
[plugin]
enabled = true

[identity]
bot_qq = "你的Bot的QQ号"    # 推荐填写，留空则自动从消息中获取

[auto_moderate]
enabled_groups = ["123456789"]  # 需要管理的群号，留空=全部群生效

[admin]
admins = ["你的QQ号"]           # 能使用/admin命令的管理员

[auto_approve]
enabled = false                 # 建议初次使用关闭自动审批
default_action = "ignore"
```

### 启用

WebUI → 插件管理 → 找到 `deepseek-v4-pro.maimai-group-admin` → 点击启用

### 验证

在管理的群内发送 `/admin status`，应看到：

```
群 123456789 管理面板
身份：owner
状态：运行中
今日已禁言 0 人（上限 10），已踢出 0 人（上限 3）
```

---

## 配置文件详解

以下是 `config.toml` 每个字段的完整说明，按配置分组列出。

---

### [plugin] — 插件开关

| 字段 | 类型 | 默认值 | 说明 |
|------|------|--------|------|
| `enabled` | bool | `false` | 插件总开关，设为 `true` 后插件才开始工作 |
| `config_version` | string | `"2.1.0"` | 配置版本号，升级插件时用于迁移判断，**请勿手动修改** |

---

### [admin] — 管理员权限

| 字段 | 类型 | 默认值 | 说明 |
|------|------|--------|------|
| `admins` | list[string] | `[]` | 人类管理员 QQ 号列表，**必须填写你的 QQ 号**才能使用 `/admin` 等命令。跨群有效 |
| `allow_group_owner` | bool | `true` | 是否允许目标群的群主执行管理员命令（即使不在 admins 列表中） |
| `owner_allowed_commands` | list[string] | `[]` | 群主可用的命令白名单（如 `["status","log","mute","kick"]`），留空 = 全部可用。已在权限校验中实际执行 |
| `deny_response` | string | `"silent"` | 无权限用户的处理方式：`"silent"`=静默忽略，`"reply"`=回复"你没有权限执行此操作" |

> **重要**：`admins` 必须至少填一个 QQ 号，否则仅群主能用管理命令。

---

### [identity] — 身份标识

| 字段 | 类型 | 默认值 | 说明 |
|------|------|--------|------|
| `bot_nickname` | string | `"麦麦"` | Bot 昵称，会出现在管理 prompt 和通知中。建议与 Bot 的人设名称一致 |
| `auto_detect` | bool | `true` | 是否自动检测 Bot 在各群的权限角色（群主/管理员/普通成员） |
| `bot_qq` | string | `""` | Bot 的 QQ 号。**强烈推荐填写**，留空则从首次群消息事件中自动获取 |
| `override_roles` | dict[str,str] | `{}` | 手动覆盖指定群的 Bot 角色。格式：`"群号" = "owner"`（可选值：`owner`/`admin`/`member`）。优先级高于自动检测 |

> `override_roles` 示例：
> ```toml
> [identity.override_roles]
> "123456789" = "owner"
> "987654321" = "admin"
> ```

---

### [auto_moderate] — 自动审核

| 字段 | 类型 | 默认值 | 说明 |
|------|------|--------|------|
| `enabled` | bool | `true` | 是否启用 LLM 自动审核（双路注入 + 按群精确注入 + Planner 决策注入） |
| `enabled_groups` | list[string] | `[]` | 需要管理的群号白名单，如 `["123456789"]`。留空 = 全部群生效 |

> **注意**：`enabled_groups` 留空时插件会在所有群启用自动审核。填写后仅白名单内的群生效。

---

### [safeguard] — 安全管理

| 字段 | 类型 | 默认值 | 说明 |
|------|------|--------|------|
| `max_mute_duration` | int | `3600` | 单次禁言最大秒数（1 小时）。LLM 请求的超长禁言会被截断到该值 |
| `kick_require_confirm` | bool | `true` | 踢人前是否要求 LLM 先调用 `group_get_member` 确认目标身份 |
| `mute_cooldown` | int | `300` | 同一用户两次禁言的最小间隔（秒）。已实际执行，tool_mute_user 和 /mute 命令均会检查 |
| `daily_mute_limit` | int | `10` | 每个群每天最大禁言次数（防止误操作风暴） |
| `daily_kick_limit` | int | `3` | 每个群每天最大踢人次数 |
| `protected_users` | list[string] | `[]` | **全局保护名单**，这些 QQ 号在任何群里都不会被操作。建议填群主和重要成员 |
| `exempt_users` | dict[str,list] | `{}` | **按群豁免名单**，格式见下方示例。通过 `/admin ban`/`unban` 命令也可添加 |
| `auto_exempt_admins` | bool | `true` | 是否自动豁免群主和管理员（系统硬拦截，LLM 无法操作他们） |

> `exempt_users` 示例：
> ```toml
> [safeguard.exempt_users]
> "123456789" = ["111222333", "444555666"]
> ```

---

### [warning] — 警告系统

| 字段 | 类型 | 默认值 | 说明 |
|------|------|--------|------|
| `enabled` | bool | `true` | 是否启用警告计数器 |
| `spam_warn_threshold` | int | `3` | 刷屏类警告次数达到该值后系统提示升级处罚 |
| `spam_warn_window` | int | `600` | 刷屏警告计数窗口（秒），超出窗口的旧警告自动过期 |
| `abuse_warn_threshold` | int | `1` | 辱骂类警告阈值（建议设低，辱骂零容忍） |
| `abuse_warn_window` | int | `3600` | 辱骂警告计数窗口（秒） |
| `ad_warn_threshold` | int | `1` | 广告类警告阈值 |
| `ad_warn_window` | int | `1800` | 广告警告计数窗口（秒） |

> 当某类警告达到阈值时，Tool 返回值会附带 `"该用户 xxx 类警告已达 n/m，建议升级处罚"` 提示。

---

### [escalation] — 处罚阶梯

| 字段 | 类型 | 默认值 | 说明 |
|------|------|--------|------|
| `enabled` | bool | `true` | 是否启用处罚阶梯 |
| `escalation_steps` | list[table] | `[]` | 阶梯规则列表，**默认空=不生效**，需自行配置 TOML 数组 |

每条阶梯规则的字段：

| 子字段 | 类型 | 说明 |
|--------|------|------|
| `within_hours` | int | 回溯多少小时内 |
| `count` | int | 操作次数达到该值后触发 |
| `action` | string | 触发动作：`"mute"` 或 `"kick"` |
| `max_duration` | int | 若 action=mute，禁言最大秒数（覆盖 LLM 请求的时长） |

> 配置示例（TOML 数组格式，每项用 `[[escalation.escalation_steps]]` 开头）：
> ```toml
> [[escalation.escalation_steps]]
> within_hours = 24
> count = 1
> action = "mute"
> max_duration = 600
> 
> [[escalation.escalation_steps]]
> within_hours = 24
> count = 2
> action = "mute"
> max_duration = 1800
> 
> [[escalation.escalation_steps]]
> within_hours = 72
> count = 3
> action = "kick"
> ```

---

### [auto_approve] — 自动审批入群

| 字段 | 类型 | 默认值 | 说明 |
|------|------|--------|------|
| `enabled` | bool | `false` | 是否启用自动审批入群。**建议初次使用保持关闭** |
| `default_action` | string | `"ignore"` | 默认动作：`"ignore"`=不处理，`"approve"`=自动通过，`"reject"`=自动拒绝 |
| `require_message_keywords` | list[string] | `[]` | 入群申请必须包含的关键词（全部满足才按 default_action 处理） |
| `reject_keywords` | list[string] | `[]` | 拒绝关键词，申请中包含任一即自动拒绝 |
| `max_pending_seconds` | int | `300` | 超过此秒数的申请自动跳过（避免处理积压旧申请） |
| `daily_approve_limit` | int | `5` | 每日自动通过上限 |
| `daily_reject_limit` | int | `10` | 每日自动拒绝上限 |
| `check_interval_seconds` | int | `120` | 后台扫描间隔（秒），设为 `0` 禁用后台任务 |
| `groups` | list | `[]` | 按群覆盖设置，TOML 数组表格式。每项字段见下方 |

**groups 子字段：**

| 字段 | 类型 | 默认值 | 说明 |
|------|------|--------|------|
| `group_id` | string | `""` | 群号 |
| `default_action` | string | `"ignore"` | 默认动作: ignore/approve/reject |
| `require_keywords` | string | `""` | 必须包含的关键词，逗号分隔 |
| `reject_keywords` | string | `""` | 拒绝关键词，逗号分隔 |
| `daily_approve_limit` | int | `0` | 每日通过上限（0=使用全局） |
| `daily_reject_limit` | int | `0` | 每日拒绝上限（0=使用全局） |

> 配置示例：
> ```toml
> [[auto_approve.groups]]
> group_id = "123456789"
> default_action = "approve"
> reject_keywords = "广告, 推广"
> daily_approve_limit = 5
> daily_reject_limit = 10
> 
> [[auto_approve.groups]]
> group_id = "987654321"
> default_action = "ignore"
> ```

---

### [logging] — 日志与记录

| 字段 | 类型 | 默认值 | 说明 |
|------|------|--------|------|
| `max_log_entries` | int | `2000` | 内存中保留的操作日志最大条数（超过后自动丢弃旧记录） |
| `default_log_lines` | int | `10` | `/admin log` 不加行数参数时的默认显示行数 |
| `verbose_logging` | bool | `false` | **v1.1 新增**。开启后输出完整注入 prompt 和守门详情到 INFO 日志，用于排查提示词效果 |

---

### [prompts] — 提示词

| 字段 | 类型 | 默认值 | 说明 |
|------|------|--------|------|
| `auto_moderate_system` | string | (长文本) | 自动审核系统提示词（Replyer 用），支持 `{bot_role}` 和 `{available_actions}` 模板变量。可自定义 |
| `planner_moderate_system` | string | (长文本) | **v2.0 新增**。规划器系统提示词（Planner 决策用），支持 `{bot_role}` 和 `{available_actions}` 模板变量 |
| `command_denied_message` | string | `"你没有权限执行此操作。"` | 非授权用户尝试使用管理命令时的回复内容（仅 deny_response="reply" 时生效） |

> `auto_moderate_system` 和 `planner_moderate_system` 均支持模板变量：`{bot_role}`（群主/管理员/普通成员）、`{available_actions}`（动态可用工具列表）

---

## 功能详解

### 一、LLM 自动管理层（18 个 Tool + 5 个 HookHandler）

Bot 通过 chat.receive.after_process HookHandler 缓存 msg_id → group_id，为双路注入（`before_request → extra_prompt` + `before_model_request → messages`）提供精确的群号映射。每次 LLM 思考时按群注入管理上下文，Planner/Timing Gate/Replyer 全部具备管理意识。回复后 `after_response` HookHandler 守门检查不当行为。

> **v1.4 变更**：新增 `chat.receive.after_process` 缓存钩子解决双路注入无法获取群号的根本问题，实现按群精确注入（每个群获取真实 bot 角色），未启用群和私聊自动跳过。不再依赖配置中第一个群的硬编码角色。

#### 写操作（14 个）

| Tool | 参数 | 最低权限 | 说明 |
|------|------|----------|------|
| `group_warn_user` | group_id, user_id, violation_type(spam/abuse/ad), reason | 管理员 | 发送警告消息 + 写入警告计数器，阈值达标后提示升级 |
| `group_mute_user` | group_id, user_id, duration(秒), reason | 管理员 | 禁言指定用户，受 max_mute_duration 限制 |
| `group_unmute_user` | group_id, user_id | 管理员 | 解除禁言（duration=0） |
| `group_kick_user` | group_id, user_id, reason | 管理员/群主 | 踢出用户。管理员需在严重违规请示群主或群主要求时使用 |
| `group_recall_msg` | group_id, message_id, reason | 管理员 | 撤回消息（群主/管理员无2分钟限制，需先回复目标消息获取 message_id） |
| `group_set_essence` | group_id, message_id | 管理员 | 设为精华消息（需先让用户回复目标消息获取 message_id） |
| `group_unset_essence` | group_id, message_id | 管理员 | 取消精华 |
| `group_set_user_card` | group_id, user_id, card | 管理员 | 修改群名片（只能改普通成员） |
| `group_approve_join` | group_id, request_id, reason(可选) | 管理员 | 通过入群申请 |
| `group_reject_join` | group_id, request_id, reason | 管理员 | 拒绝入群申请 |
| `group_set_name` | group_id, name | **仅群主** | 修改群名称 |
| `group_set_title` | group_id, user_id, title | **仅群主** | 设置专属头衔（最长6字符） |
| `group_post_notice` | group_id, content | 管理员/群主 | 发布群公告，返回 notice_id 供后续删除 |
| `group_delete_notice` | group_id, notice_id | 管理员/群主 | 删除群公告（先用 group_get_notice 获取 notice_id） |

#### 查询（4 个）

| Tool | 参数 | 最低权限 | 说明 |
|------|------|----------|------|
| `group_get_member` | group_id, user_id | 无 | 查询群成员身份（owner/admin/member）、昵称和群名片。踢人/禁言前必须先调用 |
| `group_get_shut_list` | group_id | 管理员 | 查看当前群的禁言列表 |
| `group_get_system_msg` | group_id | 管理员/群主 | 获取群系统消息（入群申请、邀请入群） |
| `group_get_notice` | group_id | 无 | 获取群公告列表（含 notice_id），删除公告前调用 |

#### 动态注册

Tool 全部 18 个静态注册，始终可用。Bot 角色的影响在 prompt 注入环节体现：

- **群主**：注入完整管理权限描述（"全部管理: 禁言/解禁/警告/设精华/撤回/改名片/公告/改名/审批入群/踢人"）
- **管理员**：注入受限描述（"禁言/解禁/警告/设精华/撤回/改名片/公告/审批入群/踢人"）
- **普通成员**：注入提示"你在此群无管理操作权限，可协助管理员做决策建议。"

> 注意：Tool 本身不按角色禁用。若 Bot 为普通成员调用禁言等操作，QQ API 会在执行时返回权限不足的错误。

---

### 二、人类管理员命令（15 个）

所有命令需满足权限校验（`config.admin.admins` 或群主身份）。

#### /admin 控制台（8 个）

| 命令 | 用法 | 说明 |
|------|------|------|
| `/admin status [群号]` | 查看运行状态 | 显示 bot 角色、日计数、启用状态 |
| `/admin off [群号]` | 关闭自动管理 | 从 `enabled_groups` 移除并持久化到 `config.toml`，重启后保留 |
| `/admin on [群号]` | 开启自动管理 | 自动加入 `enabled_groups` 并持久化到 `config.toml`，重启后保留 |
| `/admin undo [群号] @qq` | 强制解禁 | 同时从 exempt_users 移除 |
| `/admin log [群号] [n]` | 操作记录 | 查看最近 n 条操作（默认 10 条） |
| `/admin ban [群号] @qq` | 添加豁免 | 写入 `exempt_users[群号]` |
| `/admin unban [群号] @qq` | 移除豁免 | 从 `exempt_users` 删除 |
| `/admin reload` | 刷新配置 | **仅 admins 列表中的用户**可用 |

#### 快捷操作（7 个）

| 命令 | 用法 | 权限 | 安全护栏 |
|------|------|------|----------|
| `/mute @qq 5分钟 刷屏` | 禁言，支持 QQ 号或昵称 | admins/群主 | 受保护用户/豁免名单检查 |
| `/unmute @qq` | 解禁 | admins/群主 | — |
| `/kick @qq 广告` | 踢出 | admins/群主 | 受保护用户/豁免名单检查 |
| `/warn @qq spam 原因` | 正式警告 | admins/群主 | — |
| `/essence` | 设精华（需先回复目标消息） | admins/群主 | — |
| `/recall` | 撤回（需先回复目标消息） | admins/群主 | — |
| `/shutlist` | 查看禁言列表 | admins/群主 | — |

> **注意**：`/essence` 和 `/recall` 需要先在 QQ 中**回复（引用）目标消息**，然后再发送命令。命令会自动从回复中提取目标消息的 ID。

---

### 三、安全护栏（8 步校验链）

所有 LLM Tool 和 `/mute` `/kick` 快捷命令在执行前均按以下顺序校验：

```
① protected_users（全局保护名单）
    ↓ 命中 → 拒绝
② exempt_users[群号]（按群豁免）
    ↓ 命中 → 拒绝
③ admins（bot管理员，与群主同级保护）
    ↓ 命中 → 拒绝
④ auto_exempt_admins（自动查身份）
    ↓ 目标为群主/管理员 → 拒绝
⑤ mute_cooldown（同用户禁言最小间隔）
    ↓ 未达标 → 拒绝
⑥ 每日限额（每群独立计数）
    ↓ 超额 → 拒绝
⑦ kick_require_confirm（踢人确认）
    ↓ 未调用 group_get_member → 拒绝
⑧ 处罚阶梯（warn/mute/kick 联合计数）
    ↓ 命中 → 自动覆盖 LLM 请求参数
    ↓ 通过 → 执行操作
```

阶梯匹配后系统**自动覆盖** LLM 请求的禁言时长，Tool 返回值中附带提示。

---

### 四、自动审批入群

后台 `asyncio.Task` 定时扫描所有已启用群的入群申请。

**处理逻辑**：

1. 获取系统消息 → 提取 `join_requests`
2. 遍历 `groups` 数组，匹配 `group_id` 找到该群的覆盖配置
3. 无匹配时使用全局 `default_action`/`require_message_keywords`/`reject_keywords`/`daily_*_limit`
4. 超过 `max_pending_seconds` 的申请自动跳过
5. `reject_keywords` 命中 → 拒绝；`require_keywords` 未满足 → 忽略
6. 执行 approve/reject，受限额约束

**配置示例**（自动通过含"同意协议"的申请，拒绝含"广告"的申请）：

```toml
[auto_approve]
enabled = true
default_action = "approve"
require_message_keywords = ["同意协议"]
reject_keywords = ["广告", "推广", "代练"]
max_pending_seconds = 300
daily_approve_limit = 10
daily_reject_limit = 10
check_interval_seconds = 60
```

---

### 五、提示词系统

#### 注入架构（v2.1）

```
消息到达 → EventHandler(追踪: 群号映射/计数/角色缓存)
                │
LLM 每次思考（Planner / Replyer / Timing Gate）
    ├── HookHandler: before_request → extra_prompt 注入管理 prompt
    ├── HookHandler: before_model_request → messages 直注管理 prompt
    │       └── 双路注入互相补充，确保所有子代理都看到管理上下文
    │
    └── HookHandler: after_response → 守门检查
            └── Bot 有管理权限却说"没权限"时自动替换回复
```

#### 管理上下文 Prompt（v2.1 精简版）

```
【群管理参考 — 保持人设，自然融入】

身份：{bot_role}  可用操作：{available_actions}

发现违规时自然处理，不要解释操作、不要切换管理员口吻：
  广告/诈骗 → 撤回 + 禁言10-30分钟
  连续刷屏 → 提醒一句，仍继续再禁言5-10分钟
  辱骂/人身攻击 → 撤回 + 禁言1-6小时，再犯踢出
  色情/违法 → 撤回 + 踢出
  高质量分享 → 设精华表达赞赏
  不确定 → 先观察，别着急动手

操作前先用 group_get_member 确认目标身份；撤回/精华需先回复目标消息获取 message_id

节奏：正常聊天，发现违规再处理。不要说"已将xxx禁言"这类话
```

> **v2.1 优化**：标题精简为"保持人设，自然融入"；移除冗余的"不要切换管理员口吻"；统一"可用工具"为"可用操作"；将操作指引合并为一句；节奏控制合并为一句。

---

### 六、权限体系

#### 命令权限（三层校验，取并集）

| 优先级 | 条件 | 适用范围 |
|--------|------|----------|
| 1 | `config.admin.admins` 中的 QQ 号 | 跨群有效，不受任何限制 |
| 2 | 发送者为目标群群主 + `allow_group_owner=true` | 当前群，受 `owner_allowed_commands` 白名单限制 |
| 3 | admins 为空时默认仅群主可用 | 安全默认值 |

#### Bot 角色检测

- **自动检测**（`auto_detect=true`）：首次收到群消息时调用 `get_group_member_info(self_id)` 获取角色
- **手动覆盖**：`identity.override_roles` 配置优先级高于自动检测
- **配置 bot_qq**：推荐填写 `identity.bot_qq`，避免因未收到消息事件导致检测失败
- **刷新周期**：每 30 分钟自动刷新一次

---

## 推荐配置

### 娱乐向（默认适合）

```toml
[plugin]
enabled = true

[auto_moderate]
enabled_groups = ["你的群号"]

[safeguard]
max_mute_duration = 3600
daily_mute_limit = 10
daily_kick_limit = 3
auto_exempt_admins = true

[auto_approve]
enabled = false
```

### 正式管理向（保守）

```toml
[auto_moderate]
enabled = false    # 关闭自动审核，仅用管理员命令

[safeguard]
max_mute_duration = 600     # 最大10分钟
daily_mute_limit = 5        # 保守上限
daily_kick_limit = 1
protected_users = ["群主QQ", "其他管理QQ"]

[admin]
admins = ["你的QQ"]
deny_response = "reply"     # 无权限时回复提示
```

### 严格过滤向

```toml
[warning]
spam_warn_threshold = 1     # 首次刷屏就警告
abuse_warn_threshold = 0    # 辱骂直接处罚不警告

[escalation]
[[escalation.escalation_steps]]
within_hours = 24
count = 1
action = "mute"
max_duration = 1800          # 首次就禁言30分钟
```

---

## 常见问题

### Q: `/admin status` 显示 bot 角色为"未知"

**原因**：Bot 未收到过群消息事件，或 `bot_qq` 未配置。

**解决**：
1. 在 `config.toml` 中设置 `[identity] bot_qq = "你的Bot的QQ号"`
2. 或在群内发送一条消息触发角色检测
3. 或手动设置 `[identity.override_roles] "群号" = "owner"`

### Q: LLM 不响应管理请求（只会说"我不会"）

**原因**：Bot 的人设优先级高于管理 prompt，v1.0 的规章制度式 prompt 尤其容易被忽略。

**解决**：
1. 确保 Bot 在群内是管理员/群主
2. 检查 `auto_moderate.enabled = true`
3. **v1.1 已优化**：新 prompt 开头声明"保持你原本人设"，强调融入语气，与人设冲突大幅降低
4. 开启 `logging.verbose_logging = true` 可在日志中看到每次注入的完整 prompt，确认是否到位
5. 如仍不行，在 `auto_moderate_system` 中进一步定制与人设协调的措辞

### Q: `/mute @昵称` 提示"未找到成员"

**原因**：昵称匹配需要先调用 `get_group_member_list` 获取成员列表。

**解决**：使用 QQ 号代替昵称：`/mute @123456789 5分钟`

### Q: 快捷命令（`/mute` `/kick` 等）无法识别

**原因**：新增的 Command 需要 WebUI 完整重载才能注册。

**解决**：WebUI → 插件管理 → 禁用 → 启用（不能仅用 `/admin reload`）

### Q: `send.hybrid` 权限被拒绝

**原因**：manifest 中的 `capabilities` 未包含 `send.hybrid`。

**解决**：确认 `_manifest.json` 中 `capabilities` 包含 `"send.hybrid"`，然后 WebUI 完整重载

### Q: 自动审批不工作

**原因**：`default_action = "ignore"` 或全局 `enabled = false` 且无 per-group 覆盖。

**解决**：设置全局 `enabled = true` 并 `default_action = "approve"/"reject"`，或在 `groups` 中为指定群添加覆盖配置

### Q: 自动审批处理了其他群的申请

**原因**：`get_group_system_msg` 可能返回全部群的系统消息。

**解决**：插件已内置 `req.group_id` 过滤，会跳过不匹配的群

### Q: 处罚阶梯不生效

**原因**：`escalation_steps` 配置为空列表。

**解决**：在 `config.toml` 中配置 `[[escalation.escalation_steps]]` 条目（使用 TOML 数组格式，不是内联表）

### Q: `/admin reload` 后修改不生效

**原因**：`/admin reload` 只刷新内存配置，不重新注册组件。

**解决**：代码修改、新增 Tool/Command/HookHandler 需要 WebUI 完整重载（禁用 → 启用）

### Q: v1.1 升级后提示词注入没生效

**原因**：v1.1 新增的 HookHandler 需要 WebUI 完整重载才能注册。

**解决**：WebUI → 插件管理 → 禁用 → 启用。仅 `/admin reload` 不会注册新的 hook 点。

### Q: 排查提示词是否注入到位

**解决**：设置 `logging.verbose_logging = true`，然后 `/admin reload`，日志中将输出每次注入的完整 prompt 和守门动作。

### Q: 操作日志/计数器重启后丢失

**原因**：所有运行时状态（日志、计数器、豁免名单）存储在内存中。

**解决**：这是设计决定，`/admin ban/unban` 的修改如需持久化请直接编辑 `config.toml`

---

## 日志参考

所有功能输出 `[群管理]` 前缀日志，方便排查问题：

| 日志前缀 | 含义 |
|----------|------|
| `Tool-mute / Tool-kick / Tool-warn / ...` | LLM 调用管理 Tool |
| `Cmd-status / Cmd-mute / Cmd-off / ...` | 管理员命令执行 |
| `角色检测结果: group=... role=...` | Bot 身份识别 |
| `注入管理 prompt: group=... role=...` | HookHandler 直注（v1.4: 按群精确注入） |
| `注入检测: group_id=...` | v1.4 verbose_logging 注入诊断 |
| `守门拦截: Bot(role=...)错误宣称无权限` | after_response 守门触发（v1.1） |
| `守门改写回复: group=...` | 守门已替换回复内容（v1.1） |
| `自动检查入群申请: groups={...}` | 自动审批扫描开始 |
| `入群申请详情 / 入群申请决策` | 审批决策过程 |
| `自动通过入群 / 自动拒绝入群` | 审批执行结果 |
| `操作被拦截: ...` | 安全护栏拦截 |
| `跨日清零: group=...` | 每日计数器重置 |

---

## 技术细节

- **平台**：QQ（NapCat / MaiBot1.0-1.99）
- **SDK**：MaiBot Plugin SDK v2
- **适配器**：MaiBot-Napcat-Adapter
- **提示词注入**：v2.0 三阶段注入 — `chat.receive.after_process` 缓存映射 → `maisaka.planner.before_request`（Planner 决策准则） → `maisaka.replyer.before_request` + `before_model_request`（Replyer 自然语言提示）
- **守门**：`after_response` HookHandler 拦截 Bot 错误宣称无权限的回复，动态替换为"我是{群主/管理员}，我来处理。"
- **并发安全**：`asyncio.Lock` 保护所有共享状态
- **API 调用**：群管理核心操作使用 `_call_api`（直接 kwarg），系统消息/审批使用 `_call_action_api`（params 包装）
- **依赖**：tomlkit（配置持久化读写）
- **许可证**：GPL-v3.0-or-later

---

## v2.1 功能总览

| 模块 | 数量 | 详情 |
|------|:---:|------|
| 管理 Tool | 18 | warn / mute / unmute / kick / recall / set_essence / unset_essence / card / title / name / approve_join / reject_join / post_notice / delete_notice / get_member / get_shut_list / get_system_msg / get_notice |
| 快捷命令 | 15 | /admin(status\|off\|on\|undo\|log\|ban\|unban\|reload) + /mute / /unmute / /kick / /warn / /essence / /recall / /shutlist |
| HookHandler | 5 | chat.receive(缓存) / planner.before_request(Planner注入) / replyer.before_request(extra_prompt) / replyer.before_model_request(messages) / replyer.after_response(守门) |
| EventHandler | 1 | 追踪（群号映射/计数/角色缓存） |
| 安全护栏 | 8 步 | protected_users → exempt_users → admins → auto_exempt → mute_cooldown → 每日限额 → kick_confirm → 处罚阶梯 |
| 配置分区 | 10 | plugin / admin / identity / auto_moderate / safeguard / warning / escalation / auto_approve / logging / prompts |
| 自动审批 | 支持 | 全局 + 按群独立覆盖（TOML 数组表），关键词过滤 + 每日限额 |
| 警告系统 | 支持 | spam / abuse / ad 三类，可配阈值和计数窗口 |
| 处罚阶梯 | 支持 | 按回溯小时数和操作次数自动升级 mute→kick |
| 角色感知 | 支持 | 自动检测 Bot 在各群的 owner/admin/member 身份，注入对应权限描述 |
| 并发安全 | asyncio.Lock | 所有 Tool 和后台任务共享一把锁 |

---

## 更新日志

### v2.1.0 (2026-07-05)

**命令系统修复与配置持久化 + 安全修复与提示词优化（共 17 项）**

**命令签名修复（2 项）**
- 所有 15 个命令 handler 添加 `user_id` / `matched_groups` 显式参数，匹配 SDK 通过函数签名反射传参的机制，解决命令因提取不到 sender 和参数被静默拒绝的问题。
- `_check_admin_permission` 签名改为直接接收 `user_id`，不再从 `**kwargs` 中猜测，权限校验准确率 100%。

**配置持久化（3 项）**
- `/admin ban` / `/admin unban` / `/admin undo` 修改豁免名单后自动写入 `config.toml`，重启后保留。
- `/admin on` / `/admin off` 自动增删 `enabled_groups` 并持久化到 `config.toml`，重启后保留。
- 新增 `_save_exempt_users()` 和 `_save_enabled_groups()` 方法，使用 `tomlkit` 直接读写配置文件。

**Bug 修复（6 项）**
- 修复 `enabled_groups` 为空（全部群启用）时 HookHandler 注入静默跳过的问题：`_prepare_injection` 和 `inject_admin_planner_prompt` 改为使用 `_is_group_enabled` 统一判断。
- 修复 `tool_recall_msg` 使用 `_to_int(message_id)` 导致字符串类型消息ID被转成 0 的撤回失败 bug，改为直接传递原始 message_id。
- 修复 `inject_admin_model_prompt` 无差别写入 `content_text` 字段可能破坏部分模型的消息格式，改为仅在原始消息包含该字段时写入。
- 修复 README 插件 ID 错误（`maimai.group-admin` → `deepseek-v4-pro.maimai-group-admin`）、`config_version` 默认值错误（`"2.0.0"` → `"2.1.0"`）。
- 修复 `_manifest.json` 的 `dependencies` 格式：缺少 discriminator `type` 字段、字段名 `version` 应为 `version_spec`、`reason` 不被 SDK schema 接受。
- 修复权限描述不一致：`_ACTIONS_BY_ROLE` 补全管理员可用的"公告"和"踢人"；`tool_kick_user` 取消对管理员的拦截改为 `bot_role not in ("owner", "admin")`，描述改为"管理员需在严重违规请示群主或群主要求时使用"；`group_post_notice`/`group_delete_notice` 从"仅群主"改为"管理员/群主可用"；其余 8 个 Tool 补全权限标注；README 查询表补全最低权限列。

**配置补充（1 项）**
- `config.toml` 新增 `planner_moderate_system` 字段，与 `config_model.py` 默认值对齐。

**提示词优化（3 项）**
- `auto_moderate_system` 标题精简为"保持人设，自然融入"，删除冗余的"不要切换管理员口吻"（已在正文中体现），合并节奏控制表述。
- `planner_moderate_system` 从工具名导向改为行为导向（`group_recall_msg 撤回` → `撤回`），扁平化结构，尾部强调词更简洁。
- 两个提示词同步更新 `config_model.py` 默认值和 `config.toml` 实际配置。

### v2.0.0 (2026-07-03)

**重大架构重构与提示词体系升级（15 项）**

**多文件模块化（6 项）**
- 将单文件 1736 行 `plugin.py` 拆分为 6 个模块文件：`config_model.py`、`plugin_core.py`、`tools.py`、`commands.py`、`handlers.py`、`plugin.py`
- 采用 Python Mixin 多继承模式，每个模块职责单一、便于维护
- 清理 `plugin_core.py` 中未使用的 import（减少 9 个冗余导入）
- `HandlerMixin` 类注释修正为 5 个 HookHandler

**Planner 阶段注入（3 项）**
- 新增 `maisaka.planner.before_request` HookHandler，向 Planner 的 system messages 注入群管理决策准则
- Planner 提示词独立于 Replyer 提示词，`planner_moderate_system` 使用 `# 群管理准则` 标题风格匹配系统 prompt
- Planner 注入使用独立的 `_build_admin_planner_prompt` 构建方法

**缓存修复（2 项）**
- `cache_session_group` 钩子改为从 `message.session_id` 字段直接提取会话 ID 缓存映射
- 修复 `chat.receive.after_process` 不传 `session_id` 进 kwargs 导致 Planner 注入找不到群号的问题
- Planner hook 找到群号后立即回写 `_stream_to_group[session_id]` 供后续轮次使用

**提示词优化（4 项）**
- 角色名中文化：`{bot_role}` 输出 `群主`/`管理员`/`普通成员` 而非英文 owner/admin/member
- 权限列表统一：`_ACTIONS_BY_ROLE` 共享字典，owner/admin/member 各自对应正确的可用操作（admin 含踢人但需征求群主同意）
- 守门回复动态化：替换文本从硬编码 `"收到，我来处理。"` 改为 `"我是{群主/管理员}，我来处理。"`
- 18 个 Tool 描述规范化：统一 `"做什么（谁可用）"` 格式，移除操作流程混入

### v1.5.0 (2026-06-30)

**全面质量修复（22 项）**

**缓存生命周期（8 项）**
- `_known_roles` 值改为 `(role, timestamp)` 元组，读写均带 3600s TTL，清理按时间戳排序淘汰
- `_last_mute_time` / `_get_member_called` 在 `_cleanup_memory` 中按时清理过期条目
- `_daily_*_count` 在 `_check_daily_reset` 中清理旧日 key
- 删除死代码 `_msg_counter`
- 新增独立 `_cleanup_task`（每 600s 运行），不再依赖 `auto_approve` 或事件驱动
- `_bot_self_id` 改为全局单值 `Optional[int]`，从任意群首次消息即可赋值

**跨群统计隔离（5 项）**
- `_warnings` 结构改为 `{group_id: {user_id: {vtype: [(ts,c)]}}}`，所有读写按群隔离
- `_check_escalation` / `_count_ops_in_window` 加入 `group_id` 过滤
- `_check_warning_threshold` 新增 `group_id` 参数
- `_check_join_requests` 合并 `auto_moderate.enabled_groups` + `auto_approve.groups[]`
- 修复 `auto_approve.groups` 中单独配置的群被 `_is_group_enabled` 跳过的 bug

**竞态条件（2 项）**
- `_check_join_requests` 中 approve/reject 分支的计数修改加 `async with self._lock`
- `cmd_admin_warn` 写入 `_warnings` 加 `async with self._lock`

**管理员体验（4 项）**
- `/admin reload` 新增 `_clear_runtime_cache()` 清空角色/群组/流映射等缓存
- `on_config_update` 自动重启 `_auto_check_task`
- 自动审批前加入 `_is_protected` 检查
- `tool_warn_user` 优先使用 `stream_id` 发送消息

**异常处理统一（27 处）**
- 全 17 个 Tool + 2 个后台循环 + 1 个 Command：`logger.error("...", exc_info=True)`（含完整 traceback）
- 5 个 API 层 helper：`logger.warning(f"...: {e}")`（简洁消息）
- 2 个数据质量兜底：静默
- `_call_api` / `_call_action_api` 新增日志
- `_resolve_target` 裸 `except: pass` → 加日志
- `_ensure_bot_role` 异常→加日志
- 所有 `[群管理]` 前缀 100% 覆盖
- 6 个 Command 中未使用变量 `data` → `_`

### v1.4.0 (2026-06-28)

**重大安全修复**

- 新增 `chat.receive.after_process` HookHandler 缓存 `msg_id → group_id` 映射，解决 `before_request` / `before_model_request` 双路注入无法获取群号的根本问题。
- 按群精确注入：每个启用群获取真实 bot 角色（owner/admin/member），未启用群和私聊自动跳过。
- 修复 `_ensure_bot_role` 跨群 self_id fallback（`next(iter(...))`）导致权限污染。
- 修复 `_check_admin_permission` group_id=0 时误判 sender 为 owner。
- 修复 `_resolve_group_id_from_hook` 正则猜测和 stream 缓存反向污染。
- 修复 `_get_member_called` 无 TTL，改为时间戳存储（300s 过期）。
- 修复 `_check_join_requests` known_groups 跨群污染和 data 标准化紊乱。
- 所有 18 个 Tool 添加 `group_id <= 0` 前置校验。
- 精简 `_prepare_injection` 从 7 级检测简化为缓存查找，移除 ~110 行死代码。
- 移除未使用的 `_last_inject_time` 字段。

### v1.3.0 (2026-06-26)

**重大重构**

- 双路注入架构：`before_request → extra_prompt` + `before_model_request → messages` 同时注入，彻底解决 Planner/Timing Gate/Replyer 管理上下文缺失导致自动审核形同虚设的问题。
- 精简默认 prompt 约 40%（380→220 中文字符），去除冗余修辞，信息密度更高。
- 移除废弃字段 `re_inject_interval_messages` / `re_inject_interval_seconds`。
- 提取 `_prepare_injection()` 消除重复代码。
- 修复 EventHandler 重复代码块。
- 版本号迭代至 1.3.0，config_version 同步更新。

### v1.1.0 (2026-06-24)

**优化部分**

- 全面优化了插件提示词和提示词注入方式

**Bug 修复**

- 修复 `_check_daily_reset` 在跨日时将整个日计数字典覆写为单日条目，导致历史计数丢失的问题

**清理**

- 移除 `_manifest.json` 中已废弃的 `maisaka.context.append` capability（v1.1 已迁移到 HookHandler + extra_prompt）

**文案优化**

- `/admin status` 输出改为面板卡片风格，禁言/踢人计数不再显示为 `0/10` 进度条格式
- `/admin log` 输出从管道分隔格式改为更紧凑的 `[时间] 状态 动作 @用户 -- 原因` 格式
