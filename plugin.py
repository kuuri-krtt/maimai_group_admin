"""群管理助手 — LLM 自主管理 QQ 群插件。

18 个管理 Tool + 15 条快捷命令，支持禁言/解禁/踢人/警告/设精华/撤回/改名片/
改头衔/改群名/公告发布与删除/入群审批，含 8 步安全护栏 + 按群独立配置。
"""

from __future__ import annotations

import asyncio
import re
import time
from collections import deque
from datetime import datetime, timedelta
from typing import Any, ClassVar, Optional

from maibot_sdk import Command, EventHandler, Field, MaiBotPlugin, PluginConfigBase, Tool
from maibot_sdk.types import EventType, ToolParameterInfo, ToolParamType


# =============================================================================
# 配置模型
# =============================================================================

class PluginSectionConfig(PluginConfigBase):
    __ui_label__ = "插件开关"; __ui_icon__ = "power"; __ui_order__ = 0
    enabled: bool = Field(default=False, description="是否启用插件")
    config_version: str = Field(default="1.0.0", description="配置版本")

class AdminSectionConfig(PluginConfigBase):
    __ui_label__ = "管理员权限"; __ui_icon__ = "shield"; __ui_order__ = 1
    admins: list[str] = Field(default_factory=list, description="人类管理员QQ号列表")
    allow_group_owner: bool = Field(default=True, description="是否允许群主执行/admin命令")
    owner_allowed_commands: list[str] = Field(default_factory=list, description="群主可用命令白名单")
    deny_response: str = Field(default="silent", description="非授权用户行为: silent/reply")

class IdentitySectionConfig(PluginConfigBase):
    __ui_label__ = "身份标识"; __ui_icon__ = "user"; __ui_order__ = 2
    bot_nickname: str = Field(default="麦麦", description="Bot昵称")
    auto_detect: bool = Field(default=True, description="留空自动获取bot角色")
    bot_qq: str = Field(default="", description="Bot的QQ号,留空则从消息事件自动获取")
    override_roles: dict[str, str] = Field(default_factory=dict, description="手动覆盖指定群的bot角色")

class AutoModerateSectionConfig(PluginConfigBase):
    __ui_label__ = "自动审核"; __ui_icon__ = "zap"; __ui_order__ = 3
    enabled: bool = Field(default=True, description="是否启用自动审核")
    re_inject_interval_messages: int = Field(default=10, description="多少条消息后重新注入prompt")
    re_inject_interval_seconds: int = Field(default=1800, description="多少秒后重新注入prompt")
    enabled_groups: list[str] = Field(default_factory=list, description="启用插件的群号白名单")

class SafeguardSectionConfig(PluginConfigBase):
    __ui_label__ = "安全管理"; __ui_icon__ = "shield-off"; __ui_order__ = 4
    max_mute_duration: int = Field(default=3600, description="最大禁言秒数")
    kick_require_confirm: bool = Field(default=True, description="踢人前LLM必须先调用group_get_member")
    mute_cooldown: int = Field(default=300, description="同用户禁言最小间隔秒")
    daily_mute_limit: int = Field(default=10, description="每群每日禁言上限")
    daily_kick_limit: int = Field(default=3, description="每群每日踢人上限")
    protected_users: list[str] = Field(default_factory=list, description="全局保护名单")
    exempt_users: dict[str, list[str]] = Field(default_factory=dict, description="按群豁免列表")
    auto_exempt_admins: bool = Field(default=True, description="自动豁免群主/管理员")

class WarningSectionConfig(PluginConfigBase):
    __ui_label__ = "警告系统"; __ui_icon__ = "alert-triangle"; __ui_order__ = 5
    enabled: bool = Field(default=True, description="是否启用警告系统")
    spam_warn_threshold: int = Field(default=3, description="刷屏警告阈值")
    spam_warn_window: int = Field(default=600, description="刷屏计数窗口(秒)")
    abuse_warn_threshold: int = Field(default=1, description="辱骂警告阈值")
    abuse_warn_window: int = Field(default=3600, description="辱骂计数窗口(秒)")
    ad_warn_threshold: int = Field(default=1, description="广告警告阈值")
    ad_warn_window: int = Field(default=1800, description="广告计数窗口(秒)")

class EscalationStepConfig(PluginConfigBase):
    within_hours: int = Field(default=24, description="回溯小时数")
    count: int = Field(default=1, description="触发次数")
    action: str = Field(default="mute", description="动作: mute/kick")
    max_duration: int = Field(default=600, description="最大禁言秒数(仅mute时有效)")

class EscalationSectionConfig(PluginConfigBase):
    __ui_label__ = "处罚阶梯"; __ui_icon__ = "trending-up"; __ui_order__ = 6
    enabled: bool = Field(default=True, description="是否启用处罚阶梯")
    escalation_steps: list[EscalationStepConfig] = Field(default_factory=list, description="处罚阶梯列表")

class GroupApproveOverrideConfig(PluginConfigBase):
    group_id: str = Field(default="", description="群号")
    default_action: str = Field(default="ignore", description="默认动作: ignore/approve/reject")
    require_keywords: str = Field(default="", description="必须包含的关键词(逗号分隔, 留空=不过滤)")
    reject_keywords: str = Field(default="", description="拒绝关键词(逗号分隔)")
    daily_approve_limit: int = Field(default=0, description="每日自动通过上限(0=使用全局)")
    daily_reject_limit: int = Field(default=0, description="每日自动拒绝上限(0=使用全局)")

class AutoApproveSectionConfig(PluginConfigBase):
    __ui_label__ = "自动审批入群"; __ui_icon__ = "user-plus"; __ui_order__ = 7
    enabled: bool = Field(default=False, description="是否启用自动审批")
    default_action: str = Field(default="ignore", description="默认动作: ignore/approve/reject")
    require_message_keywords: list[str] = Field(default_factory=list, description="必须包含的关键词")
    reject_keywords: list[str] = Field(default_factory=list, description="拒绝关键词")
    max_pending_seconds: int = Field(default=300, description="审批超时秒数")
    daily_approve_limit: int = Field(default=5, description="每日自动通过上限")
    daily_reject_limit: int = Field(default=10, description="每日自动拒绝上限")
    check_interval_seconds: int = Field(default=120, description="自动检查间隔(秒), 0=禁用")
    groups: list[GroupApproveOverrideConfig] = Field(default_factory=list, description="按群覆盖设置(数组)")

class LoggingSectionConfig(PluginConfigBase):
    __ui_label__ = "日志与记录"; __ui_icon__ = "file-text"; __ui_order__ = 8
    max_log_entries: int = Field(default=2000, description="操作日志最大条数")
    default_log_lines: int = Field(default=10, description="/admin log 默认行数")

class PromptsSectionConfig(PluginConfigBase):
    __ui_label__ = "提示词"; __ui_icon__ = "message-square"; __ui_order__ = 9
    auto_moderate_system: str = Field(default=(
        "【角色】你是本群的{bot_role}「{bot_nickname}」，当前群号: {group_name}。\n"
        "  可用操作: {available_actions}\n"
        "\n"
        "【职责】实时监控群聊，对违规行为采取渐进式处罚：口头提醒 → 正式警告 → 禁言 → 踢出。\n"
        "\n"
        "【处罚标准】(duration 单位为秒)\n"
        "  广告/诈骗/钓鱼链接 → 立即撤回 + 禁言 600~1800秒(10~30分钟)\n"
        "  刷屏(连续5+条相同/相似内容) → 先口头提醒，继续刷则禁言 300~600秒(5~10分钟)\n"
        "  人身攻击/辱骂/引战 → 撤回 + 禁言 3600~21600秒(1~6小时)，24h内再犯直接踢出\n"
        "  色情/血腥/违法内容 → 立即撤回 + 踢出(零容忍)\n"
        "  恶意刷表情/长图刷屏 → 口头提醒后禁言 300~600秒(5~10分钟)\n"
        "  高质量内容、技术分享、精彩创作 → 可设精华鼓励\n"
        "  轻微擦边/不确定内容 → 仅观察，不主动操作\n"
        "\n"
        "【调工具前须知】\n"
        "  禁言/警告: 直接传 group_id 和 user_id 即可\n"
        "  踢人: 仅群主可直接踢人。如你为管理员，需先征求群主或bot管理员同意。必须先调 group_get_member 确认目标\n"
        "  撤回/设精华: 需要 message_id，请用户在群里回复(引用)目标消息后获取\n"
        "  入群审批: 先调 group_get_system_msg 获取 request_id\n"
        "  删除公告: 先调 group_get_notice 获取公告列表和 notice_id, 再用 group_delete_notice 删除\n"
        "\n"
        "【注意】\n"
        "  1. 无需操作时直接跳过，不要生成「审查完毕/无异常」之类的回复\n"
        "  2. user_id 从消息的 @mention 或内容中提取，群号已在上方给出\n"
        "  3. 禁言/踢人拦截时(如目标在保护名单)，告知原因即可"
    ), description="自动审核系统提示词")
    command_denied_message: str = Field(default="你没有权限执行此操作。", description="权限拒绝回复")

class GroupAdminConfig(PluginConfigBase):
    plugin: PluginSectionConfig = Field(default_factory=PluginSectionConfig)
    admin: AdminSectionConfig = Field(default_factory=AdminSectionConfig)
    identity: IdentitySectionConfig = Field(default_factory=IdentitySectionConfig)
    auto_moderate: AutoModerateSectionConfig = Field(default_factory=AutoModerateSectionConfig)
    safeguard: SafeguardSectionConfig = Field(default_factory=SafeguardSectionConfig)
    warning: WarningSectionConfig = Field(default_factory=WarningSectionConfig)
    escalation: EscalationSectionConfig = Field(default_factory=EscalationSectionConfig)
    auto_approve: AutoApproveSectionConfig = Field(default_factory=AutoApproveSectionConfig)
    logging: LoggingSectionConfig = Field(default_factory=LoggingSectionConfig)
    prompts: PromptsSectionConfig = Field(default_factory=PromptsSectionConfig)


# =============================================================================
# 插件主类
# =============================================================================

class GroupAdminPlugin(MaiBotPlugin):
    config_model: ClassVar[type[PluginConfigBase] | None] = GroupAdminConfig

    def __init__(self) -> None:
        super().__init__()
        self._group_roles: dict[int, str] = {}
        self._role_refresh_time: dict[int, float] = {}
        self._known_roles: dict[tuple[int, int], str] = {}
        self._bot_self_id: dict[int, int] = {}
        self._stream_to_group: dict[str, int] = {}
        self._disabled_groups: set[int] = set()
        self._msg_counter: dict[int, int] = {}
        self._last_inject_time: dict[int, float] = {}
        self._daily_mute_count: dict[int, dict[str, int]] = {}
        self._daily_kick_count: dict[int, dict[str, int]] = {}
        self._daily_approve_count: dict[int, dict[str, int]] = {}
        self._daily_reject_count: dict[int, dict[str, int]] = {}
        self._warnings: dict[int, dict[str, list[tuple[float, int]]]] = {}
        self._op_log: deque[dict[str, Any]] = deque(maxlen=5000)
        self._get_member_called: dict[int, set[int]] = {}
        self._last_mute_time: dict[tuple[int, int], float] = {}
        self._lock: asyncio.Lock = asyncio.Lock()
        self._auto_check_task: Optional[asyncio.Task] = None
        self._last_cleanup_time: float = 0

    # ===== 生命周期 =====

    async def on_load(self) -> None:
        if not self.config.plugin.enabled:
            return
        self._ensure_op_log_capacity()
        self._start_auto_check()

    async def on_unload(self) -> None:
        self._stop_auto_check()

    async def on_config_update(self, scope: str, config_data: dict[str, Any], version: str) -> None:
        if scope != "self":
            return
        self.set_plugin_config(config_data)
        self._ensure_op_log_capacity()
        if version:
            self.ctx.logger.debug(f"群管理插件配置更新: {version}")

    # ===== 自动审批后台任务 =====

    def _start_auto_check(self):
        interval = self.config.auto_approve.check_interval_seconds
        if interval <= 0:
            return
        has_any_enabled = self.config.auto_approve.enabled
        if not has_any_enabled:
            if self.config.auto_approve.groups:
                has_any_enabled = True
        if not has_any_enabled:
            return
        if self._auto_check_task and not self._auto_check_task.done():
            return
        self._auto_check_task = asyncio.create_task(self._auto_check_loop(interval))
        self.ctx.logger.info(f"[群管理] 入群申请自动检查已启动: 间隔={interval}s")

    def _stop_auto_check(self):
        if self._auto_check_task and not self._auto_check_task.done():
            self._auto_check_task.cancel()
        self._auto_check_task = None

    async def _auto_check_loop(self, interval: int):
        while True:
            try:
                await asyncio.sleep(interval)
                await self._check_join_requests()
                self._cleanup_memory()
            except asyncio.CancelledError:
                break
            except Exception as e:
                self.ctx.logger.error(f"[群管理] 自动检查异常: {e}")

    async def _check_join_requests(self):
        aa = self.config.auto_approve
        known_groups = set(self._group_roles.keys()) | set(self._bot_self_id.keys())
        am_enabled = self.config.auto_moderate.enabled_groups
        if am_enabled:
            known_groups |= {int(g) for g in am_enabled if g}
        if not known_groups:
            return
        self.ctx.logger.info(f"[群管理] 自动检查入群申请: groups={known_groups}")
        now = datetime.now()
        max_age = aa.max_pending_seconds
        for gid in known_groups:
            if not self._is_group_enabled(gid):
                continue
            grp_enabled, grp_default = self._get_aa_enabled_action(gid)
            if not grp_enabled or grp_default == "ignore":
                continue
            ok, data = await self._call_action_api(api_name="adapter.napcat.group.get_group_system_msg", group_id=gid)
            if not ok or not isinstance(data, dict):
                continue
            items = data.get("data", data)
            if not isinstance(items, list):
                items = [items] if items else []
            for item in items:
                if not isinstance(item, dict):
                    continue
                join_list = item.get("join_requests", item.get("JoinRequest", []))
                if not join_list:
                    join_list = item.get("invited_requests", item.get("InvitedRequest", []))
                if not isinstance(join_list, list):
                    join_list = [join_list] if join_list else []
                for req in join_list:
                    if not isinstance(req, dict):
                        continue
                    req_gid = self._to_int(req.get("group_id", req.get("GroupId", gid)))
                    if req_gid != gid:
                        continue
                    request_id = str(req.get("flag", req.get("request_id", req.get("id", ""))))
                    join_msg = str(req.get("message", req.get("comment", req.get("apply_msg", ""))))
                    user_id = str(req.get("user_id", req.get("requester_uin", req.get("uin", ""))))
                    ts = req.get("time", req.get("timestamp", now.timestamp()))
                    try:
                        req_time = datetime.fromtimestamp(float(ts))
                    except Exception:
                        req_time = now
                    if (now - req_time).total_seconds() > max_age:
                        continue
                    if not request_id:
                        continue
                    req_kws, rej_kws = self._get_aa_keywords(req_gid)
                    appr_lim, rej_lim = self._get_aa_limits(req_gid)
                    action = grp_default
                    reject_match = rej_kws and any(k in join_msg for k in rej_kws)
                    require_match = not req_kws or all(k in join_msg for k in req_kws)
                    if reject_match:
                        action = "reject"
                    elif not require_match:
                        action = "ignore"
                    self.ctx.logger.info(f"[群管理] 入群申请决策: gid={req_gid} req={request_id} action={action}")
                    if action == "approve":
                        await self._check_daily_reset(req_gid)
                        today = self._today_key()
                        self._daily_approve_count.setdefault(req_gid, {}).setdefault(today, 0)
                        if self._daily_approve_count[req_gid][today] >= appr_lim:
                            continue
                        ok2, _ = await self._call_action_api(api_name="adapter.napcat.group.set_group_add_request", group_id=req_gid, flag=request_id, approve=True)
                        if ok2:
                            self._daily_approve_count[req_gid][today] += 1
                            self._add_log(req_gid, "approve", self._to_int(user_id), "自动通过", True)
                            self.ctx.logger.info(f"[群管理] 自动通过入群: gid={req_gid} user={user_id}")
                    elif action == "reject":
                        await self._check_daily_reset(req_gid)
                        today = self._today_key()
                        self._daily_reject_count.setdefault(req_gid, {}).setdefault(today, 0)
                        if self._daily_reject_count[req_gid][today] >= rej_lim:
                            continue
                        reason = "含拒绝关键词" if reject_match else "自动拒绝"
                        ok2, _ = await self._call_action_api(api_name="adapter.napcat.group.set_group_add_request", group_id=req_gid, flag=request_id, approve=False, reason=reason)
                        if ok2:
                            self._daily_reject_count[req_gid][today] += 1
                            self._add_log(req_gid, "reject", self._to_int(user_id), reason, True)
                            self.ctx.logger.info(f"[群管理] 自动拒绝入群: gid={req_gid} user={user_id}")

    # ===== 辅助方法 =====

    def _today_key(self) -> str:
        return datetime.now().strftime("%Y-%m-%d")

    def _to_int(self, value: Any) -> int:
        if isinstance(value, int): return value
        s = str(value).strip()
        if not s: return 0
        try: return int(s)
        except ValueError: return 0

    def _ensure_op_log_capacity(self):
        """根据escalation配置计算所需最小日志容量并调整deque maxlen。"""
        maxlen = max(self.config.logging.max_log_entries, 2000)
        if self.config.escalation.enabled and self.config.escalation.escalation_steps:
            max_hours = max((s.within_hours for s in self.config.escalation.escalation_steps), default=0)
            groups = max(len(self.config.auto_moderate.enabled_groups), 5)
            daily_ops = self.config.safeguard.daily_mute_limit + self.config.safeguard.daily_kick_limit + 30
            days = int(max_hours / 24) + 1 if max_hours > 0 else 1
            needed = int(groups * daily_ops * days * 1.5)
            maxlen = max(maxlen, needed)
        if self._op_log.maxlen is None or self._op_log.maxlen < maxlen:
            self._op_log = deque(self._op_log, maxlen=maxlen)

    def _cleanup_memory(self):
        """清理过期内存数据：warnings 过期条目、known_roles/stream_to_group 上限裁剪。"""
        now = time.time()
        max_warn_window = max(
            self.config.warning.spam_warn_window,
            self.config.warning.abuse_warn_window,
            self.config.warning.ad_warn_window,
            3600,
        )
        keep_seconds = max_warn_window * 2
        for uid in list(self._warnings.keys()):
            for vtype in list(self._warnings[uid].keys()):
                self._warnings[uid][vtype] = [
                    (ts, c) for ts, c in self._warnings[uid][vtype]
                    if now - ts <= keep_seconds
                ]
                if not self._warnings[uid][vtype]:
                    del self._warnings[uid][vtype]
            if not self._warnings[uid]:
                del self._warnings[uid]
        if len(self._known_roles) > 2000:
            keys = list(self._known_roles.keys())
            for k in keys[:-1000]:
                del self._known_roles[k]
        if len(self._stream_to_group) > 1000:
            keys = list(self._stream_to_group.keys())
            for k in keys[:-500]:
                del self._stream_to_group[k]
        self._last_cleanup_time = now

    def _get_group_role(self, group_id: int) -> Optional[str]:
        gid_str = str(group_id)
        override = self.config.identity.override_roles
        if gid_str in override: return override[gid_str]
        return self._group_roles.get(group_id)

    def _is_group_enabled(self, group_id: int) -> bool:
        conf = self.config.auto_moderate
        if conf.enabled_groups and str(group_id) not in conf.enabled_groups:
            return False
        return group_id not in self._disabled_groups

    async def _call_api(self, api_name: str, **api_args: Any) -> tuple[bool, Any]:
        try:
            result = await self.ctx.api.call(api_name=api_name, version="1", **api_args)
            return True, result
        except Exception as e:
            return False, str(e)

    async def _call_action_api(self, api_name: str, **params: Any) -> tuple[bool, Any]:
        try:
            result = await self.ctx.api.call(api_name=api_name, version="1", params=params)
            return True, result
        except Exception as e:
            return False, str(e)

    async def _check_daily_reset(self, group_id: int):
        today = self._today_key()
        for cnt_dict in (self._daily_mute_count, self._daily_kick_count, self._daily_approve_count, self._daily_reject_count):
            if group_id not in cnt_dict: cnt_dict[group_id] = {}
            if today not in cnt_dict[group_id]:
                cnt_dict[group_id] = {today: 0}

    async def _check_target_role(self, group_id: int, user_id: int) -> Optional[str]:
        key = (group_id, user_id)
        if key in self._known_roles: return self._known_roles[key]
        try:
            ok, data = await self._call_api(api_name="adapter.napcat.group.get_group_member_info", group_id=group_id, user_id=user_id, no_cache=True)
            if ok and isinstance(data, dict):
                role = data.get("role", "")
                self._known_roles[key] = role
                return role
        except Exception as e:
            self.ctx.logger.warning(f"查询用户身份失败: {e}")
        return None

    async def _is_protected(self, group_id: int, user_id: int) -> tuple[bool, str]:
        user_str = str(user_id)
        if user_str in self.config.safeguard.protected_users:
            return True, "该用户在全局保护名单中，不能操作"
        group_exempt = self.config.safeguard.exempt_users.get(str(group_id), [])
        if user_str in group_exempt:
            return True, "该用户在本群豁免名单中，不能操作"
        if user_str in self.config.admin.admins:
            return True, "该用户是bot管理员，与群主同级保护"
        if self.config.safeguard.auto_exempt_admins:
            role = await self._check_target_role(group_id, user_id)
            if role in ("owner", "admin"):
                return True, f"目标是本群{role}，系统自动保护"
        return False, ""

    async def _ensure_bot_role(self, group_id: int) -> Optional[str]:
        existing = self._get_group_role(group_id)
        if existing and (time.time() - self._role_refresh_time.get(group_id, 0) < 1800):
            return existing
        if self.config.identity.auto_detect:
            try:
                self_id = None
                if self.config.identity.bot_qq and self.config.identity.bot_qq.strip():
                    self_id = self._to_int(self.config.identity.bot_qq)
                if not self_id:
                    self_id = self._bot_self_id.get(group_id)
                if not self_id:
                    self_id = next(iter(self._bot_self_id.values()), None)
                if not self_id:
                    return None
                ok, data = await self._call_api(api_name="adapter.napcat.group.get_group_member_info", group_id=group_id, user_id=self_id, no_cache=True)
                if ok and isinstance(data, dict):
                    role = data.get("role", "member")
                    self._group_roles[group_id] = role
                    self._role_refresh_time[group_id] = time.time()
                    self.ctx.logger.info(f"[群管理] 角色检测结果: group={group_id} role={role}")
                    return role
                else:
                    self._group_roles[group_id] = "member"
                    self._role_refresh_time[group_id] = time.time()
                    return "member"
            except Exception as e:
                self._group_roles[group_id] = "member"
                self._role_refresh_time[group_id] = time.time()
                return "member"
        else:
            gid_str = str(group_id)
            override = self.config.identity.override_roles
            if gid_str in override:
                self._group_roles[group_id] = override[gid_str]
                return override[gid_str]
            return None

    def _get_aa_limits(self, group_id: int) -> tuple[int, int]:
        aa = self.config.auto_approve
        for g in aa.groups:
            if str(g.group_id) == str(group_id):
                appr = g.daily_approve_limit if g.daily_approve_limit > 0 else aa.daily_approve_limit
                rej = g.daily_reject_limit if g.daily_reject_limit > 0 else aa.daily_reject_limit
                return appr, rej
        return aa.daily_approve_limit, aa.daily_reject_limit

    def _get_aa_keywords(self, group_id: int) -> tuple[list[str], list[str]]:
        aa = self.config.auto_approve
        for g in aa.groups:
            if str(g.group_id) == str(group_id):
                req = [k.strip() for k in g.require_keywords.split(",") if k.strip()] if g.require_keywords else []
                rej = [k.strip() for k in g.reject_keywords.split(",") if k.strip()] if g.reject_keywords else []
                return req, rej
        return aa.require_message_keywords, aa.reject_keywords

    def _get_aa_enabled_action(self, group_id: int) -> tuple[bool, str]:
        aa = self.config.auto_approve
        for g in aa.groups:
            if str(g.group_id) == str(group_id):
                return True, g.default_action
        return aa.enabled, aa.default_action

    def _add_log(self, group_id: int, action: str, target_user_id: int, reason: str, success: bool):
        self._op_log.append({"timestamp": datetime.now().isoformat(), "group_id": group_id, "action": action, "target_user_id": target_user_id, "reason": reason, "success": success})

    def _count_ops_in_window(self, user_id: int, window_hours: float) -> int:
        cutoff = datetime.now() - timedelta(hours=window_hours)
        return sum(1 for e in self._op_log if e["target_user_id"] == user_id and e["action"] in ("warn", "mute", "kick") and datetime.fromisoformat(e["timestamp"]) > cutoff)

    def _check_escalation(self, user_id: int) -> Optional[EscalationStepConfig]:
        if not self.config.escalation.enabled: return None
        steps = self.config.escalation.escalation_steps
        if not steps: return None
        for step in steps:
            if self._count_ops_in_window(user_id, float(step.within_hours)) >= int(step.count):
                return step
        return None

    def _check_warning_threshold(self, user_id: int, violation_type: str) -> tuple[bool, int, int]:
        wc = self.config.warning
        if not wc.enabled: return False, 0, 999
        thresholds = {"spam": (wc.spam_warn_threshold, wc.spam_warn_window), "abuse": (wc.abuse_warn_threshold, wc.abuse_warn_window), "ad": (wc.ad_warn_threshold, wc.ad_warn_window)}
        threshold, window = thresholds.get(violation_type, (3, 600))
        if threshold <= 0: return True, 0, 0
        user_w = self._warnings.get(user_id, {}).get(violation_type, [])
        now = time.time()
        user_w = [(ts, c) for ts, c in user_w if now - ts <= window]
        count = sum(c for _, c in user_w)
        return count >= threshold, count, threshold

    def _resolve_group_id(self, stream_id: str, kwargs: dict = None) -> int:
        gid = self._stream_to_group.get(stream_id, 0)
        if gid:
            return gid
        if not kwargs:
            return 0
        for key in ("group_id", "group", "gid", "chat_id"):
            val = kwargs.get(key)
            if val:
                gid = self._to_int(val)
                if gid:
                    self._stream_to_group[stream_id] = gid
                    return gid
        msg = kwargs.get("message", {}) or {}
        if isinstance(msg, dict):
            for key in ("group_id", "group", "chat_id"):
                val = msg.get(key)
                if val:
                    gid = self._to_int(val)
                    if gid:
                        self._stream_to_group[stream_id] = gid
                        return gid
            mi = msg.get("message_info", {}) or {}
            if isinstance(mi, dict):
                gi = mi.get("group_info", {}) or {}
                gid = self._to_int(gi.get("group_id", 0))
                if gid:
                    self._stream_to_group[stream_id] = gid
                    return gid
                gid = self._to_int(mi.get("group_id", 0))
                if gid:
                    self._stream_to_group[stream_id] = gid
                    return gid
        return 0

    async def _resolve_target(self, gid: int, target: str, stream_id: str) -> int:
        target = target.lstrip("@")
        if target.isdigit(): return int(target)
        try:
            ok, data = await self._call_api(api_name="adapter.napcat.group.get_group_member_list", group_id=gid)
            if ok and isinstance(data, list):
                for m in data:
                    if not isinstance(m, dict): continue
                    nick = str(m.get("nickname", ""))
                    card = str(m.get("card", ""))
                    uid = str(m.get("user_id", ""))
                    if target.lower() in (nick.lower(), card.lower()):
                        return self._to_int(uid)
        except Exception: pass
        await self.ctx.send.text(f"未找到成员: {target} (请使用QQ号)", stream_id)
        return 0

    async def _send_at_text(self, stream_id: str, prefix: str, qq: int, suffix: str = ""):
        """发送带 @mention 的消息。"""
        segments = []
        if prefix:
            segments.append({"type": "text", "content": prefix + " "})
        segments.append({"type": "at", "data": {"target_user_id": str(qq)}})
        if suffix:
            segments.append({"type": "text", "content": suffix})
        await self.ctx.send.hybrid(segments, stream_id)

    def _extract_sender_id(self, kwargs: dict[str, Any]) -> int:
        for key in ("user_id", "sender_id", "user"):
            val = kwargs.get(key)
            if val: return self._to_int(val)
        return 0

    async def _check_admin_permission(self, stream_id: str, group_id: int, kwargs: dict[str, Any]) -> bool:
        sender_id = self._extract_sender_id(kwargs)
        admins = self.config.admin.admins
        deny_mode = self.config.admin.deny_response
        if str(sender_id) in admins: return True
        is_owner = False
        if group_id:
            role = await self._check_target_role(group_id, sender_id)
            if role == "owner":
                is_owner = True
        elif not admins:
            role = await self._check_target_role(group_id, sender_id) if group_id else None
            if role == "owner":
                is_owner = True
        if is_owner:
            if not self.config.admin.allow_group_owner:
                pass
            else:
                allowed = self.config.admin.owner_allowed_commands
                if not allowed:
                    return True
                text = str(kwargs.get("text", ""))
                for cmd in allowed:
                    if re.search(r'\b' + re.escape(cmd) + r'\b', text):
                        return True
                if deny_mode == "reply":
                    await self.ctx.send.text(self.config.prompts.command_denied_message, stream_id)
                return False
        if deny_mode == "reply":
            await self.ctx.send.text(self.config.prompts.command_denied_message, stream_id)
        return False

    # =========================================================================
    # Tool: group_warn_user
    # =========================================================================

    @Tool("group_warn_user", description="对指定群成员发出正式警告并记录, violation_type 为 spam(刷屏)/abuse(辱骂)/ad(广告)", parameters=[
        ToolParameterInfo(name="group_id", param_type=ToolParamType.INTEGER, description="群号", required=True),
        ToolParameterInfo(name="user_id", param_type=ToolParamType.INTEGER, description="用户QQ号", required=True),
        ToolParameterInfo(name="violation_type", param_type=ToolParamType.STRING, description="违规类型: spam/abuse/ad", required=True),
        ToolParameterInfo(name="reason", param_type=ToolParamType.STRING, description="警告原因(简要说明违规内容)", required=True),
    ])
    async def tool_warn_user(self, group_id: int = 0, user_id: int = 0, violation_type: str = "", reason: str = "", **kwargs: Any) -> dict[str, Any]:
        del kwargs
        self.ctx.logger.info(f"[群管理] Tool-warn: group={group_id} user={user_id} type={violation_type}")
        async with self._lock:
            await self._check_daily_reset(group_id)
            is_protected, msg = await self._is_protected(group_id, user_id)
            if is_protected: return {"name": "group_warn_user", "content": f"无法警告: {msg}"}
            self._warnings.setdefault(user_id, {}).setdefault(violation_type, []).append((time.time(), 1))
            type_cn = {"spam": "刷屏", "abuse": "辱骂", "ad": "广告"}.get(violation_type, violation_type)
            warn_text = f"⚠ 提醒: {reason}"
            await self.ctx.send.text(warn_text, str(group_id))
            over, current, thresh = self._check_warning_threshold(user_id, violation_type)
            self._add_log(group_id, "warn", user_id, reason, True)
            extra = f"\n该用户 {type_cn} 类提醒已达 {current}/{thresh}，请注意是否需要升级处理。" if over else ""
            return {"name": "group_warn_user", "content": f"已向 {user_id} 发出正式提醒（{type_cn}），原因：{reason}{extra}"}

    # =========================================================================
    # Tool: group_mute_user
    # =========================================================================

    @Tool("group_mute_user", description="禁言指定群成员, duration 为秒(10分钟=600秒, 1小时=3600秒)", parameters=[
        ToolParameterInfo(name="group_id", param_type=ToolParamType.INTEGER, description="群号", required=True),
        ToolParameterInfo(name="user_id", param_type=ToolParamType.INTEGER, description="用户QQ号", required=True),
        ToolParameterInfo(name="duration", param_type=ToolParamType.INTEGER, description="禁言秒数(例: 600=10分钟, 1800=30分钟, 3600=1小时)", required=True),
        ToolParameterInfo(name="reason", param_type=ToolParamType.STRING, description="禁言原因", required=True),
    ])
    async def tool_mute_user(self, group_id: int = 0, user_id: int = 0, duration: int = 0, reason: str = "", **kwargs: Any) -> dict[str, Any]:
        del kwargs
        self.ctx.logger.info(f"[群管理] Tool-mute: group={group_id} user={user_id} dur={duration}s")
        async with self._lock:
            await self._check_daily_reset(group_id)
            is_protected, msg = await self._is_protected(group_id, user_id)
            if is_protected: return {"name": "group_mute_user", "content": f"无法禁言: {msg}"}
            sf = self.config.safeguard
            if duration > sf.max_mute_duration: return {"name": "group_mute_user", "content": f"禁言时长过长（最大 {sf.max_mute_duration}秒，约 {sf.max_mute_duration//3600}小时），请缩短时长"}
            today = self._today_key()
            self._daily_mute_count.setdefault(group_id, {}).setdefault(today, 0)
            if self._daily_mute_count[group_id][today] >= sf.daily_mute_limit: return {"name": "group_mute_user", "content": f"今天已经禁言了 {sf.daily_mute_limit} 个用户，已达每日上限"}
            mute_key = (group_id, user_id)
            last_mute = self._last_mute_time.get(mute_key, 0)
            if sf.mute_cooldown > 0 and (time.time() - last_mute) < sf.mute_cooldown:
                remain = int(sf.mute_cooldown - (time.time() - last_mute))
                return {"name": "group_mute_user", "content": f"该用户 {remain} 秒前刚被禁言过，冷却中（至少间隔 {sf.mute_cooldown} 秒）"}
            esc = self._check_escalation(user_id)
            if esc and esc.action == "mute": duration = min(duration, esc.max_duration)
            if esc and esc.action == "kick": return {"name": "group_mute_user", "content": "该用户已达处罚阶梯要求，应踢出而非禁言，请使用 group_kick_user"}
            try:
                ok, data = await self._call_api(api_name="adapter.napcat.group.set_group_ban", group_id=self._to_int(group_id), user_id=self._to_int(user_id), duration=duration)
                if not ok: return {"name": "group_mute_user", "content": f"禁言未能生效: {data}"}
                self._daily_mute_count[group_id][today] += 1
                self._last_mute_time[mute_key] = time.time()
                self._add_log(group_id, "mute", user_id, reason, True)
                dur_min = duration // 60
                dur_str = f"{dur_min}分钟" if dur_min > 0 else f"{duration}秒"
                tip = "（已按阶梯规则调整时长）" if esc else ""
                return {"name": "group_mute_user", "content": f"已将 @{user_id} 禁言 {dur_str}，原因：{reason}{tip}"}
            except Exception as e:
                self._add_log(group_id, "mute", user_id, reason, False)
                return {"name": "group_mute_user", "content": f"禁言未能生效: {e}"}

    # =========================================================================
    # Tool: group_unmute_user
    # =========================================================================

    @Tool("group_unmute_user", description="解除指定群成员的禁言", parameters=[
        ToolParameterInfo(name="group_id", param_type=ToolParamType.INTEGER, description="群号", required=True),
        ToolParameterInfo(name="user_id", param_type=ToolParamType.INTEGER, description="用户QQ号", required=True),
    ])
    async def tool_unmute_user(self, group_id: int = 0, user_id: int = 0, **kwargs: Any) -> dict[str, Any]:
        del kwargs
        self.ctx.logger.info(f"[群管理] Tool-unmute: group={group_id} user={user_id}")
        async with self._lock:
            try:
                ok, data = await self._call_api(api_name="adapter.napcat.group.set_group_ban", group_id=self._to_int(group_id), user_id=self._to_int(user_id), duration=0)
                if not ok: return {"name": "group_unmute_user", "content": f"解除禁言未能生效: {data}"}
                return {"name": "group_unmute_user", "content": f"已解除 @{user_id} 的禁言"}
            except Exception as e:
                return {"name": "group_unmute_user", "content": f"解除禁言未能生效: {e}"}

    # =========================================================================
    # Tool: group_kick_user
    # =========================================================================

    @Tool("group_kick_user", description="踢出指定群成员(仅群主可直接踢人, 管理员踢人前需征求群主同意)。必须先调用 group_get_member 确认目标身份", parameters=[
        ToolParameterInfo(name="group_id", param_type=ToolParamType.INTEGER, description="群号", required=True),
        ToolParameterInfo(name="user_id", param_type=ToolParamType.INTEGER, description="用户QQ号", required=True),
        ToolParameterInfo(name="reason", param_type=ToolParamType.STRING, description="踢出原因", required=True),
    ])
    async def tool_kick_user(self, group_id: int = 0, user_id: int = 0, reason: str = "", **kwargs: Any) -> dict[str, Any]:
        del kwargs
        self.ctx.logger.info(f"[群管理] Tool-kick: group={group_id} user={user_id}")
        async with self._lock:
            await self._check_daily_reset(group_id)
            bot_role = self._get_group_role(group_id)
            if bot_role == "admin":
                return {"name": "group_kick_user", "content": "你是管理员而非群主，踢人前请先在群里征求群主或管理员的同意"}
            if bot_role not in ("owner",):
                return {"name": "group_kick_user", "content": "权限不足，仅群主可以踢人"}
            is_protected, msg = await self._is_protected(group_id, user_id)
            if is_protected: return {"name": "group_kick_user", "content": f"无法踢出: {msg}"}
            esc = self._check_escalation(user_id)
            if esc and esc.action == "mute":
                return {"name": "group_kick_user", "content": f"处罚阶梯建议先禁言 {esc.max_duration} 秒而非直接踢出，请使用 group_mute_user"}
            sf = self.config.safeguard
            if sf.kick_require_confirm and user_id not in self._get_member_called.get(group_id, set()): return {"name": "group_kick_user", "content": "踢人前请先调用 group_get_member 确认目标身份"}
            today = self._today_key()
            self._daily_kick_count.setdefault(group_id, {}).setdefault(today, 0)
            if self._daily_kick_count[group_id][today] >= sf.daily_kick_limit: return {"name": "group_kick_user", "content": f"今天已经踢出了 {sf.daily_kick_limit} 个用户，已达每日上限"}
            try:
                ok, data = await self._call_api(api_name="adapter.napcat.group.set_group_kick", group_id=self._to_int(group_id), user_id=self._to_int(user_id), reject_add_request=False)
                if not ok: self._add_log(group_id, "kick", user_id, reason, False); return {"name": "group_kick_user", "content": f"踢出未能生效: {data}"}
                self._daily_kick_count[group_id][today] += 1
                self._add_log(group_id, "kick", user_id, reason, True)
                self._get_member_called[group_id].discard(user_id)
                return {"name": "group_kick_user", "content": f"已将 @{user_id} 踢出群聊，原因：{reason}"}
            except Exception as e:
                self._add_log(group_id, "kick", user_id, reason, False)
                return {"name": "group_kick_user", "content": f"踢出未能生效: {e}"}

    # =========================================================================
    # Tool: group_set_user_card
    # =========================================================================

    @Tool("group_set_user_card", description="修改指定群成员的群名片", parameters=[
        ToolParameterInfo(name="group_id", param_type=ToolParamType.INTEGER, description="群号", required=True),
        ToolParameterInfo(name="user_id", param_type=ToolParamType.INTEGER, description="用户QQ号", required=True),
        ToolParameterInfo(name="card", param_type=ToolParamType.STRING, description="新群名片", required=True),
    ])
    async def tool_set_user_card(self, group_id: int = 0, user_id: int = 0, card: str = "", **kwargs: Any) -> dict[str, Any]:
        del kwargs
        self.ctx.logger.info(f"[群管理] Tool-card: group={group_id} user={user_id}")
        async with self._lock:
            is_protected, msg = await self._is_protected(group_id, user_id)
            if is_protected: return {"name": "group_set_user_card", "content": f"无法修改群名片: {msg}"}
            try:
                ok, data = await self._call_action_api(api_name="adapter.napcat.group.set_group_card", group_id=self._to_int(group_id), user_id=self._to_int(user_id), card=card)
                if not ok: return {"name": "group_set_user_card", "content": f"修改群名片未能生效: {data}"}
                return {"name": "group_set_user_card", "content": f"已将 @{user_id} 的群名片改为「{card}」"}
            except Exception as e:
                return {"name": "group_set_user_card", "content": f"修改群名片未能生效: {e}"}

    # =========================================================================
    # Tool: group_set_title
    # =========================================================================

    @Tool("group_set_title", description="设置指定群成员的专属头衔 (仅群主)", parameters=[
        ToolParameterInfo(name="group_id", param_type=ToolParamType.INTEGER, description="群号", required=True),
        ToolParameterInfo(name="user_id", param_type=ToolParamType.INTEGER, description="用户QQ号", required=True),
        ToolParameterInfo(name="title", param_type=ToolParamType.STRING, description="专属头衔(最长6字符)", required=True),
    ])
    async def tool_set_title(self, group_id: int = 0, user_id: int = 0, title: str = "", **kwargs: Any) -> dict[str, Any]:
        del kwargs
        self.ctx.logger.info(f"[群管理] Tool-title: group={group_id} user={user_id}")
        async with self._lock:
            is_protected, msg = await self._is_protected(group_id, user_id)
            if is_protected: return {"name": "group_set_title", "content": f"无法设置头衔: {msg}"}
            try:
                ok, data = await self._call_action_api(api_name="adapter.napcat.group.set_group_special_title", group_id=self._to_int(group_id), user_id=self._to_int(user_id), special_title=title)
                if not ok: return {"name": "group_set_title", "content": f"设置头衔未能生效: {data}"}
                return {"name": "group_set_title", "content": f"已将 @{user_id} 的专属头衔设为「{title}」"}
            except Exception as e:
                return {"name": "group_set_title", "content": f"设置头衔未能生效: {e}"}

    # =========================================================================
    # Tool: group_set_name
    # =========================================================================

    @Tool("group_set_name", description="修改群名称 (仅群主)", parameters=[
        ToolParameterInfo(name="group_id", param_type=ToolParamType.INTEGER, description="群号", required=True),
        ToolParameterInfo(name="name", param_type=ToolParamType.STRING, description="新群名称", required=True),
    ])
    async def tool_set_name(self, group_id: int = 0, name: str = "", **kwargs: Any) -> dict[str, Any]:
        del kwargs
        self.ctx.logger.info(f"[群管理] Tool-setname: group={group_id} name={name}")
        async with self._lock:
            try:
                ok, data = await self._call_api(api_name="adapter.napcat.group.set_group_name", group_id=self._to_int(group_id), group_name=name)
                if not ok: return {"name": "group_set_name", "content": f"修改群名未能生效: {data}"}
                return {"name": "group_set_name", "content": f"已将群名改为「{name}」"}
            except Exception as e:
                return {"name": "group_set_name", "content": f"修改群名失败: {e}"}

    # =========================================================================
    # Tool: group_approve_join
    # =========================================================================

    @Tool("group_approve_join", description="通过入群申请 (request_id 从 group_get_system_msg 获取)", parameters=[
        ToolParameterInfo(name="group_id", param_type=ToolParamType.INTEGER, description="群号", required=True),
        ToolParameterInfo(name="request_id", param_type=ToolParamType.STRING, description="申请flag/ID (来自 group_get_system_msg 的入群申请列表)", required=True),
        ToolParameterInfo(name="reason", param_type=ToolParamType.STRING, description="通过原因(可选)", required=False),
    ])
    async def tool_approve_join(self, group_id: int = 0, request_id: str = "", reason: str = "", **kwargs: Any) -> dict[str, Any]:
        del kwargs
        self.ctx.logger.info(f"[群管理] Tool-approve: group={group_id} req={request_id}")
        async with self._lock:
            await self._check_daily_reset(group_id)
            today = self._today_key()
            self._daily_approve_count.setdefault(group_id, {}).setdefault(today, 0)
            appr_lim, _ = self._get_aa_limits(group_id)
            if self._daily_approve_count[group_id][today] >= appr_lim: return {"name": "group_approve_join", "content": f"今日已通过 {appr_lim} 个申请，已达上限"}
            try:
                ok, data = await self._call_action_api(api_name="adapter.napcat.group.set_group_add_request", group_id=self._to_int(group_id), flag=request_id, approve=True, reason=reason)
                if not ok: return {"name": "group_approve_join", "content": f"通过申请未能生效: {data}"}
                self._daily_approve_count[group_id][today] += 1
                return {"name": "group_approve_join", "content": f"已通过入群申请 {request_id}"}
            except Exception as e:
                return {"name": "group_approve_join", "content": f"通过申请失败: {e}"}

    # =========================================================================
    # Tool: group_reject_join
    # =========================================================================

    @Tool("group_reject_join", description="拒绝入群申请 (request_id 从 group_get_system_msg 获取)", parameters=[
        ToolParameterInfo(name="group_id", param_type=ToolParamType.INTEGER, description="群号", required=True),
        ToolParameterInfo(name="request_id", param_type=ToolParamType.STRING, description="申请flag/ID (来自 group_get_system_msg 的入群申请列表)", required=True),
        ToolParameterInfo(name="reason", param_type=ToolParamType.STRING, description="拒绝原因", required=True),
    ])
    async def tool_reject_join(self, group_id: int = 0, request_id: str = "", reason: str = "", **kwargs: Any) -> dict[str, Any]:
        del kwargs
        self.ctx.logger.info(f"[群管理] Tool-reject: group={group_id} req={request_id}")
        async with self._lock:
            await self._check_daily_reset(group_id)
            today = self._today_key()
            self._daily_reject_count.setdefault(group_id, {}).setdefault(today, 0)
            _, rej_lim = self._get_aa_limits(group_id)
            if self._daily_reject_count[group_id][today] >= rej_lim: return {"name": "group_reject_join", "content": f"今日已拒绝 {rej_lim} 个申请，已达上限"}
            try:
                ok, data = await self._call_action_api(api_name="adapter.napcat.group.set_group_add_request", group_id=self._to_int(group_id), flag=request_id, approve=False, reason=reason)
                if not ok: return {"name": "group_reject_join", "content": f"拒绝申请未能生效: {data}"}
                self._daily_reject_count[group_id][today] += 1
                return {"name": "group_reject_join", "content": f"已拒绝入群申请 {request_id}: {reason}"}
            except Exception as e:
                return {"name": "group_reject_join", "content": f"拒绝申请失败: {e}"}

    # =========================================================================
    # Tool: group_post_notice / group_delete_notice / group_set_essence / group_unset_essence / group_recall_msg / group_get_member / group_get_shut_list / group_get_system_msg
    # =========================================================================

    @Tool("group_post_notice", description="发布群公告 (仅群主). 返回 notice_id 供后续删除使用", parameters=[
        ToolParameterInfo(name="group_id", param_type=ToolParamType.INTEGER, description="群号", required=True),
        ToolParameterInfo(name="content", param_type=ToolParamType.STRING, description="公告内容", required=True),
    ])
    async def tool_post_notice(self, group_id: int = 0, content: str = "", **kwargs: Any) -> dict[str, Any]:
        del kwargs
        self.ctx.logger.info(f"[群管理] Tool-notice-post: group={group_id}")
        async with self._lock:
            try:
                ok, data = await self._call_action_api(api_name="adapter.napcat.group.send_group_notice", group_id=self._to_int(group_id), content=content)
                if not ok: return {"name": "group_post_notice", "content": f"发布公告未能生效: {data}"}
                notice_id = ""
                if isinstance(data, dict):
                    notice_id = str(data.get("notice_id", data.get("noticeId", data.get("id", ""))))
                result = "群公告已发布"
                if notice_id:
                    result += f"，ID: {notice_id}"
                else:
                    result += f"，返回数据: {data}"
                return {"name": "group_post_notice", "content": result}
            except Exception as e:
                return {"name": "group_post_notice", "content": f"发布公告失败: {e}"}

    @Tool("group_delete_notice", description="删除群公告 (仅群主). 先用 group_get_notice 获取公告列表和 notice_id", parameters=[
        ToolParameterInfo(name="group_id", param_type=ToolParamType.INTEGER, description="群号", required=True),
        ToolParameterInfo(name="notice_id", param_type=ToolParamType.STRING, description="公告ID (来自 group_get_notice 返回值)", required=True),
    ])
    async def tool_delete_notice(self, group_id: int = 0, notice_id: str = "", **kwargs: Any) -> dict[str, Any]:
        del kwargs
        self.ctx.logger.info(f"[群管理] Tool-notice-del: group={group_id}")
        async with self._lock:
            try:
                ok, data = await self._call_action_api(api_name="adapter.napcat.group.delete_group_notice", group_id=self._to_int(group_id), notice_id=notice_id)
                if not ok: return {"name": "group_delete_notice", "content": f"删除公告未能生效: {data}"}
                return {"name": "group_delete_notice", "content": f"已删除公告 {notice_id}"}
            except Exception as e:
                return {"name": "group_delete_notice", "content": f"删除公告未能生效: {e}"}

    @Tool("group_set_essence", description="将消息设为群精华。操作流程: 先在群里请用户回复(引用)目标消息, 用户回复后从回复中提取 message_id 调用本工具", parameters=[
        ToolParameterInfo(name="group_id", param_type=ToolParamType.INTEGER, description="群号", required=True),
        ToolParameterInfo(name="message_id", param_type=ToolParamType.STRING, description="消息ID (用户回复目标消息后从回复数据中提取)", required=True),
    ])
    async def tool_set_essence(self, group_id: int = 0, message_id: str = "", **kwargs: Any) -> dict[str, Any]:
        del kwargs
        self.ctx.logger.info(f"[群管理] Tool-essence-set: group={group_id}")
        async with self._lock:
            try:
                ok, data = await self._call_action_api(api_name="adapter.napcat.group.set_essence_msg", group_id=self._to_int(group_id), message_id=message_id)
                if not ok: return {"name": "group_set_essence", "content": f"设为精华未能生效: {data}"}
                return {"name": "group_set_essence", "content": f"已将消息 {message_id} 设为精华"}
            except Exception as e:
                return {"name": "group_set_essence", "content": f"设为精华未能生效: {e}"}

    @Tool("group_unset_essence", description="取消消息的精华状态。操作流程同 group_set_essence", parameters=[
        ToolParameterInfo(name="group_id", param_type=ToolParamType.INTEGER, description="群号", required=True),
        ToolParameterInfo(name="message_id", param_type=ToolParamType.STRING, description="消息ID (用户回复后提取)", required=True),
    ])
    async def tool_unset_essence(self, group_id: int = 0, message_id: str = "", **kwargs: Any) -> dict[str, Any]:
        del kwargs
        self.ctx.logger.info(f"[群管理] Tool-essence-del: group={group_id}")
        async with self._lock:
            try:
                ok, data = await self._call_action_api(api_name="adapter.napcat.group.delete_essence_msg", group_id=self._to_int(group_id), message_id=message_id)
                if not ok: return {"name": "group_unset_essence", "content": f"取消精华未能生效: {data}"}
                return {"name": "group_unset_essence", "content": f"已取消消息 {message_id} 的精华"}
            except Exception as e:
                return {"name": "group_unset_essence", "content": f"取消精华未能生效: {e}"}

    @Tool("group_recall_msg", description="撤回指定消息。操作流程: 先在群里请用户回复(引用)目标消息, 用户回复后从回复中提取 message_id 调用本工具。群主/管理员无2分钟限制", parameters=[
        ToolParameterInfo(name="group_id", param_type=ToolParamType.INTEGER, description="群号", required=True),
        ToolParameterInfo(name="message_id", param_type=ToolParamType.STRING, description="消息ID (用户回复目标消息后从回复数据中提取)", required=True),
        ToolParameterInfo(name="reason", param_type=ToolParamType.STRING, description="撤回原因", required=True),
    ])
    async def tool_recall_msg(self, group_id: int = 0, message_id: str = "", reason: str = "", **kwargs: Any) -> dict[str, Any]:
        del kwargs
        self.ctx.logger.info(f"[群管理] Tool-recall: group={group_id} mid={message_id}")
        async with self._lock:
            try:
                ok, data = await self._call_api(api_name="adapter.napcat.message.delete_msg", message_id=self._to_int(message_id))
                if not ok: return {"name": "group_recall_msg", "content": f"撤回未能生效: {data}"}
                return {"name": "group_recall_msg", "content": f"已撤回消息 {message_id}: {reason}"}
            except Exception as e:
                return {"name": "group_recall_msg", "content": f"撤回未能生效: {e}"}

    @Tool("group_get_member", description="查询群成员的身份(owner/admin/member)、昵称和群名片。踢人/禁言前必须先调用此工具确认目标不是群主/管理员", parameters=[
        ToolParameterInfo(name="group_id", param_type=ToolParamType.INTEGER, description="群号", required=True),
        ToolParameterInfo(name="user_id", param_type=ToolParamType.INTEGER, description="用户QQ号", required=True),
    ])
    async def tool_get_member(self, group_id: int = 0, user_id: int = 0, **kwargs: Any) -> dict[str, Any]:
        del kwargs
        self.ctx.logger.info(f"[群管理] Tool-get-member: group={group_id} user={user_id}")
        async with self._lock:
            self._get_member_called.setdefault(group_id, set()).add(user_id)
            try:
                ok, data = await self._call_api(api_name="adapter.napcat.group.get_group_member_info", group_id=self._to_int(group_id), user_id=self._to_int(user_id), no_cache=True)
                if ok and isinstance(data, dict):
                    role = data.get("role", "unknown"); card = data.get("card", ""); nick = data.get("nickname", "")
                    self._known_roles[(self._to_int(group_id), self._to_int(user_id))] = role
                    return {"name": "group_get_member", "content": f"@{user_id}: 昵称={nick}, 群名片={card}, 身份={role}"}
                return {"name": "group_get_member", "content": f"未找到 @{user_id} 的信息"}
            except Exception as e:
                return {"name": "group_get_member", "content": f"查询成员信息未能生效: {e}"}

    @Tool("group_get_shut_list", description="查看当前群的禁言列表", parameters=[
        ToolParameterInfo(name="group_id", param_type=ToolParamType.INTEGER, description="群号", required=True),
    ])
    async def tool_get_shut_list(self, group_id: int = 0, **kwargs: Any) -> dict[str, Any]:
        del kwargs
        self.ctx.logger.info(f"[群管理] Tool-get-shutlist: group={group_id}")
        async with self._lock:
            try:
                ok, data = await self._call_action_api(api_name="adapter.napcat.group.get_group_shut_list", group_id=self._to_int(group_id))
                if ok and isinstance(data, dict): return {"name": "group_get_shut_list", "content": f"禁言列表: {data.get('data', data)}"}
                return {"name": "group_get_shut_list", "content": "该群当前没有被禁言的用户"}
            except Exception as e:
                return {"name": "group_get_shut_list", "content": f"查询禁言列表未能生效: {e}"}

    @Tool("group_get_notice", description="获取群公告列表(含 notice_id 和 content), 删除公告前必须先调用此工具获取 notice_id", parameters=[
        ToolParameterInfo(name="group_id", param_type=ToolParamType.INTEGER, description="群号", required=True),
    ])
    async def tool_get_notice(self, group_id: int = 0, **kwargs: Any) -> dict[str, Any]:
        del kwargs
        self.ctx.logger.info(f"[群管理] Tool-get-notice: group={group_id}")
        async with self._lock:
            try:
                ok, data = await self._call_action_api(api_name="adapter.napcat.group.get_group_notice", group_id=self._to_int(group_id))
                if ok and isinstance(data, dict):
                    return {"name": "group_get_notice", "content": f"公告列表: {data.get('data', data)}"}
                return {"name": "group_get_notice", "content": f"获取公告列表未能生效: {data}"}
            except Exception as e:
                return {"name": "group_get_notice", "content": f"获取公告列表未能生效: {e}"}

    @Tool("group_get_system_msg", description="获取群的系统消息(含入群申请列表)", parameters=[
        ToolParameterInfo(name="group_id", param_type=ToolParamType.INTEGER, description="群号", required=True),
    ])
    async def tool_get_system_msg(self, group_id: int = 0, **kwargs: Any) -> dict[str, Any]:
        del kwargs
        self.ctx.logger.info(f"[群管理] Tool-get-sysmsg: group={group_id}")
        async with self._lock:
            try:
                ok, data = await self._call_action_api(api_name="adapter.napcat.group.get_group_system_msg", group_id=self._to_int(group_id))
                now = datetime.now()
                if ok and isinstance(data, dict):
                    items = data.get("data", data)
                    if not isinstance(items, list):
                        items = [items] if items else []
                    max_age = self.config.auto_approve.max_pending_seconds
                    all_join = []
                    all_invites = []
                    for item in items:
                        if not isinstance(item, dict):
                            continue
                        jl = item.get("join_requests", item.get("JoinRequest", []))
                        if not isinstance(jl, list):
                            jl = [jl] if jl else []
                        all_join.extend(jl)
                        il = item.get("invited_requests", item.get("InvitedRequest", []))
                        if not isinstance(il, list):
                            il = [il] if il else []
                        all_invites.extend(il)
                    result_parts = []
                    if all_join:
                        filtered = []
                        for req in all_join:
                            if isinstance(req, dict):
                                ts = req.get("time", req.get("timestamp", now.timestamp()))
                                try:
                                    req_time = datetime.fromtimestamp(float(ts))
                                    if (now - req_time).total_seconds() <= max_age:
                                        filtered.append(req)
                                except Exception:
                                    filtered.append(req)
                            else:
                                filtered.append(req)
                        if filtered:
                            result_parts.append(f"入群申请({len(filtered)}条): {filtered}")
                    if all_invites:
                        result_parts.append(f"邀请入群({len(all_invites)}条): {all_invites}")
                    if result_parts:
                        return {"name": "group_get_system_msg", "content": "\n".join(result_parts)}
                    return {"name": "group_get_system_msg", "content": "当前没有待处理的入群申请或邀请（公告请用 group_get_notice 获取）"}
                return {"name": "group_get_system_msg", "content": str(data)}
            except Exception as e:
                return {"name": "group_get_system_msg", "content": f"获取系统消息未能生效: {e}"}

    # =========================================================================
    # Command: /admin 系列 (8个)
    # =========================================================================

    @Command("admin_status", description="查看群管理运行状态", pattern=r"^/admin\s+status(?:\s+(?P<group_id>\d+))?")
    async def cmd_admin_status(self, stream_id: str = "", **kwargs: Any):
        self.ctx.logger.info(f"[群管理] Cmd-status: stream={stream_id}")
        matched = kwargs.get("matched_groups") or {}
        gid = int(matched.get("group_id", 0) or 0) or self._resolve_group_id(stream_id, kwargs)
        if not await self._check_admin_permission(stream_id, gid, kwargs): return True, "", True
        role = self._get_group_role(gid)
        if role is None and gid: role = await self._ensure_bot_role(gid)
        role = role or "未知"
        today = self._today_key()
        mute_cnt = self._daily_mute_count.get(gid, {}).get(today, 0)
        kick_cnt = self._daily_kick_count.get(gid, {}).get(today, 0)
        enabled = "运行中" if self._is_group_enabled(gid) else "已暂停"
        info = f"群 {gid} 管理状态:\n  bot角色: {role}\n  状态: {enabled}\n  今日禁言: {mute_cnt}/{self.config.safeguard.daily_mute_limit}\n  今日踢人: {kick_cnt}/{self.config.safeguard.daily_kick_limit}"
        await self.ctx.send.text(info, stream_id)
        return True, "", True

    @Command("admin_off", description="关闭指定群的自动管理", pattern=r"^/admin\s+off(?:\s+(?P<group_id>\d+))?")
    async def cmd_admin_off(self, stream_id: str = "", **kwargs: Any):
        self.ctx.logger.info(f"[群管理] Cmd-off: stream={stream_id}")
        matched = kwargs.get("matched_groups") or {}
        gid = int(matched.get("group_id", 0) or 0) or self._resolve_group_id(stream_id, kwargs)
        if not await self._check_admin_permission(stream_id, gid, kwargs): return True, "", True
        self._disabled_groups.add(gid)
        await self.ctx.send.text(f"已关闭群 {gid} 的自动管理", stream_id)
        return True, "", True

    @Command("admin_on", description="重新开启指定群的自动管理", pattern=r"^/admin\s+on(?:\s+(?P<group_id>\d+))?")
    async def cmd_admin_on(self, stream_id: str = "", **kwargs: Any):
        self.ctx.logger.info(f"[群管理] Cmd-on: stream={stream_id}")
        matched = kwargs.get("matched_groups") or {}
        gid = int(matched.get("group_id", 0) or 0) or self._resolve_group_id(stream_id, kwargs)
        enabled_groups = [int(x) for x in self.config.auto_moderate.enabled_groups]
        if not await self._check_admin_permission(stream_id, gid, kwargs): return True, "", True
        if enabled_groups and gid not in enabled_groups:
            await self.ctx.send.text(f"群 {gid} 不在 enabled_groups 白名单中", stream_id)
            return True, "", True
        self._disabled_groups.discard(gid)
        await self.ctx.send.text(f"已恢复群 {gid} 的自动管理", stream_id)
        return True, "", True

    @Command("admin_undo", description="强制解禁", pattern=r"^/admin\s+undo(?:\s+(?P<group_id>\d+))?\s+@?(?P<qq>\d+)")
    async def cmd_admin_undo(self, stream_id: str = "", **kwargs: Any):
        self.ctx.logger.info(f"[群管理] Cmd-undo: stream={stream_id}")
        matched = kwargs.get("matched_groups") or {}
        raw_text = str(kwargs.get("text", ""))
        if not isinstance(matched, dict) or not matched.get("qq"):
            m = re.search(r"@?(\d+)", raw_text.split("undo", 1)[-1] if "undo" in raw_text else raw_text)
            if m: matched = {"qq": m.group(1)}
        gid = int(matched.get("group_id", 0) or 0) or self._resolve_group_id(stream_id, kwargs)
        qq = int(matched.get("qq", 0))
        if not qq: await self.ctx.send.text("用法: /admin undo [群号] @qq", stream_id); return True, "", True
        if not await self._check_admin_permission(stream_id, gid, kwargs): return True, "", True
        await self._call_api(api_name="adapter.napcat.group.set_group_ban", group_id=gid, user_id=qq, duration=0)
        exempt = self.config.safeguard.exempt_users
        gid_str = str(gid)
        if gid_str in exempt and str(qq) in exempt[gid_str]:
            exempt[gid_str] = [u for u in exempt[gid_str] if u != str(qq)]
            if not exempt[gid_str]: del exempt[gid_str]
        await self._send_at_text(stream_id, "已强制解禁", qq, "，同时移出豁免名单")
        return True, "", True

    @Command("admin_log", description="查看操作记录 /admin log [群号|行数] [行数]", pattern=r"^/admin\s+log(?:\s+(?P<arg1>\d+))?(?:\s+(?P<arg2>\d+))?")
    async def cmd_admin_log(self, stream_id: str = "", **kwargs: Any):
        self.ctx.logger.info(f"[群管理] Cmd-log: stream={stream_id}")
        matched = kwargs.get("matched_groups") or {}
        arg1 = int(matched.get("arg1", 0) or 0)
        arg2 = int(matched.get("arg2", 0) or 0)
        gid = 0
        n = self.config.logging.default_log_lines
        for val in (arg1, arg2):
            if val <= 0:
                continue
            if val >= 10000:
                gid = val
            else:
                n = val
        if not gid:
            gid = self._resolve_group_id(stream_id, kwargs)
        if not await self._check_admin_permission(stream_id, gid, kwargs): return True, "", True
        entries = list(self._op_log)
        if gid: entries = [e for e in entries if e["group_id"] == gid]
        entries = entries[-n:]
        if not entries: await self.ctx.send.text("暂无操作记录", stream_id); return True, "", True
        lines = [f"群 {gid or '全部'} 最近 {len(entries)} 条操作记录:"]
        for e in entries:
            status = "✓" if e["success"] else "✗"
            lines.append(f"  {e['timestamp'][:16]} | {e['action']:8s} | @{e['target_user_id']:12s} | {status} | {e['reason']}")
        await self.ctx.send.text("\n".join(lines), stream_id)
        return True, "", True

    @Command("admin_ban", description="添加豁免", pattern=r"^/admin\s+ban(?:\s+(?P<group_id>\d+))?\s+@?(?P<qq>\d+)")
    async def cmd_admin_ban(self, stream_id: str = "", **kwargs: Any):
        self.ctx.logger.info(f"[群管理] Cmd-ban: stream={stream_id}")
        matched = kwargs.get("matched_groups") or {}
        raw_text = str(kwargs.get("text", ""))
        if not isinstance(matched, dict) or not matched.get("qq"):
            m = re.search(r"@?(\d+)", raw_text.split("ban", 1)[-1] if "ban" in raw_text else raw_text)
            if m: matched = {"qq": m.group(1)}
        gid = int(matched.get("group_id", 0) or 0) or self._resolve_group_id(stream_id, kwargs)
        qq = int(matched.get("qq", 0))
        if not qq: await self.ctx.send.text("用法: /admin ban [群号] @qq", stream_id); return True, "", True
        if not await self._check_admin_permission(stream_id, gid, kwargs): return True, "", True
        exempt = self.config.safeguard.exempt_users
        gid_str = str(gid); exempt.setdefault(gid_str, [])
        if str(qq) not in exempt[gid_str]: exempt[gid_str].append(str(qq))
        await self.ctx.send.text(f"已将 {qq} 添加到群 {gid} 的豁免名单", stream_id)
        return True, "", True

    @Command("admin_unban", description="移除豁免", pattern=r"^/admin\s+unban(?:\s+(?P<group_id>\d+))?\s+@?(?P<qq>\d+)")
    async def cmd_admin_unban(self, stream_id: str = "", **kwargs: Any):
        self.ctx.logger.info(f"[群管理] Cmd-unban: stream={stream_id}")
        matched = kwargs.get("matched_groups") or {}
        raw_text = str(kwargs.get("text", ""))
        if not isinstance(matched, dict) or not matched.get("qq"):
            m = re.search(r"@?(\d+)", raw_text.split("unban", 1)[-1] if "unban" in raw_text else raw_text)
            if m: matched = {"qq": m.group(1)}
        gid = int(matched.get("group_id", 0) or 0) or self._resolve_group_id(stream_id, kwargs)
        qq = int(matched.get("qq", 0))
        if not qq: await self.ctx.send.text("用法: /admin unban [群号] @qq", stream_id); return True, "", True
        if not await self._check_admin_permission(stream_id, gid, kwargs): return True, "", True
        exempt = self.config.safeguard.exempt_users
        gid_str = str(gid)
        if gid_str in exempt and str(qq) in exempt[gid_str]:
            exempt[gid_str] = [u for u in exempt[gid_str] if u != str(qq)]
            if not exempt[gid_str]: del exempt[gid_str]
        await self.ctx.send.text(f"已将 {qq} 从群 {gid} 的豁免名单移除", stream_id)
        return True, "", True

    @Command("admin_reload", description="热重载配置", pattern=r"/admin\s+reload")
    async def cmd_admin_reload(self, stream_id: str = "", **kwargs: Any):
        self.ctx.logger.info(f"[群管理] Cmd-reload: stream={stream_id}")
        sender_id = self._extract_sender_id(kwargs)
        admins = self.config.admin.admins
        if str(sender_id) not in admins:
            if self.config.admin.deny_response == "reply": await self.ctx.send.text(self.config.prompts.command_denied_message, stream_id)
            return True, "", True
        try:
            self.set_plugin_config(self.config.model_dump())
            await self.ctx.send.text("插件配置已刷新", stream_id)
        except Exception as e:
            await self.ctx.send.text(f"刷新失败: {e}", stream_id)
        return True, "", True

    # =========================================================================
    # Command: 管理员快捷操作 (7个)
    # =========================================================================

    @Command("admin_mute", description="管理员禁言: /mute @qq|昵称 N分钟 原因", pattern=r"^/mute\s+@?(?P<target>\S+)\s+(?P<duration>\d+)\s*(?P<unit>分钟|小时|秒|min|h|s)?\s*(?P<reason>.*)?")
    async def cmd_admin_mute(self, stream_id: str = "", **kwargs: Any):
        self.ctx.logger.info(f"[群管理] Cmd-mute: stream={stream_id}")
        matched = kwargs.get("matched_groups") or {}
        gid = self._resolve_group_id(stream_id, kwargs)
        target = (matched.get("target") or "").strip(); duration = int(matched.get("duration", 0) or 0)
        unit = (matched.get("unit") or "分钟").strip(); reason = (matched.get("reason") or "").strip()
        if not target or duration <= 0: await self.ctx.send.text("用法: /mute @qq或昵称 N分钟 [原因]", stream_id); return True, "", True
        qq = await self._resolve_target(gid, target, stream_id)
        if not qq: return True, "", True
        if not await self._check_admin_permission(stream_id, gid, kwargs): return True, "", True
        if unit in ("小时", "h"): duration *= 3600
        elif unit in ("秒", "s"): pass
        else: duration *= 60
        is_protected, msg = await self._is_protected(gid, qq)
        if is_protected: await self.ctx.send.text(f"操作被拦截: {msg}", stream_id); return True, "", True
        sf = self.config.safeguard
        mute_key = (gid, qq)
        last_mute = self._last_mute_time.get(mute_key, 0)
        if sf.mute_cooldown > 0 and (time.time() - last_mute) < sf.mute_cooldown:
            remain = int(sf.mute_cooldown - (time.time() - last_mute))
            await self.ctx.send.text(f"该用户 {remain} 秒前刚被禁言，请稍后再试", stream_id)
            return True, "", True
        esc = self._check_escalation(qq)
        if esc and esc.action == "kick":
            await self.ctx.send.text(f"该用户 {esc.within_hours}h 内已被处罚 {esc.count} 次，建议使用 /kick 踢出", stream_id)
            return True, "", True
        if esc and esc.action == "mute":
            duration = min(duration, esc.max_duration)
        duration = min(duration, sf.max_mute_duration)
        await self._check_daily_reset(gid)
        today = self._today_key()
        self._daily_mute_count.setdefault(gid, {}).setdefault(today, 0)
        if self._daily_mute_count[gid][today] >= sf.daily_mute_limit:
            await self.ctx.send.text(f"今天已经禁言了 {sf.daily_mute_limit} 个用户，已达每日上限", stream_id)
            return True, "", True
        ok, data = await self._call_api(api_name="adapter.napcat.group.set_group_ban", group_id=gid, user_id=qq, duration=duration)
        if ok:
            self._last_mute_time[mute_key] = time.time()
            self._daily_mute_count[gid][today] += 1
            dur_min = duration // 60
            dur_str = f"{dur_min}分钟" if dur_min > 0 else f"{duration}秒"
            await self._send_at_text(stream_id, f"已将", qq, f"禁言 {dur_str}" + (f"（{reason}）" if reason else ""))
            self._add_log(gid, "mute", qq, reason or "管理员命令", True)
        else: await self.ctx.send.text(f"禁言未能生效: {data}", stream_id)
        return True, "", True

    @Command("admin_unmute", description="管理员解禁: /unmute @qq|昵称", pattern=r"^/unmute\s+@?(?P<target>\S+)")
    async def cmd_admin_unmute(self, stream_id: str = "", **kwargs: Any):
        self.ctx.logger.info(f"[群管理] Cmd-admin-unmute: stream={stream_id}")
        matched = kwargs.get("matched_groups") or {}
        gid = self._resolve_group_id(stream_id, kwargs)
        target = (matched.get("target") or "").strip()
        if not target: await self.ctx.send.text("用法: /unmute @qq或昵称", stream_id); return True, "", True
        qq = await self._resolve_target(gid, target, stream_id)
        if not qq: return True, "", True
        if not await self._check_admin_permission(stream_id, gid, kwargs): return True, "", True
        ok, data = await self._call_api(api_name="adapter.napcat.group.set_group_ban", group_id=gid, user_id=qq, duration=0)
        if ok: await self._send_at_text(stream_id, "已解除", qq, "的禁言")
        else: await self.ctx.send.text(f"解禁未能生效: {data}", stream_id)
        return True, "", True

    @Command("admin_kick", description="管理员踢人: /kick @qq|昵称 原因", pattern=r"^/kick\s+@?(?P<target>\S+)\s*(?P<reason>.*)?")
    async def cmd_admin_kick(self, stream_id: str = "", **kwargs: Any):
        self.ctx.logger.info(f"[群管理] Cmd-kick: stream={stream_id}")
        matched = kwargs.get("matched_groups") or {}
        gid = self._resolve_group_id(stream_id, kwargs)
        target = (matched.get("target") or "").strip(); reason = (matched.get("reason") or "").strip()
        if not target: await self.ctx.send.text("用法: /kick @qq或昵称 [原因]", stream_id); return True, "", True
        qq = await self._resolve_target(gid, target, stream_id)
        if not qq: return True, "", True
        if not await self._check_admin_permission(stream_id, gid, kwargs): return True, "", True
        is_protected, msg = await self._is_protected(gid, qq)
        if is_protected: await self.ctx.send.text(f"操作被拦截: {msg}", stream_id); return True, "", True
        esc = self._check_escalation(qq)
        if esc and esc.action == "mute":
            await self.ctx.send.text(f"处罚阶梯建议先禁言 {esc.max_duration} 秒而非直接踢出，请使用 /mute", stream_id)
            return True, "", True
        await self._check_daily_reset(gid)
        today = self._today_key()
        self._daily_kick_count.setdefault(gid, {}).setdefault(today, 0)
        if self._daily_kick_count[gid][today] >= self.config.safeguard.daily_kick_limit:
            await self.ctx.send.text(f"今天已经踢出了 {self.config.safeguard.daily_kick_limit} 个用户，已达每日上限", stream_id)
            return True, "", True
        ok, data = await self._call_api(api_name="adapter.napcat.group.set_group_kick", group_id=gid, user_id=qq, reject_add_request=False)
        if ok:
            await self._send_at_text(stream_id, "已踢出", qq, reason if reason else "")
            self._daily_kick_count[gid][today] += 1
            self._add_log(gid, "kick", qq, reason or "管理员命令", True)
        else: await self.ctx.send.text(f"踢出未能生效: {data}", stream_id)
        return True, "", True

    @Command("admin_warn", description="管理员警告: /warn @qq|昵称 spam/abuse/ad 原因", pattern=r"^/warn\s+@?(?P<target>\S+)\s+(?P<type>spam|abuse|ad)\s*(?P<reason>.*)?")
    async def cmd_admin_warn(self, stream_id: str = "", **kwargs: Any):
        self.ctx.logger.info(f"[群管理] Cmd-warn: stream={stream_id}")
        matched = kwargs.get("matched_groups") or {}
        gid = self._resolve_group_id(stream_id, kwargs)
        target = (matched.get("target") or "").strip(); vtype = (matched.get("type") or "").strip(); reason = (matched.get("reason") or "").strip()
        if not target or not vtype: await self.ctx.send.text("用法: /warn @qq或昵称 spam/abuse/ad [原因]", stream_id); return True, "", True
        qq = await self._resolve_target(gid, target, stream_id)
        if not qq: return True, "", True
        if not await self._check_admin_permission(stream_id, gid, kwargs): return True, "", True
        self._warnings.setdefault(qq, {}).setdefault(vtype, []).append((time.time(), 1))
        self._add_log(gid, "warn", qq, reason or "管理员命令", True)
        type_cn = {"spam": "刷屏", "abuse": "辱骂", "ad": "广告"}.get(vtype, vtype)
        await self._send_at_text(stream_id, f"已提醒", qq, f"[{type_cn}]" + (f"（{reason}）" if reason else ""))
        return True, "", True

    def _get_reply_msg_id(self, kwargs: dict) -> str:
        """从命令上下文中提取被回复消息的 ID。"""
        # 方式1: kwargs 直接携带
        for key in ("reply_message_id", "msg_id", "target_msg_id"):
            val = kwargs.get(key)
            if val: return str(val)
        # 方式2: 从 message 的 raw_message 中提取 reply 段
        msg = kwargs.get("message", {}) or {}
        if isinstance(msg, dict):
            raw = msg.get("raw_message", [])
            if isinstance(raw, list):
                for seg in raw:
                    if isinstance(seg, dict) and seg.get("type") == "reply":
                        data = seg.get("data", {}) or {}
                        mid = data.get("id", data.get("message_id", data.get("target_message_id", "")))
                        if mid: return str(mid)
        return ""

    @Command("admin_essence", description="设精华: 回复消息后 /essence", pattern=r"/essence")
    async def cmd_admin_essence(self, stream_id: str = "", **kwargs: Any):
        self.ctx.logger.info(f"[群管理] Cmd-essence: stream={stream_id}")
        gid = self._resolve_group_id(stream_id, kwargs)
        if not await self._check_admin_permission(stream_id, gid, kwargs): return True, "", True
        msg_id = self._get_reply_msg_id(kwargs)
        if not msg_id: await self.ctx.send.text("请先回复目标消息再使用 /essence", stream_id); return True, "", True
        ok, data = await self._call_action_api(api_name="adapter.napcat.group.set_essence_msg", group_id=gid, message_id=msg_id)
        await self.ctx.send.text("已设为精华消息" if ok else f"未能生效: {data}", stream_id)
        return True, "", True

    @Command("admin_recall", description="撤回: 回复消息后 /recall", pattern=r"/recall")
    async def cmd_admin_recall(self, stream_id: str = "", **kwargs: Any):
        self.ctx.logger.info(f"[群管理] Cmd-recall: stream={stream_id}")
        gid = self._resolve_group_id(stream_id, kwargs)
        if not await self._check_admin_permission(stream_id, gid, kwargs): return True, "", True
        msg_id = self._get_reply_msg_id(kwargs)
        if not msg_id: await self.ctx.send.text("请先回复目标消息再使用 /recall", stream_id); return True, "", True
        ok, data = await self._call_api(api_name="adapter.napcat.message.delete_msg", message_id=self._to_int(msg_id))
        await self.ctx.send.text("已撤回" if ok else f"未能生效: {data}", stream_id)
        return True, "", True

    @Command("admin_shutlist", description="查看禁言列表: /shutlist", pattern=r"/shutlist")
    async def cmd_admin_shutlist(self, stream_id: str = "", **kwargs: Any):
        self.ctx.logger.info(f"[群管理] Cmd-shutlist: stream={stream_id}")
        gid = self._resolve_group_id(stream_id, kwargs)
        if not await self._check_admin_permission(stream_id, gid, kwargs): return True, "", True
        ok, data = await self._call_action_api(api_name="adapter.napcat.group.get_group_shut_list", group_id=gid)
        if ok and isinstance(data, dict): await self.ctx.send.text(f"禁言列表: {data.get('data', data)}", stream_id)
        else: await self.ctx.send.text(f"查询未能生效: {data}", stream_id)
        return True, "", True

    # =========================================================================
    # EventHandler: auto_moderate_inline
    # =========================================================================

    @EventHandler("auto_moderate_inline", description="自动审核: 注入管理prompt到LLM上下文", event_type=EventType.ON_MESSAGE)
    async def handle_auto_moderate(self, message: Any = None, stream_id: str = "", **kwargs: Any):
        if not self.config.plugin.enabled: return {"continue_processing": True}
        if not self.config.auto_moderate.enabled: return {"continue_processing": True}
        group_id = 0
        if isinstance(message, dict):
            mi = message.get("message_info", {}) or {}
            gi = mi.get("group_info", {}) or {}
            ac = mi.get("additional_config", {}) or {}
            group_id = self._to_int(gi.get("group_id", 0))
            self_id = ac.get("self_id")
            if self_id and group_id: self._bot_self_id[group_id] = self._to_int(self_id)
            if group_id and stream_id: self._stream_to_group[stream_id] = group_id
        if not group_id or not self._is_group_enabled(group_id): return {"continue_processing": True}
        role = await self._ensure_bot_role(group_id) or "member"
        is_mentioned = bool(message.get("is_mentioned") or message.get("is_at")) if isinstance(message, dict) else False
        self._msg_counter[group_id] = self._msg_counter.get(group_id, 0) + 1
        last_inject = self._last_inject_time.get(group_id, 0)
        amc = self.config.auto_moderate
        needs_inject = (self._msg_counter[group_id] == 1 or is_mentioned or self._msg_counter[group_id] % amc.re_inject_interval_messages == 0 or (time.time() - last_inject) >= amc.re_inject_interval_seconds)
        if not needs_inject: return {"continue_processing": True}
        self._last_inject_time[group_id] = time.time()
        available = []
        if role == "owner": available.append("全部管理: 禁言/解禁/踢人/警告/设精华/撤回/公告/改名/审批入群")
        elif role == "admin": available.append("禁言/解禁/踢人/警告/设精华/撤回/改名片/审批入群")
        else: available.append("你在此群为普通成员，管理操作受限于QQ权限。可协助管理员做决策建议。")
        hard_prefix = (
            "【管理助手指令】\n"
            "你需要以群管理助手的身份协助维护群秩序。"
            "以下是当前群的上下文信息，请基于此做出管理判断:\n"
            f"  当前群号: {group_id}\n"
            f"  你的身份: {role}\n"
            "当发现违规行为时，请调用对应的管理工具处理。"
            "如有人询问群号等群信息，可从上下文直接获取回答。\n\n"
        )
        prompt = hard_prefix + self.config.prompts.auto_moderate_system
        prompt = prompt.replace("{bot_role}", role).replace("{bot_nickname}", self.config.identity.bot_nickname).replace("{group_name}", str(group_id)).replace("{available_actions}", "; ".join(available))
        try:
            await self.ctx.maisaka.context.append(stream_id=stream_id, segments=[{"type": "text", "content": prompt}], visible_text=prompt, source_kind="plugin:maimai.group-admin")
        except Exception as e:
            self.ctx.logger.error(f"注入管理 prompt 失败: {e}")
        modified_message = None
        if is_mentioned and isinstance(message, dict):
            modified_message = dict(message)
            raw = list(message.get("raw_message", []))
            action_list = "禁言/解禁/踢人/警告/设精华/撤回/审批" if role in ("owner", "admin") else "协助管理决策"
            instruction = (
                f"[群管理上下文] 群号={group_id}，你是本群{role}，可用{action_list}。"
                "如消息涉及管理需求，请使用管理工具协助处理。"
            )
            raw.insert(0, {"type": "text", "content": instruction})
            modified_message["raw_message"] = raw
            ppt = message.get("processed_plain_text", "")
            modified_message["processed_plain_text"] = f"{instruction}\n{ppt}"
            modified_message["display_message"] = modified_message["processed_plain_text"]
        if self._msg_counter[group_id] > 100000: self._msg_counter[group_id] = 0
        if time.time() - self._last_cleanup_time > 3600:
            self._cleanup_memory()
        result = {"continue_processing": True}
        if modified_message: result["modified_message"] = modified_message
        return result


def create_plugin() -> GroupAdminPlugin:
    return GroupAdminPlugin()
