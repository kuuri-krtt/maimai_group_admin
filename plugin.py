"""群管理助手 — LLM 自主管理 QQ 群插件。v1.5

18 个管理 Tool + 15 条快捷命令 + 4 个 HookHandler，支持禁言/解禁/踢人/警告/设精华/撤回/改名片/
改头衔/改群名/公告发布与删除/入群审批，含 8 步安全护栏 + 按群独立配置。

v1.5: 全面修复三大类缺陷 — 缓存生命周期(TTL + 独立清理任务)、跨群统计隔离(_warnings/_escalation/_bot_self_id)、
内存清理策略(_last_mute_time/_get_member_called/_known_roles 按时淘汰)、竞态条件加锁、异常处理统一、
/admin reload 清空运行时缓存、auto_approve.groups 按群覆盖修复，共 27 处异常风格统一。
"""

from __future__ import annotations

import asyncio
import base64
import json
import re
import time
import urllib.request
from collections import deque
from datetime import datetime, timedelta
from typing import Any, ClassVar, Optional

from maibot_sdk import Command, EventHandler, Field, HookHandler, MaiBotPlugin, PluginConfigBase, Tool
from maibot_sdk.types import ErrorPolicy, EventType, HookMode, HookOrder, ToolParameterInfo, ToolParamType


# =============================================================================
# 配置模型
# =============================================================================

class PluginSectionConfig(PluginConfigBase):
    __ui_label__ = "插件开关"; __ui_icon__ = "power"; __ui_order__ = 0
    enabled: bool = Field(default=False, description="是否启用插件")
    config_version: str = Field(default="1.5.0", description="配置版本")

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
    enabled_groups: list[str] = Field(default_factory=list, description="启用插件的群号白名单")
    audit_model: str = Field(default="planner", description="入站LLM审核使用的任务模型，如 planner/utils/replyer")
    audit_max_tokens: int = Field(default=220, description="入站LLM审核最大输出token数")
    treat_forwarded_records_as_single_message: bool = Field(
        default=True,
        description="将QQ合并转发聊天记录作为单条消息整体审核，不把内部多条记录视为发送者连续刷屏",
    )

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
    verbose_logging: bool = Field(default=False, description="详细日志: 输出完整注入prompt和守门详情，用于排查问题")

class PromptsSectionConfig(PluginConfigBase):
    __ui_label__ = "提示词"; __ui_icon__ = "message-square"; __ui_order__ = 9
    auto_moderate_system: str = Field(default=(
        "【群管理参考 — 保持人设，融入语气，不要切换管理员口吻】\n"
        "\n"
        "身份：{bot_role}  可用工具：{available_actions}\n"
        "\n"
        "处理参考（以你的人设语气自然执行）：\n"
        "  广告/诈骗 → 撤回 + 禁言10-30分钟\n"
        "  连续刷屏 → 提醒一句，继续刷再禁言5-10分钟\n"
        "  辱骂/人身攻击 → 撤回 + 禁言1-6小时，再犯踢出\n"
        "  色情/违法 → 撤回 + 踢出\n"
        "  高质量分享 → 设精华表达赞赏\n"
        "  不确定 → 先观察，别着急动手\n"
        "\n"
        "操作提示：警告/禁言填 group_id 和 user_id；踢人前先调 group_get_member；撤回/精华需 \n"
        "用户回复目标消息后获取 message_id\n"
        "\n"
        "节奏控制：正常聊天做自己，只在违规时动工具，不要说'已将xxx禁言'，用自然方式带过"
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
        self._known_roles: dict[tuple[int, int], tuple[str, float]] = {}
        self._bot_self_id: Optional[int] = None
        self._stream_to_group: dict[str, int] = {}
        self._disabled_groups: set[int] = set()
        self._daily_mute_count: dict[int, dict[str, int]] = {}
        self._daily_kick_count: dict[int, dict[str, int]] = {}
        self._daily_approve_count: dict[int, dict[str, int]] = {}
        self._daily_reject_count: dict[int, dict[str, int]] = {}
        self._warnings: dict[int, dict[int, dict[str, list[tuple[float, int]]]]] = {}
        self._recent_user_messages: dict[tuple[Any, ...], deque[tuple[float, str]]] = {}
        self._audit_tasks: dict[tuple[Any, ...], asyncio.Task] = {}
        self._audit_seen_messages: dict[str, float] = {}
        self._op_log: deque[dict[str, Any]] = deque(maxlen=5000)
        self._get_member_called: dict[int, dict[int, float]] = {}
        self._last_mute_time: dict[tuple[int, int], float] = {}
        self._lock: asyncio.Lock = asyncio.Lock()
        self._auto_check_task: Optional[asyncio.Task] = None
        self._cleanup_task: Optional[asyncio.Task] = None
        self._last_cleanup_time: float = 0

    # ===== 生命周期 =====

    async def on_load(self) -> None:
        if not self.config.plugin.enabled:
            return
        self._ensure_op_log_capacity()
        self._start_auto_check()
        self._start_cleanup()

    async def on_unload(self) -> None:
        self._stop_auto_check()
        self._stop_cleanup()
        for task in list(self._audit_tasks.values()):
            if task and not task.done():
                task.cancel()
        self._audit_tasks.clear()

    async def on_config_update(self, scope: str, config_data: dict[str, Any], version: str) -> None:
        if scope != "self":
            return
        self.set_plugin_config(config_data)
        self._ensure_op_log_capacity()
        self._stop_auto_check()
        self._start_auto_check()
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

    def _start_cleanup(self):
        if self._cleanup_task and not self._cleanup_task.done():
            return
        self._cleanup_task = asyncio.create_task(self._cleanup_loop())

    def _stop_cleanup(self):
        if self._cleanup_task and not self._cleanup_task.done():
            self._cleanup_task.cancel()
        self._cleanup_task = None

    async def _cleanup_loop(self):
        while True:
            try:
                await asyncio.sleep(600)
                self._cleanup_memory()
            except asyncio.CancelledError:
                break
            except Exception:
                self.ctx.logger.error("[群管理] 清理循环异常", exc_info=True)

    async def _auto_check_loop(self, interval: int):
        while True:
            try:
                await asyncio.sleep(interval)
                await self._check_join_requests()
                self._cleanup_memory()
            except asyncio.CancelledError:
                break
            except Exception:
                self.ctx.logger.error("[群管理] 自动检查异常", exc_info=True)

    async def _check_join_requests(self):
        aa = self.config.auto_approve
        am_enabled = self.config.auto_moderate.enabled_groups
        known_groups = {int(g) for g in am_enabled if g}
        aa_only_groups: set[int] = set()
        for g in aa.groups:
            gid = self._to_int(g.group_id)
            if gid > 0 and gid not in known_groups:
                aa_only_groups.add(gid)
                known_groups.add(gid)
        if not known_groups:
            return
        self.ctx.logger.info(f"[群管理] 自动检查入群申请: groups={known_groups}")
        now = datetime.now()
        max_age = aa.max_pending_seconds
        for gid in known_groups:
            if gid not in aa_only_groups and not self._is_group_enabled(gid):
                continue
            grp_enabled, grp_default = self._get_aa_enabled_action(gid)
            if not grp_enabled or grp_default == "ignore":
                continue
            ok, data = await self._call_action_api(api_name="adapter.napcat.group.get_group_system_msg", group_id=gid)
            if not ok or not isinstance(data, dict):
                continue
            items = data.get("data", data)
            if isinstance(items, dict):
                items = [items]
            elif not isinstance(items, list):
                continue
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
                        is_protected, protect_msg = await self._is_protected(req_gid, self._to_int(user_id))
                        if is_protected:
                            self.ctx.logger.info(f"[群管理] 入群申请跳过(受保护): gid={req_gid} user={user_id}: {protect_msg}")
                            continue
                        async with self._lock:
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
                        is_protected, protect_msg = await self._is_protected(req_gid, self._to_int(user_id))
                        if is_protected:
                            self.ctx.logger.info(f"[群管理] 入群申请跳过(受保护): gid={req_gid} user={user_id}: {protect_msg}")
                            continue
                        async with self._lock:
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
        """清理过期内存数据：warnings 过期条目、known_roles/stream_to_group 上限裁剪、last_mute_time/get_member_called 旧条目。"""
        now = time.time()
        max_warn_window = max(
            self.config.warning.spam_warn_window,
            self.config.warning.abuse_warn_window,
            self.config.warning.ad_warn_window,
            3600,
        )
        keep_seconds = max_warn_window * 2
        for gid in list(self._warnings.keys()):
            for uid in list(self._warnings[gid].keys()):
                for vtype in list(self._warnings[gid][uid].keys()):
                    self._warnings[gid][uid][vtype] = [
                        (ts, c) for ts, c in self._warnings[gid][uid][vtype]
                        if now - ts <= keep_seconds
                    ]
                    if not self._warnings[gid][uid][vtype]:
                        del self._warnings[gid][uid][vtype]
                if not self._warnings[gid][uid]:
                    del self._warnings[gid][uid]
            if not self._warnings[gid]:
                del self._warnings[gid]
        known_ttl = 3600
        for k in list(self._known_roles.keys()):
            if now - self._known_roles[k][1] > known_ttl:
                del self._known_roles[k]
        if len(self._known_roles) > 2000:
            keys = sorted(self._known_roles.keys(), key=lambda k: self._known_roles[k][1])
            for k in keys[:len(keys) - 1000]:
                del self._known_roles[k]
        if len(self._stream_to_group) > 1000:
            keys = list(self._stream_to_group.keys())
            for k in keys[:-500]:
                del self._stream_to_group[k]
        mute_cooldown_max = max(self.config.safeguard.mute_cooldown, 300) * 3
        for k in list(self._last_mute_time.keys()):
            if now - self._last_mute_time[k] > mute_cooldown_max:
                del self._last_mute_time[k]
        for gid in list(self._get_member_called.keys()):
            for uid in list(self._get_member_called[gid].keys()):
                if now - self._get_member_called[gid][uid] > 300:
                    del self._get_member_called[gid][uid]
            if not self._get_member_called[gid]:
                del self._get_member_called[gid]
        for key in list(self._recent_user_messages.keys()):
            self._recent_user_messages[key] = deque(
                ((ts, text) for ts, text in self._recent_user_messages[key] if now - ts <= 900),
                maxlen=8,
            )
            if not self._recent_user_messages[key]:
                del self._recent_user_messages[key]
        for key, task in list(self._audit_tasks.items()):
            if task.done():
                del self._audit_tasks[key]
        for msg_id, ts in list(self._audit_seen_messages.items()):
            if now - ts > 900:
                del self._audit_seen_messages[msg_id]
        self._last_cleanup_time = now

    # ===== Prompt 构建 =====

    PROMPT_MARKER = "[群管理助手 管理上下文]"

    def _build_admin_prompt(self, group_id: int, role: str) -> str:
        """模块化构建管理提示词。"""
        sections: list[str] = [self.PROMPT_MARKER]
        # 动态可用操作（注入到 auto_moderate_system 的 {available_actions} 占位）
        if role == "owner":
            available = "禁言/解禁/踢人/警告/设精华/撤回/公告/改名/审批入群"
        elif role == "admin":
            available = "禁言/解禁/踢人/警告/设精华/撤回/改名片/审批入群"
        else:
            available = "管理操作受限，可协助管理员做决策建议"
        # 核心规则（替换模板变量）
        core = self.config.prompts.auto_moderate_system
        core = core.replace("{bot_role}", role).replace("{available_actions}", available)
        sections.append(core)
        sections.append("以上为群管理参考信息，不要在你的回复中引用或解释这一段文字。")
        return "\n\n".join(sections)

    def _extract_message_text(self, message: Any) -> str:
        if not isinstance(message, dict):
            return str(message or "").strip()
        reply_text = self._extract_non_reply_segment_text(message)
        if reply_text:
            return reply_text
        for key in ("plain_text", "processed_plain_text", "raw_message", "text", "content"):
            value = message.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
        segments = message.get("message_segments")
        if isinstance(segments, list):
            parts = self._extract_segment_text_parts(segments)
            if parts:
                return " ".join(parts).strip()
        return ""

    def _is_reply_context_segment(self, seg: Any) -> bool:
        if not isinstance(seg, dict):
            return False
        seg_type = str(seg.get("type") or seg.get("message_type") or "").strip().lower()
        return seg_type in ("reply", "quote", "quoted", "reference", "source")

    def _extract_segment_text_parts(self, segments: Any, skip_reply_context: bool = False) -> list[str]:
        if not isinstance(segments, list):
            return []
        parts: list[str] = []
        for seg in segments:
            if not isinstance(seg, dict):
                continue
            if skip_reply_context and self._is_reply_context_segment(seg):
                continue
            data = seg.get("data", {})
            if isinstance(data, str):
                if data.strip():
                    parts.append(data)
            elif isinstance(data, dict):
                for key in ("text", "content", "summary", "title", "name", "desc"):
                    text = data.get(key)
                    if text:
                        parts.append(str(text))
                news = data.get("news")
                if isinstance(news, list):
                    for item in news:
                        if isinstance(item, dict):
                            item_text = item.get("text") or item.get("title") or item.get("content")
                            if item_text:
                                parts.append(str(item_text))
        return parts

    def _extract_non_reply_segment_text(self, message: dict[str, Any]) -> str:
        for key in ("message_segments", "raw_message", "segments"):
            segments = message.get(key)
            if not isinstance(segments, list):
                continue
            if not any(self._is_reply_context_segment(seg) for seg in segments if isinstance(seg, dict)):
                continue
            parts = self._extract_segment_text_parts(segments, skip_reply_context=True)
            text = " ".join(parts).strip()
            if text:
                return text
        return ""

    def _iter_message_segments(self, value: Any, skip_reply_context: bool = False):
        if isinstance(value, dict):
            if skip_reply_context and self._is_reply_context_segment(value):
                return
            yield value
            for key in ("message_segments", "raw_message", "segments", "content", "data", "message"):
                nested = value.get(key)
                if nested is value:
                    continue
                yield from self._iter_message_segments(nested, skip_reply_context=skip_reply_context)
        elif isinstance(value, list):
            for item in value:
                yield from self._iter_message_segments(item, skip_reply_context=skip_reply_context)

    def _extract_image_segments(self, message: Any) -> list[dict[str, str]]:
        if not isinstance(message, dict):
            return []
        images: list[dict[str, str]] = []
        seen: set[tuple[str, str, str, str]] = set()
        for seg in self._iter_message_segments(message, skip_reply_context=True):
            seg_type = str(seg.get("type") or seg.get("message_type") or "").strip().lower()
            data = seg.get("data", {})
            data_dict = data if isinstance(data, dict) else {}
            if seg_type not in ("image", "emoji") and not any(
                data_dict.get(key) or seg.get(key)
                for key in ("binary_data_base64", "base64", "image_base64", "emoji_base64", "hash")
            ):
                continue
            image_hash = str(
                seg.get("hash")
                or seg.get("binary_hash")
                or data_dict.get("hash")
                or data_dict.get("file_hash")
                or data_dict.get("image_hash")
                or ""
            ).strip()
            image_base64 = str(
                seg.get("binary_data_base64")
                or seg.get("base64")
                or seg.get("image_base64")
                or seg.get("emoji_base64")
                or data_dict.get("binary_data_base64")
                or data_dict.get("base64")
                or data_dict.get("image_base64")
                or data_dict.get("emoji_base64")
                or ""
            ).strip()
            image_format = str(
                seg.get("image_format")
                or seg.get("format")
                or data_dict.get("image_format")
                or data_dict.get("format")
                or ""
            ).strip().lower()
            image_url = str(
                seg.get("url")
                or seg.get("file")
                or data_dict.get("url")
                or data_dict.get("file")
                or ""
            ).strip()
            if not image_format:
                filename = image_url
                suffix = filename.rsplit(".", 1)[-1].lower() if "." in filename else ""
                image_format = suffix if suffix in ("jpg", "jpeg", "png", "gif", "webp") else ""
            if image_hash or image_base64 or image_url.startswith(("http://", "https://")):
                key = (image_hash, image_base64[:64], image_url, image_format)
                if key in seen:
                    continue
                seen.add(key)
                images.append({"hash": image_hash, "base64": image_base64, "url": image_url, "format": image_format})
        return images

    def _is_forwarded_chat_record(self, message: Any, text: str = "") -> bool:
        """Best-effort detection for QQ merged-forward chat records."""
        if isinstance(message, dict):
            for seg in self._iter_message_segments(message, skip_reply_context=True):
                if not isinstance(seg, dict):
                    continue
                seg_type = str(seg.get("type") or seg.get("message_type") or "").strip().lower()
                data = seg.get("data", {})
                data_dict = data if isinstance(data, dict) else {}
                if seg_type in ("forward", "merged_forward", "forward_msg", "node"):
                    return True
                if seg_type in ("xml", "json"):
                    payload = " ".join(
                        str(value)
                        for value in (
                            data if isinstance(data, str) else "",
                            data_dict.get("data", ""),
                            data_dict.get("content", ""),
                            data_dict.get("text", ""),
                        )
                        if value
                    )
                    if any(marker in payload for marker in ("聊天记录", "合并转发", "转发的聊天记录")):
                        return True
                if any(data_dict.get(key) for key in ("forward_id", "resid", "node_id")) and any(
                    marker in str(data_dict) for marker in ("聊天记录", "转发")
                ):
                    return True

        normalized = re.sub(r"\s+", "", str(text or ""))
        if not normalized:
            return False
        markers = (
            "合并转发",
            "转发聊天记录",
            "转发的聊天记录",
            "群聊的聊天记录",
            "查看转发消息",
            "这串转发",
            "[聊天记录]",
            "【聊天记录】",
        )
        if any(marker in normalized for marker in markers):
            return True
        if re.search(r".{1,30}的聊天记录", normalized):
            return True
        return False

    def _extract_forward_record_ids(self, message: Any, text: str = "") -> list[str]:
        ids: list[str] = []

        def _add(value: Any) -> None:
            token = str(value or "").strip()
            if token and token not in ids:
                ids.append(token)

        if isinstance(message, dict):
            for seg in self._iter_message_segments(message, skip_reply_context=True):
                if not isinstance(seg, dict):
                    continue
                seg_type = str(seg.get("type") or seg.get("message_type") or "").strip().lower()
                data = seg.get("data", {})
                data_dict = data if isinstance(data, dict) else {}
                if seg_type in ("forward", "merged_forward", "forward_msg"):
                    for key in ("forward_id", "resid", "id", "file"):
                        _add(seg.get(key) or data_dict.get(key))
                for key in ("forward_id", "resid"):
                    _add(seg.get(key) or data_dict.get(key))

        for pattern in (
            r"(?:forward_id|resid)\s*[:=]\s*['\"]?([A-Za-z0-9_\-+/=]{8,})",
            r'"(?:forward_id|resid)"\s*:\s*"([^"]+)"',
        ):
            for match in re.finditer(pattern, str(text or "")):
                _add(match.group(1))
        return ids

    def _merge_image_segments(self, first: list[dict[str, str]], second: list[dict[str, str]]) -> list[dict[str, str]]:
        merged: list[dict[str, str]] = []
        seen: set[tuple[str, str, str, str]] = set()
        for image in [*first, *second]:
            if not isinstance(image, dict):
                continue
            normalized = {
                "hash": str(image.get("hash") or "").strip(),
                "base64": str(image.get("base64") or "").strip(),
                "url": str(image.get("url") or "").strip(),
                "format": str(image.get("format") or "").strip(),
            }
            key = (normalized["hash"], normalized["base64"][:64], normalized["url"], normalized["format"])
            if key in seen or not (normalized["hash"] or normalized["base64"] or normalized["url"]):
                continue
            seen.add(key)
            merged.append(normalized)
        return merged

    def _render_forward_payload_text(self, value: Any, depth: int = 0) -> str:
        if depth > 8:
            return ""
        if isinstance(value, str):
            return value.strip()
        if isinstance(value, list):
            parts = self._extract_segment_text_parts(value)
            if parts:
                return " ".join(parts).strip()
            return "\n".join(
                part for item in value if (part := self._render_forward_payload_text(item, depth + 1))
            ).strip()
        if not isinstance(value, dict):
            return ""

        data = value.get("data", {})
        data_dict = data if isinstance(data, dict) else {}
        seg_type = str(value.get("type") or value.get("message_type") or "").strip().lower()
        if seg_type == "text":
            return str(value.get("text") or data_dict.get("text") or data or "").strip()

        for key in ("text", "content", "summary", "title", "desc", "message"):
            content = value.get(key)
            if content:
                rendered = self._render_forward_payload_text(content, depth + 1)
                if rendered:
                    return rendered
            content = data_dict.get(key)
            if content:
                rendered = self._render_forward_payload_text(content, depth + 1)
                if rendered:
                    return rendered
        return ""

    def _render_forward_payload(self, payload: Any, depth: int = 0) -> tuple[str, list[dict[str, str]]]:
        if depth > 8:
            return "", []
        images: list[dict[str, str]] = []
        lines: list[str] = []

        if isinstance(payload, dict):
            images = self._merge_image_segments(images, self._extract_image_segments(payload))
            data = payload.get("data")
            if isinstance(data, dict) and any(key in data for key in ("messages", "message", "content")):
                text, nested_images = self._render_forward_payload(data, depth + 1)
                return text, self._merge_image_segments(images, nested_images)

            for list_key in ("messages", "nodes", "forward", "forward_messages"):
                items = payload.get(list_key)
                if isinstance(items, list):
                    for item in items:
                        text, nested_images = self._render_forward_payload(item, depth + 1)
                        if text:
                            lines.append(text)
                        images = self._merge_image_segments(images, nested_images)
                    return "\n".join(lines).strip(), images

            content = payload.get("content") or payload.get("message") or (data.get("content") if isinstance(data, dict) else None)
            if content:
                sender = ""
                if isinstance(data, dict):
                    sender = str(data.get("name") or data.get("nickname") or data.get("uin") or "").strip()
                sender = sender or str(payload.get("name") or payload.get("nickname") or payload.get("user_id") or "").strip()
                text = self._render_forward_payload_text(content, depth + 1)
                _, nested_images = self._render_forward_payload(content, depth + 1)
                images = self._merge_image_segments(images, nested_images)
                if text:
                    return (f"【{sender}】: {text}" if sender else text), images

            text = self._render_forward_payload_text(payload, depth + 1)
            return text, images

        if isinstance(payload, list):
            for item in payload:
                text, nested_images = self._render_forward_payload(item, depth + 1)
                if text:
                    lines.append(text)
                images = self._merge_image_segments(images, nested_images)
            return "\n".join(lines).strip(), images

        return self._render_forward_payload_text(payload, depth + 1), images

    async def _expand_forwarded_record_for_audit(
        self,
        message: Any,
        text: str,
        image_segments: list[dict[str, str]],
    ) -> tuple[str, list[dict[str, str]]]:
        forward_ids = self._extract_forward_record_ids(message, text)
        if not forward_ids:
            return text, image_segments

        api_names = (
            "adapter.napcat.message.get_forward_msg",
            "adapter.napcat.get_forward_msg",
            "adapter.napcat.forward.get_forward_msg",
            "adapter.onebot.get_forward_msg",
            "get_forward_msg",
        )
        arg_names = ("message_id", "id", "forward_id", "resid")
        last_error = ""
        for forward_id in forward_ids[:3]:
            for api_name in api_names:
                for arg_name in arg_names:
                    ok, data = await self._call_api(api_name=api_name, **{arg_name: forward_id})
                    if not ok:
                        last_error = str(data)
                        continue
                    expanded_text, expanded_images = self._render_forward_payload(data)
                    if not expanded_text and not expanded_images:
                        continue
                    merged_images = self._merge_image_segments(image_segments, expanded_images)
                    if expanded_text and expanded_text not in text:
                        text = f"{text}\n\n[合并转发展开内容]\n{expanded_text}".strip()
                    if self.config.logging.verbose_logging:
                        self.ctx.logger.info(
                            f"[群管理] 合并转发展开成功: id={forward_id} api={api_name} "
                            f"text_len={len(expanded_text)} images={len(expanded_images)}"
                        )
                    return text, merged_images

        if self.config.logging.verbose_logging:
            self.ctx.logger.warning(
                f"[群管理] 合并转发展开失败: ids={forward_ids[:3]} err={last_error or 'no usable get_forward_msg api'}"
            )
        return text, image_segments

    def _decode_image_base64(self, image_base64: str) -> bytes:
        image_base64 = str(image_base64 or "").strip()
        if image_base64.startswith("data:") and ";base64," in image_base64:
            image_base64 = image_base64.split(";base64,", maxsplit=1)[1].strip()
        return base64.b64decode(image_base64, validate=True)

    def _download_image_url_sync(self, image_url: str, timeout: float = 8.0, max_bytes: int = 10 * 1024 * 1024) -> bytes:
        image_url = str(image_url or "").strip()
        if not image_url.startswith(("http://", "https://")):
            return b""
        request = urllib.request.Request(image_url, headers={"User-Agent": "MaiBot-GroupAdmin/1.0"})
        with urllib.request.urlopen(request, timeout=timeout) as response:
            content_type = str(response.headers.get("Content-Type") or "").lower()
            if content_type and not content_type.startswith("image/"):
                return b""
            data = response.read(max_bytes + 1)
        if len(data) > max_bytes:
            return b""
        return data

    def _extract_message_user_id(self, message: Any, kwargs: dict[str, Any]) -> int:
        for key in ("user_id", "sender_id"):
            uid = self._to_int(kwargs.get(key, 0))
            if uid:
                return uid
        if not isinstance(message, dict):
            return 0
        mbi = message.get("message_base_info", {}) or {}
        uid = self._to_int(mbi.get("user_id", 0))
        if uid:
            return uid
        mi = message.get("message_info", {}) or {}
        ui = mi.get("user_info", {}) or {}
        return self._to_int(ui.get("user_id", 0))

    def _extract_json_object(self, text: str) -> dict[str, Any]:
        text = text.strip()
        candidates = [text]
        match = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, flags=re.S)
        if match:
            candidates.insert(0, match.group(1))
        start = text.find("{")
        end = text.rfind("}")
        if start >= 0 and end > start:
            candidates.append(text[start : end + 1])
        for candidate in candidates:
            try:
                data = json.loads(candidate)
            except Exception:
                continue
            if isinstance(data, dict):
                return data
        return {}

    def _is_vlm_sensitive_refusal(self, text: str) -> bool:
        if not text.lstrip().startswith("[图片消息]"):
            return False
        refusal_markers = (
            "抱歉，我无法满足",
            "无法按要求描述",
            "无法描述这张图片",
            "不能描述这张图片",
            "不能协助描述",
            "无法提供该图片的具体内容",
        )
        return any(marker in text for marker in refusal_markers)

    def _build_audit_prompt(
        self,
        group_id: int,
        user_id: int,
        text: str,
        history: list[tuple[float, str]],
        forwarded_record_single_message: bool = False,
    ) -> str:
        recent_lines = []
        now = time.time()
        for ts, item in history[-6:]:
            age = max(0, int(now - ts))
            recent_lines.append(f"- {age}s ago: {item}")
        recent = "\n".join(recent_lines) if recent_lines else "- none"
        forwarded_note = (
            "注意：current_message 是用户发送的一条QQ合并转发聊天记录。请把整串转发作为这一条当前消息整体判断；"
            "刷屏判定时只按当前发送者发了一条消息计数，不要把转发内部的多条聊天记录当成该用户连续刷屏，也不要按内部发言人的 user_id 处理。"
            "但内容审核仍需认真检查转发内部所有可见文本和可取得的图片描述：如果整串转发传播广告、诈骗、色情、违法、严重辱骂、隐私泄露等风险内容，"
            "应按当前发送者传播该内容处理。\n\n"
            if forwarded_record_single_message
            else ""
        )
        return (
            "你是QQ群入站消息审核器。请根据语义判断是否需要群管理动作，不要按关键词机械判定。\n"
            "你需要识别：连续刷屏、辱骂/人身攻击、广告/诈骗/赌博/交易诱导、色情/违法/血腥惊吓、恶意引战。\n"
            "如果 current_message 是图片描述，需按图片内容判断；图片描述模型拒答（如“抱歉，我无法满足该请求”、“无法描述该图片”、“不能协助”）"
            "代表视觉模型已经遇到高风险敏感图像，不允许返回 none；应按最可能类型返回 warn 或 mute，并设置 recall=true。\n"
            "如果图片描述出现裸露、色情意味、露骨、挑逗姿势、性暗示、下体/性器官、体液、血腥暴力或违法内容，也不允许仅提醒不撤回；"
            "应设置 recall=true，violation_type 优先使用 sexual 或 illegal。\n"
            "普通口癖、玩笑、轻微情绪、无明确对象的吐槽、正常聊天，一律不要处罚。\n"
            "只有把握较高时才返回 warn 或 mute；不确定必须返回 none。\n\n"
            "recall 表示是否应该撤回当前这条消息：广告/诈骗链接、严重辱骂、色情违法、隐私泄露等应为 true；"
            "普通刷屏或不确定时应为 false。\n"
            f"{forwarded_note}"
            "Do not classify a single file share or a single link as spam. Only classify spam when same_user_recent_messages shows the same user repeatedly sent highly similar content.\n"
            f"group_id: {group_id}\nuser_id: {user_id}\n"
            f"same_user_recent_messages:\n{recent}\n\n"
            f"current_message:\n{text}\n\n"
            "只输出一个 JSON 对象，不要解释：\n"
            '{"action":"none|warn|mute","violation_type":"none|spam|abuse|ad|sexual|illegal|conflict",'
            '"confidence":0.0,"duration":0,"recall":false,"reason":"简短中文原因"}\n'
            "duration 仅 action=mute 时使用，单位秒；轻度刷屏 300-600，辱骂/广告 600-1800，严重风险最高 3600。"
        )

    async def _generate_moderation_notice(self, violation_type: str, reason: str) -> str:
        prompt = (
            "你是当前群聊里的角色“星期六”，需要对刚刚的群管理处理自然接一句短话。\n"
            "要求：保持人设和口吻，像群里自然说话；不要说“已警告/已禁言/系统判定/插件/审核”；"
            "不要长篇说教，不要@任何人，不要输出括号说明。\n"
            f"违规类型：{violation_type}\n原因：{reason}\n"
            "只输出一句中文短回复。"
        )
        try:
            result = await self.ctx.llm.generate(prompt=prompt, model="replyer", temperature=0.6, max_tokens=60)
            if isinstance(result, dict) and result.get("success"):
                text = str(result.get("response", "")).strip()
                text = re.sub(r"^```.*?```$", "", text, flags=re.S).strip()
                text = text.strip('"').strip("'").strip("“”「」")
                if text:
                    return text[:120]
        except Exception as e:
            self.ctx.logger.warning(f"[群管理] 生成人设提醒失败: {e}")
        return "先停一下，这个不太适合继续刷屏喵。"

    async def _resolve_group_stream_id(self, group_id: int, stream_id: str = "") -> str:
        stream_id = str(stream_id or "").strip()
        if stream_id:
            return stream_id
        try:
            stream = await self.ctx.call_capability("chat.get_stream_by_group_id", group_id=str(group_id), platform="qq")
            if isinstance(stream, dict):
                sid = str(stream.get("stream_id") or stream.get("session_id") or stream.get("id") or "").strip()
                if sid:
                    return sid
            opened = await self.ctx.call_capability(
                "chat.open_session",
                platform="qq",
                chat_type="group",
                group_id=str(group_id),
            )
            if isinstance(opened, dict):
                stream_obj = opened.get("stream") if isinstance(opened.get("stream"), dict) else opened
                sid = str(stream_obj.get("stream_id") or stream_obj.get("session_id") or stream_obj.get("id") or "").strip()
                if sid:
                    return sid
        except Exception as e:
            self.ctx.logger.warning(f"[群管理] 解析群聊stream失败: group={group_id} err={e}")
        return ""

    async def _trigger_native_moderation_reply(self, stream_id: str, group_id: int, action: str, violation_type: str, reason: str):
        stream_id = await self._resolve_group_stream_id(group_id, stream_id)
        if not stream_id:
            if self.config.logging.verbose_logging:
                self.ctx.logger.warning(f"[群管理] 无法触发Maisaka回复: 缺少stream_id group={group_id}")
            return
        intent = (
            "群管理插件刚刚完成了一次违规处理。请基于当前聊天上下文走原生回复流程，"
            "用你的人设自然回应一句，重点是维持群聊氛围和说明边界；"
            "不要机械播报禁言结果，不要复述违规原文或链接，不要提插件、审核流程或JSON。"
            f"处理动作={action}，违规类型={violation_type}，原因={reason}"
        )
        try:
            result = await self.ctx.call_capability(
                "maisaka.proactive.trigger",
                stream_id=stream_id,
                intent=intent,
                reason="group_admin_moderation_action",
                priority="high",
                metadata={
                    "group_id": group_id,
                    "action": action,
                    "violation_type": violation_type,
                    "reason": reason,
                },
            )
            if self.config.logging.verbose_logging:
                self.ctx.logger.info(f"[群管理] 已触发Maisaka原生回复: group={group_id} stream={stream_id} result={result}")
        except Exception as e:
            self.ctx.logger.warning(f"[群管理] 触发Maisaka原生回复失败: group={group_id} stream={stream_id} err={e}")

    async def _maybe_recall_audited_message(self, group_id: int, message_id: str, reason: str) -> None:
        if not message_id or self._to_int(message_id) <= 0:
            if self.config.logging.verbose_logging:
                self.ctx.logger.info(f"[群管理] 跳过自动撤回: message_id无效 group={group_id} mid={message_id}")
            return
        result = await self.tool_recall_msg(group_id=group_id, message_id=message_id, reason=reason)
        if self.config.logging.verbose_logging:
            self.ctx.logger.info(f"[群管理] 自动撤回结果: group={group_id} mid={message_id} result={result}")

    async def _get_image_description_for_audit(self, image_info: dict[str, str], timeout: float = 12.0) -> str:
        image_hash = str(image_info.get("hash") or "").strip()
        image_base64 = str(image_info.get("base64") or "").strip()
        image_url = str(image_info.get("url") or "").strip()
        try:
            from src.chat.image_system.image_manager import image_manager
        except Exception as e:
            self.ctx.logger.warning(f"[群管理] 无法导入图片描述管理器: {e}")
            return ""

        async def _read_description_once() -> str:
            if image_hash:
                desc = await image_manager.get_image_description(image_hash=image_hash, wait_for_build=True)
                if desc:
                    return str(desc).strip()
            if image_base64:
                image_bytes = self._decode_image_base64(image_base64)
                desc = await image_manager.get_image_description(image_bytes=image_bytes, wait_for_build=True)
                if desc:
                    return str(desc).strip()
            if image_url:
                image_bytes = await asyncio.to_thread(self._download_image_url_sync, image_url)
                if image_bytes:
                    desc = await image_manager.get_image_description(image_bytes=image_bytes, wait_for_build=True)
                    if desc:
                        return str(desc).strip()
            return ""

        deadline = time.time() + timeout
        last_error = ""
        while time.time() < deadline:
            try:
                desc = await asyncio.wait_for(_read_description_once(), timeout=max(0.5, min(3.0, deadline - time.time())))
                if desc:
                    return desc
            except Exception as e:
                last_error = str(e)
                if not image_hash:
                    break
            await asyncio.sleep(0.5)
        if self.config.logging.verbose_logging:
            self.ctx.logger.info(
                f"[群管理] 图片描述为空或超时: hash={image_hash[:12]} has_base64={bool(image_base64)} err={last_error}"
            )
        return ""

    async def _run_image_moderation(
        self,
        group_id: int,
        user_id: int,
        images: list[dict[str, str]],
        stream_id: str = "",
        message_id: str = "",
    ) -> None:
        try:
            descriptions: list[str] = []
            for index, image_info in enumerate(images[:4], start=1):
                desc = await self._get_image_description_for_audit(image_info)
                if desc:
                    image_hash = str(image_info.get("hash") or "").strip()
                    descriptions.append(f"{index}. hash={image_hash[:12] or 'unknown'} 描述：{desc}")
            if not descriptions:
                if self.config.logging.verbose_logging:
                    self.ctx.logger.info(f"[群管理] 图片审核跳过: 未取得图片描述 group={group_id} user={user_id} mid={message_id}")
                return
            audit_text = "[图片消息] 图片描述：\n" + "\n".join(descriptions)
            if self.config.logging.verbose_logging:
                self.ctx.logger.info(
                    f"[群管理] 图片描述已取得，提交LLM审核: group={group_id} user={user_id} "
                    f"mid={message_id} descriptions={len(descriptions)}"
                )
            self._schedule_llm_moderation(
                group_id,
                user_id,
                audit_text,
                message_id,
                stream_id,
                audit_kind="image",
            )
        except Exception as e:
            self.ctx.logger.warning(f"[群管理] 图片审核任务异常: group={group_id} user={user_id} mid={message_id} err={e}")

    def _schedule_image_moderation(
        self,
        group_id: int,
        user_id: int,
        images: list[dict[str, str]],
        message_id: str = "",
        stream_id: str = "",
    ) -> None:
        if not images or group_id <= 0 or user_id <= 0:
            return
        seen_key = f"image_fetch:{message_id}" if message_id else ""
        if seen_key:
            if seen_key in self._audit_seen_messages:
                return
            self._audit_seen_messages[seen_key] = time.time()
        task_key: tuple[Any, ...] = (group_id, user_id, "image_fetch", message_id or str(time.time()))
        existing = self._audit_tasks.get(task_key)
        if existing and not existing.done():
            return
        if self.config.logging.verbose_logging:
            self.ctx.logger.info(
                f"[群管理] 图片审核排队: group={group_id} user={user_id} mid={message_id} images={len(images)}"
            )
        self._audit_tasks[task_key] = asyncio.create_task(
            self._run_image_moderation(group_id, user_id, images, stream_id, message_id)
        )

    async def _run_llm_moderation(
        self,
        group_id: int,
        user_id: int,
        text: str,
        stream_id: str = "",
        message_id: str = "",
        task_key: tuple[Any, ...] | None = None,
        forwarded_record_single_message: bool = False,
        history_snapshot: list[tuple[float, str]] | None = None,
    ):
        try:
            if self.config.logging.verbose_logging:
                self.ctx.logger.info(f"[群管理] 入站LLM审核开始: group={group_id} user={user_id} text_len={len(text)}")
            is_protected, msg = await self._is_protected(group_id, user_id)
            if is_protected:
                if self.config.logging.verbose_logging:
                    self.ctx.logger.info(f"[群管理] 入站审核跳过受保护用户: group={group_id} user={user_id} reason={msg}")
                return
            history = history_snapshot if history_snapshot is not None else list(self._recent_user_messages.get((group_id, user_id), []))
            prompt = self._build_audit_prompt(group_id, user_id, text, history, forwarded_record_single_message)
            audit_model = str(self.config.auto_moderate.audit_model or "planner").strip() or "planner"
            audit_max_tokens = self._to_int(self.config.auto_moderate.audit_max_tokens)
            if audit_max_tokens <= 0:
                audit_max_tokens = 220
            result = await self.ctx.llm.generate(
                prompt=prompt,
                model=audit_model,
                temperature=0.0,
                max_tokens=audit_max_tokens,
            )
            if not isinstance(result, dict) or not result.get("success"):
                self.ctx.logger.warning(f"[群管理] 入站LLM审核失败: {result}")
                return
            verdict = self._extract_json_object(str(result.get("response", "")).strip())
            action = str(verdict.get("action", "none")).strip().lower()
            violation_type = str(verdict.get("violation_type", "none")).strip().lower()
            reason = str(verdict.get("reason", "")).strip()[:120] or "入站审核判定违规"
            try:
                confidence = float(verdict.get("confidence", 0))
            except Exception:
                confidence = 0.0
            recall_raw = verdict.get("recall", False)
            recall_message = recall_raw is True or str(recall_raw).strip().lower() in ("true", "1", "yes")
            is_image_audit = text.lstrip().startswith("[图片消息]")
            if self.config.logging.verbose_logging:
                self.ctx.logger.info(
                    f"[群管理] 入站LLM审核结论: group={group_id} user={user_id} "
                    f"action={action} type={violation_type} confidence={confidence:.2f} "
                    f"recall={recall_message} reason={reason}"
                )
            if (action not in ("warn", "mute") or confidence < 0.72) and is_image_audit and self._is_vlm_sensitive_refusal(text):
                action = "warn"
                violation_type = "sexual"
                confidence = max(confidence, 0.9)
                recall_message = True
                reason = "图片描述模型拒绝描述，疑似敏感涉黄内容"
                if self.config.logging.verbose_logging:
                    self.ctx.logger.info(
                        f"[群管理] 图片审核按VLM拒答升级: group={group_id} user={user_id} mid={message_id}"
                    )
            if action not in ("warn", "mute") or confidence < 0.72:
                return
            valid_violation_types = ("spam", "abuse", "ad", "sexual", "illegal", "conflict")
            if violation_type not in valid_violation_types:
                if self.config.logging.verbose_logging:
                    self.ctx.logger.info(
                        f"[群管理] 入站LLM审核跳过矛盾结论: group={group_id} user={user_id} "
                        f"action={action} type={violation_type} confidence={confidence:.2f} reason={reason}"
                    )
                return
            negative_reason_markers = (
                "未涉及违规",
                "不涉及违规",
                "无违规",
                "没有违规",
                "未发现违规",
                "正常聊天",
                "正常内容",
                "普通聊天",
                "无需处理",
                "无需处罚",
                "不需要处理",
                "不应处罚",
            )
            if any(marker in reason for marker in negative_reason_markers):
                if self.config.logging.verbose_logging:
                    self.ctx.logger.info(
                        f"[群管理] 入站LLM审核跳过否定原因: group={group_id} user={user_id} "
                        f"action={action} type={violation_type} confidence={confidence:.2f} reason={reason}"
                    )
                return
            if violation_type == "conflict":
                violation_type = "abuse"
            if is_image_audit and violation_type in ("sexual", "illegal", "ad", "abuse"):
                recall_message = True
            if action == "warn":
                result = await self.tool_warn_user(group_id=group_id, user_id=user_id, violation_type=violation_type, reason=reason)
                content = str(result.get("content", "")) if isinstance(result, dict) else ""
                if content.startswith("已向"):
                    if recall_message:
                        await self._maybe_recall_audited_message(group_id, message_id, reason)
                    await self._trigger_native_moderation_reply(stream_id, group_id, action, violation_type, reason)
                return
            duration = self._to_int(verdict.get("duration", 0))
            if duration <= 0:
                if violation_type == "spam":
                    duration = 600
                elif violation_type == "illegal":
                    duration = 3600
                else:
                    duration = 1800
            result = await self.tool_mute_user(group_id=group_id, user_id=user_id, duration=duration, reason=reason)
            content = str(result.get("content", "")) if isinstance(result, dict) else ""
            if content.startswith("已将"):
                if recall_message:
                    await self._maybe_recall_audited_message(group_id, message_id, reason)
                await self._trigger_native_moderation_reply(stream_id, group_id, action, violation_type, reason)
        except Exception as e:
            self.ctx.logger.error(f"[群管理] 入站LLM审核异常: {e}", exc_info=True)
        finally:
            self._audit_tasks.pop(task_key or (group_id, user_id), None)

    def _schedule_llm_moderation(
        self,
        group_id: int,
        user_id: int,
        text: str,
        message_id: str = "",
        stream_id: str = "",
        audit_kind: str = "text",
        forwarded_record_single_message: bool = False,
    ):
        if not text or group_id <= 0 or user_id <= 0:
            return
        seen_key = f"{audit_kind}:{message_id}" if message_id else ""
        if seen_key:
            if seen_key in self._audit_seen_messages:
                return
            self._audit_seen_messages[seen_key] = time.time()
        key: tuple[Any, ...] = (group_id, user_id) if audit_kind == "text" else (group_id, user_id, audit_kind, message_id or str(time.time()))
        history = self._recent_user_messages.setdefault(key, deque(maxlen=8))
        history_snapshot = list(history)
        history_text = "[QQ合并转发聊天记录，按单条消息计入近期历史]" if forwarded_record_single_message else text
        existing = self._audit_tasks.get(key)
        if existing and not existing.done():
            history.append((time.time(), history_text))
            if self.config.logging.verbose_logging:
                self.ctx.logger.info(f"[群管理] 入站LLM审核合并: group={group_id} user={user_id} pending=1")
            return
        history.append((time.time(), history_text))
        if self.config.logging.verbose_logging:
            self.ctx.logger.info(f"[群管理] 入站LLM审核排队: group={group_id} user={user_id} text_len={len(text)}")
        self._audit_tasks[key] = asyncio.create_task(
            self._run_llm_moderation(
                group_id,
                user_id,
                text,
                stream_id,
                message_id,
                key,
                forwarded_record_single_message,
                history_snapshot,
            )
        )

    def _resolve_group_id_from_hook(self, kwargs: dict) -> int:
        """从 hook kwargs 中解析 group_id（强约束：显式字段+message_info+缓存）。"""
        # 1. 优先显式字段
        for key in ("group_id", "group", "gid", "chat_id"):
            gid = self._to_int(kwargs.get(key, 0))
            if gid > 0:
                return gid
        # 2. message_info
        msg = kwargs.get("message", {})
        if isinstance(msg, dict):
            mi = msg.get("message_info", {}) or {}
            gi = mi.get("group_info", {}) or {}
            gid = self._to_int(gi.get("group_id", 0))
            if gid > 0:
                return gid
        # 3. 缓存查找（chat.receive.after_process 写入）
        for key in ("reply_message_id", "session_id", "stream_id", "chat_id"):
            sid = str(kwargs.get(key, ""))
            if sid:
                gid = self._stream_to_group.get(sid, 0)
                if gid > 0:
                    return gid
        return 0

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
            self.ctx.logger.warning(f"[群管理] API调用失败: {api_name}: {e}")
            return False, str(e)

    async def _call_action_api(self, api_name: str, **params: Any) -> tuple[bool, Any]:
        try:
            result = await self.ctx.api.call(api_name=api_name, version="1", params=params)
            return True, result
        except Exception as e:
            self.ctx.logger.warning(f"[群管理] API调用失败: {api_name}: {e}")
            return False, str(e)

    async def _check_daily_reset(self, group_id: int):
        today = self._today_key()
        for cnt_dict in (self._daily_mute_count, self._daily_kick_count, self._daily_approve_count, self._daily_reject_count):
            if group_id not in cnt_dict: cnt_dict[group_id] = {}
            for old_day in list(cnt_dict[group_id].keys()):
                if old_day != today:
                    del cnt_dict[group_id][old_day]
            if today not in cnt_dict[group_id]:
                cnt_dict[group_id][today] = 0

    async def _check_target_role(self, group_id: int, user_id: int) -> Optional[str]:
        key = (group_id, user_id)
        if key in self._known_roles:
            role, ts = self._known_roles[key]
            if time.time() - ts < 3600:
                return role
        try:
            ok, data = await self._call_api(api_name="adapter.napcat.group.get_group_member_info", group_id=group_id, user_id=user_id, no_cache=True)
            if ok and isinstance(data, dict):
                role = data.get("role", "")
                self._known_roles[key] = (role, time.time())
                return role
        except Exception as e:
            self.ctx.logger.warning(f"[群管理] 查询用户身份失败: {e}")
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
                    self_id = self._bot_self_id
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
                self.ctx.logger.warning(f"[群管理] Bot角色检测失败: group={group_id}: {e}")
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

    def _count_ops_in_window(self, group_id: int, user_id: int, window_hours: float) -> int:
        cutoff = datetime.now() - timedelta(hours=window_hours)
        return sum(1 for e in self._op_log if e["group_id"] == group_id and e["target_user_id"] == user_id and e["action"] in ("warn", "mute", "kick") and datetime.fromisoformat(e["timestamp"]) > cutoff)

    def _check_escalation(self, group_id: int, user_id: int) -> Optional[EscalationStepConfig]:
        if not self.config.escalation.enabled: return None
        steps = self.config.escalation.escalation_steps
        if not steps: return None
        for step in steps:
            if self._count_ops_in_window(group_id, user_id, float(step.within_hours)) >= int(step.count):
                return step
        return None

    def _check_warning_threshold(self, group_id: int, user_id: int, violation_type: str) -> tuple[bool, int, int]:
        wc = self.config.warning
        if not wc.enabled: return False, 0, 999
        thresholds = {"spam": (wc.spam_warn_threshold, wc.spam_warn_window), "abuse": (wc.abuse_warn_threshold, wc.abuse_warn_window), "ad": (wc.ad_warn_threshold, wc.ad_warn_window)}
        threshold, window = thresholds.get(violation_type, (3, 600))
        if threshold <= 0: return True, 0, 0
        user_w = self._warnings.get(group_id, {}).get(user_id, {}).get(violation_type, [])
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
        except Exception as e:
            self.ctx.logger.warning(f"[群管理] 通过昵称查找成员失败: group={gid} target={target}: {e}")
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
        if group_id <= 0:
            if deny_mode == "reply":
                await self.ctx.send.text(self.config.prompts.command_denied_message, stream_id)
            return False
        is_owner = False
        role = await self._check_target_role(group_id, sender_id)
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

    @Tool("group_warn_user", description="对指定群成员发出正式警告并记录, violation_type 为 spam(刷屏)/abuse(辱骂)/ad(广告)/sexual(涉黄内容)/illegal(违法内容)", parameters=[
        ToolParameterInfo(name="group_id", param_type=ToolParamType.INTEGER, description="群号", required=True),
        ToolParameterInfo(name="user_id", param_type=ToolParamType.INTEGER, description="用户QQ号", required=True),
        ToolParameterInfo(name="violation_type", param_type=ToolParamType.STRING, description="违规类型: spam/abuse/ad/sexual/illegal", required=True),
        ToolParameterInfo(name="reason", param_type=ToolParamType.STRING, description="警告原因(简要说明违规内容)", required=True),
    ])
    async def tool_warn_user(self, group_id: int = 0, user_id: int = 0, violation_type: str = "", reason: str = "", **kwargs: Any) -> dict[str, Any]:
        stream_id = str(kwargs.get("stream_id", ""))
        del kwargs
        if group_id <= 0:
            return {"name": "group_warn_user", "content": "无效的 group_id"}
        self.ctx.logger.info(f"[群管理] Tool-warn: group={group_id} user={user_id} type={violation_type}")
        async with self._lock:
            await self._check_daily_reset(group_id)
            is_protected, msg = await self._is_protected(group_id, user_id)
            if is_protected: return {"name": "group_warn_user", "content": f"无法警告: {msg}"}
            self._warnings.setdefault(group_id, {}).setdefault(user_id, {}).setdefault(violation_type, []).append((time.time(), 1))
            type_cn = {
                "spam": "刷屏",
                "abuse": "辱骂",
                "ad": "广告",
                "sexual": "涉黄内容",
                "illegal": "违法内容",
            }.get(violation_type, violation_type)
            warn_text = await self._generate_moderation_notice(violation_type, reason)
            warn_text = f"⚠ {warn_text.lstrip('⚠ ').strip()}"
            warn_stream_id = await self._resolve_group_stream_id(group_id, stream_id)
            if warn_stream_id:
                await self.ctx.send.text(warn_text, warn_stream_id)
            else:
                self.ctx.logger.warning(f"[群管理] 无法发送警告消息: group={group_id} user={user_id} reason={reason}")
            over, current, thresh = self._check_warning_threshold(group_id, user_id, violation_type)
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
        if group_id <= 0:
            return {"name": "group_mute_user", "content": "无效的 group_id"}
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
            esc = self._check_escalation(group_id, user_id)
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
            except Exception:
                self._add_log(group_id, "mute", user_id, reason, False)
                self.ctx.logger.error(f"[群管理] Tool-mute 异常: group={group_id} user={user_id}", exc_info=True)
                return {"name": "group_mute_user", "content": "禁言未能生效，请稍后重试"}

    # =========================================================================
    # Tool: group_unmute_user
    # =========================================================================

    @Tool("group_unmute_user", description="解除指定群成员的禁言", parameters=[
        ToolParameterInfo(name="group_id", param_type=ToolParamType.INTEGER, description="群号", required=True),
        ToolParameterInfo(name="user_id", param_type=ToolParamType.INTEGER, description="用户QQ号", required=True),
    ])
    async def tool_unmute_user(self, group_id: int = 0, user_id: int = 0, **kwargs: Any) -> dict[str, Any]:
        del kwargs
        if group_id <= 0:
            return {"name": "group_unmute_user", "content": "无效的 group_id"}
        self.ctx.logger.info(f"[群管理] Tool-unmute: group={group_id} user={user_id}")
        async with self._lock:
            try:
                ok, data = await self._call_api(api_name="adapter.napcat.group.set_group_ban", group_id=self._to_int(group_id), user_id=self._to_int(user_id), duration=0)
                if not ok: return {"name": "group_unmute_user", "content": f"解除禁言未能生效: {data}"}
                return {"name": "group_unmute_user", "content": f"已解除 @{user_id} 的禁言"}
            except Exception:
                self.ctx.logger.error(f"[群管理] Tool-unmute 异常: group={group_id} user={user_id}", exc_info=True)
                return {"name": "group_unmute_user", "content": "解除禁言未能生效，请稍后重试"}

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
        if group_id <= 0:
            return {"name": "group_kick_user", "content": "无效的 group_id"}
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
            esc = self._check_escalation(group_id, user_id)
            if esc and esc.action == "mute":
                return {"name": "group_kick_user", "content": f"处罚阶梯建议先禁言 {esc.max_duration} 秒而非直接踢出，请使用 group_mute_user"}
            sf = self.config.safeguard
            if sf.kick_require_confirm:
                called_time = self._get_member_called.get(group_id, {}).get(user_id, 0)
                if time.time() - called_time > 300:
                    return {"name": "group_kick_user", "content": "踢人前请先调用 group_get_member 确认目标身份"}
            today = self._today_key()
            self._daily_kick_count.setdefault(group_id, {}).setdefault(today, 0)
            if self._daily_kick_count[group_id][today] >= sf.daily_kick_limit: return {"name": "group_kick_user", "content": f"今天已经踢出了 {sf.daily_kick_limit} 个用户，已达每日上限"}
            try:
                ok, data = await self._call_api(api_name="adapter.napcat.group.set_group_kick", group_id=self._to_int(group_id), user_id=self._to_int(user_id), reject_add_request=False)
                if not ok: self._add_log(group_id, "kick", user_id, reason, False); return {"name": "group_kick_user", "content": f"踢出未能生效: {data}"}
                self._daily_kick_count[group_id][today] += 1
                self._add_log(group_id, "kick", user_id, reason, True)
                self._get_member_called[group_id].pop(user_id, None)
                return {"name": "group_kick_user", "content": f"已将 @{user_id} 踢出群聊，原因：{reason}"}
            except Exception:
                self._add_log(group_id, "kick", user_id, reason, False)
                self.ctx.logger.error(f"[群管理] Tool-kick 异常: group={group_id} user={user_id}", exc_info=True)
                return {"name": "group_kick_user", "content": "踢出未能生效，请稍后重试"}

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
        if group_id <= 0:
            return {"name": "group_set_user_card", "content": "无效的 group_id"}
        self.ctx.logger.info(f"[群管理] Tool-card: group={group_id} user={user_id}")
        async with self._lock:
            is_protected, msg = await self._is_protected(group_id, user_id)
            if is_protected: return {"name": "group_set_user_card", "content": f"无法修改群名片: {msg}"}
            try:
                ok, data = await self._call_action_api(api_name="adapter.napcat.group.set_group_card", group_id=self._to_int(group_id), user_id=self._to_int(user_id), card=card)
                if not ok: return {"name": "group_set_user_card", "content": f"修改群名片未能生效: {data}"}
                return {"name": "group_set_user_card", "content": f"已将 @{user_id} 的群名片改为「{card}」"}
            except Exception:
                self.ctx.logger.error(f"[群管理] Tool-card 异常: group={group_id} user={user_id}", exc_info=True)
                return {"name": "group_set_user_card", "content": "修改群名片未能生效，请稍后重试"}

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
        if group_id <= 0:
            return {"name": "group_set_title", "content": "无效的 group_id"}
        self.ctx.logger.info(f"[群管理] Tool-title: group={group_id} user={user_id}")
        async with self._lock:
            is_protected, msg = await self._is_protected(group_id, user_id)
            if is_protected: return {"name": "group_set_title", "content": f"无法设置头衔: {msg}"}
            try:
                ok, data = await self._call_action_api(api_name="adapter.napcat.group.set_group_special_title", group_id=self._to_int(group_id), user_id=self._to_int(user_id), special_title=title)
                if not ok: return {"name": "group_set_title", "content": f"设置头衔未能生效: {data}"}
                return {"name": "group_set_title", "content": f"已将 @{user_id} 的专属头衔设为「{title}」"}
            except Exception:
                self.ctx.logger.error(f"[群管理] Tool-title 异常: group={group_id} user={user_id}", exc_info=True)
                return {"name": "group_set_title", "content": "设置头衔未能生效，请稍后重试"}

    # =========================================================================
    # Tool: group_set_name
    # =========================================================================

    @Tool("group_set_name", description="修改群名称 (仅群主)", parameters=[
        ToolParameterInfo(name="group_id", param_type=ToolParamType.INTEGER, description="群号", required=True),
        ToolParameterInfo(name="name", param_type=ToolParamType.STRING, description="新群名称", required=True),
    ])
    async def tool_set_name(self, group_id: int = 0, name: str = "", **kwargs: Any) -> dict[str, Any]:
        del kwargs
        if group_id <= 0:
            return {"name": "group_set_name", "content": "无效的 group_id"}
        self.ctx.logger.info(f"[群管理] Tool-setname: group={group_id} name={name}")
        async with self._lock:
            try:
                ok, data = await self._call_api(api_name="adapter.napcat.group.set_group_name", group_id=self._to_int(group_id), group_name=name)
                if not ok: return {"name": "group_set_name", "content": f"修改群名未能生效: {data}"}
                return {"name": "group_set_name", "content": f"已将群名改为「{name}」"}
            except Exception:
                self.ctx.logger.error(f"[群管理] Tool-setname 异常: group={group_id}", exc_info=True)
                return {"name": "group_set_name", "content": "修改群名未能生效，请稍后重试"}

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
        if group_id <= 0:
            return {"name": "group_approve_join", "content": "无效的 group_id"}
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
            except Exception:
                self.ctx.logger.error(f"[群管理] Tool-approve 异常: group={group_id}", exc_info=True)
                return {"name": "group_approve_join", "content": "通过申请未能生效，请稍后重试"}

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
        if group_id <= 0:
            return {"name": "group_reject_join", "content": "无效的 group_id"}
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
            except Exception:
                self.ctx.logger.error(f"[群管理] Tool-reject 异常: group={group_id}", exc_info=True)
                return {"name": "group_reject_join", "content": "拒绝申请未能生效，请稍后重试"}

    # =========================================================================
    # Tool: group_post_notice / group_delete_notice / group_set_essence / group_unset_essence / group_recall_msg / group_get_member / group_get_shut_list / group_get_system_msg
    # =========================================================================

    @Tool("group_post_notice", description="发布群公告 (仅群主). 返回 notice_id 供后续删除使用", parameters=[
        ToolParameterInfo(name="group_id", param_type=ToolParamType.INTEGER, description="群号", required=True),
        ToolParameterInfo(name="content", param_type=ToolParamType.STRING, description="公告内容", required=True),
    ])
    async def tool_post_notice(self, group_id: int = 0, content: str = "", **kwargs: Any) -> dict[str, Any]:
        del kwargs
        if group_id <= 0:
            return {"name": "group_post_notice", "content": "无效的 group_id"}
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
            except Exception:
                self.ctx.logger.error(f"[群管理] Tool-notice-post 异常: group={group_id}", exc_info=True)
                return {"name": "group_post_notice", "content": "发布公告未能生效，请稍后重试"}

    @Tool("group_delete_notice", description="删除群公告 (仅群主). 先用 group_get_notice 获取公告列表和 notice_id", parameters=[
        ToolParameterInfo(name="group_id", param_type=ToolParamType.INTEGER, description="群号", required=True),
        ToolParameterInfo(name="notice_id", param_type=ToolParamType.STRING, description="公告ID (来自 group_get_notice 返回值)", required=True),
    ])
    async def tool_delete_notice(self, group_id: int = 0, notice_id: str = "", **kwargs: Any) -> dict[str, Any]:
        del kwargs
        if group_id <= 0:
            return {"name": "group_delete_notice", "content": "无效的 group_id"}
        self.ctx.logger.info(f"[群管理] Tool-notice-del: group={group_id}")
        async with self._lock:
            try:
                ok, data = await self._call_action_api(api_name="adapter.napcat.group.delete_group_notice", group_id=self._to_int(group_id), notice_id=notice_id)
                if not ok: return {"name": "group_delete_notice", "content": f"删除公告未能生效: {data}"}
                return {"name": "group_delete_notice", "content": f"已删除公告 {notice_id}"}
            except Exception:
                self.ctx.logger.error(f"[群管理] Tool-notice-del 异常: group={group_id}", exc_info=True)
                return {"name": "group_delete_notice", "content": "删除公告未能生效，请稍后重试"}

    @Tool("group_set_essence", description="将消息设为群精华。操作流程: 先在群里请用户回复(引用)目标消息, 用户回复后从回复中提取 message_id 调用本工具", parameters=[
        ToolParameterInfo(name="group_id", param_type=ToolParamType.INTEGER, description="群号", required=True),
        ToolParameterInfo(name="message_id", param_type=ToolParamType.STRING, description="消息ID (用户回复目标消息后从回复数据中提取)", required=True),
    ])
    async def tool_set_essence(self, group_id: int = 0, message_id: str = "", **kwargs: Any) -> dict[str, Any]:
        del kwargs
        if group_id <= 0:
            return {"name": "group_set_essence", "content": "无效的 group_id"}
        self.ctx.logger.info(f"[群管理] Tool-essence-set: group={group_id}")
        async with self._lock:
            try:
                ok, data = await self._call_action_api(api_name="adapter.napcat.group.set_essence_msg", group_id=self._to_int(group_id), message_id=message_id)
                if not ok: return {"name": "group_set_essence", "content": f"设为精华未能生效: {data}"}
                return {"name": "group_set_essence", "content": f"已将消息 {message_id} 设为精华"}
            except Exception:
                self.ctx.logger.error(f"[群管理] Tool-essence-set 异常: group={group_id}", exc_info=True)
                return {"name": "group_set_essence", "content": "设为精华未能生效，请稍后重试"}

    @Tool("group_unset_essence", description="取消消息的精华状态。操作流程同 group_set_essence", parameters=[
        ToolParameterInfo(name="group_id", param_type=ToolParamType.INTEGER, description="群号", required=True),
        ToolParameterInfo(name="message_id", param_type=ToolParamType.STRING, description="消息ID (用户回复后提取)", required=True),
    ])
    async def tool_unset_essence(self, group_id: int = 0, message_id: str = "", **kwargs: Any) -> dict[str, Any]:
        del kwargs
        if group_id <= 0:
            return {"name": "group_unset_essence", "content": "无效的 group_id"}
        self.ctx.logger.info(f"[群管理] Tool-essence-del: group={group_id}")
        async with self._lock:
            try:
                ok, data = await self._call_action_api(api_name="adapter.napcat.group.delete_essence_msg", group_id=self._to_int(group_id), message_id=message_id)
                if not ok: return {"name": "group_unset_essence", "content": f"取消精华未能生效: {data}"}
                return {"name": "group_unset_essence", "content": f"已取消消息 {message_id} 的精华"}
            except Exception:
                self.ctx.logger.error(f"[群管理] Tool-essence-del 异常: group={group_id}", exc_info=True)
                return {"name": "group_unset_essence", "content": "取消精华未能生效，请稍后重试"}

    @Tool("group_recall_msg", description="撤回指定消息。操作流程: 先在群里请用户回复(引用)目标消息, 用户回复后从回复中提取 message_id 调用本工具。群主/管理员无2分钟限制", parameters=[
        ToolParameterInfo(name="group_id", param_type=ToolParamType.INTEGER, description="群号", required=True),
        ToolParameterInfo(name="message_id", param_type=ToolParamType.STRING, description="消息ID (用户回复目标消息后从回复数据中提取)", required=True),
        ToolParameterInfo(name="reason", param_type=ToolParamType.STRING, description="撤回原因", required=True),
    ])
    async def tool_recall_msg(self, group_id: int = 0, message_id: str = "", reason: str = "", **kwargs: Any) -> dict[str, Any]:
        del kwargs
        if group_id <= 0:
            return {"name": "group_recall_msg", "content": "无效的 group_id"}
        self.ctx.logger.info(f"[群管理] Tool-recall: group={group_id} mid={message_id}")
        async with self._lock:
            try:
                ok, data = await self._call_api(api_name="adapter.napcat.message.delete_msg", message_id=self._to_int(message_id))
                if not ok: return {"name": "group_recall_msg", "content": f"撤回未能生效: {data}"}
                return {"name": "group_recall_msg", "content": f"已撤回消息 {message_id}: {reason}"}
            except Exception:
                self.ctx.logger.error(f"[群管理] Tool-recall 异常: group={group_id}", exc_info=True)
                return {"name": "group_recall_msg", "content": "撤回未能生效，请稍后重试"}

    @Tool("group_get_member", description="查询群成员的身份(owner/admin/member)、昵称和群名片。踢人/禁言前必须先调用此工具确认目标不是群主/管理员", parameters=[
        ToolParameterInfo(name="group_id", param_type=ToolParamType.INTEGER, description="群号", required=True),
        ToolParameterInfo(name="user_id", param_type=ToolParamType.INTEGER, description="用户QQ号", required=True),
    ])
    async def tool_get_member(self, group_id: int = 0, user_id: int = 0, **kwargs: Any) -> dict[str, Any]:
        del kwargs
        if group_id <= 0:
            return {"name": "group_get_member", "content": "无效的 group_id"}
        self.ctx.logger.info(f"[群管理] Tool-get-member: group={group_id} user={user_id}")
        async with self._lock:
            self._get_member_called.setdefault(group_id, {})[user_id] = time.time()
            try:
                ok, data = await self._call_api(api_name="adapter.napcat.group.get_group_member_info", group_id=self._to_int(group_id), user_id=self._to_int(user_id), no_cache=True)
                if ok and isinstance(data, dict):
                    role = data.get("role", "unknown"); card = data.get("card", ""); nick = data.get("nickname", "")
                    self._known_roles[(group_id, user_id)] = (role, time.time())
                    return {"name": "group_get_member", "content": f"@{user_id}: 昵称={nick}, 群名片={card}, 身份={role}"}
                return {"name": "group_get_member", "content": f"未找到 @{user_id} 的信息"}
            except Exception:
                self.ctx.logger.error(f"[群管理] Tool-get-member 异常: group={group_id} user={user_id}", exc_info=True)
                return {"name": "group_get_member", "content": "查询成员信息未能生效，请稍后重试"}

    @Tool("group_get_shut_list", description="查看当前群的禁言列表", parameters=[
        ToolParameterInfo(name="group_id", param_type=ToolParamType.INTEGER, description="群号", required=True),
    ])
    async def tool_get_shut_list(self, group_id: int = 0, **kwargs: Any) -> dict[str, Any]:
        del kwargs
        if group_id <= 0:
            return {"name": "group_get_shut_list", "content": "无效的 group_id"}
        self.ctx.logger.info(f"[群管理] Tool-get-shutlist: group={group_id}")
        async with self._lock:
            try:
                ok, data = await self._call_action_api(api_name="adapter.napcat.group.get_group_shut_list", group_id=self._to_int(group_id))
                if ok and isinstance(data, dict): return {"name": "group_get_shut_list", "content": f"禁言列表: {data.get('data', data)}"}
                return {"name": "group_get_shut_list", "content": "该群当前没有被禁言的用户"}
            except Exception:
                self.ctx.logger.error(f"[群管理] Tool-get-shutlist 异常: group={group_id}", exc_info=True)
                return {"name": "group_get_shut_list", "content": "查询禁言列表未能生效，请稍后重试"}

    @Tool("group_get_notice", description="获取群公告列表(含 notice_id 和 content), 删除公告前必须先调用此工具获取 notice_id", parameters=[
        ToolParameterInfo(name="group_id", param_type=ToolParamType.INTEGER, description="群号", required=True),
    ])
    async def tool_get_notice(self, group_id: int = 0, **kwargs: Any) -> dict[str, Any]:
        del kwargs
        if group_id <= 0:
            return {"name": "group_get_notice", "content": "无效的 group_id"}
        self.ctx.logger.info(f"[群管理] Tool-get-notice: group={group_id}")
        async with self._lock:
            try:
                ok, data = await self._call_action_api(api_name="adapter.napcat.group.get_group_notice", group_id=self._to_int(group_id))
                if ok and isinstance(data, dict):
                    return {"name": "group_get_notice", "content": f"公告列表: {data.get('data', data)}"}
                return {"name": "group_get_notice", "content": f"获取公告列表未能生效: {data}"}
            except Exception:
                self.ctx.logger.error(f"[群管理] Tool-get-notice 异常: group={group_id}", exc_info=True)
                return {"name": "group_get_notice", "content": "获取公告列表未能生效，请稍后重试"}

    @Tool("group_get_system_msg", description="获取群的系统消息(含入群申请列表)", parameters=[
        ToolParameterInfo(name="group_id", param_type=ToolParamType.INTEGER, description="群号", required=True),
    ])
    async def tool_get_system_msg(self, group_id: int = 0, **kwargs: Any) -> dict[str, Any]:
        del kwargs
        if group_id <= 0:
            return {"name": "group_get_system_msg", "content": "无效的 group_id"}
        self.ctx.logger.info(f"[群管理] Tool-get-sysmsg: group={group_id}")
        async with self._lock:
            try:
                ok, data = await self._call_action_api(api_name="adapter.napcat.group.get_group_system_msg", group_id=self._to_int(group_id))
                now = datetime.now()
                if ok and isinstance(data, dict):
                    items = data.get("data", data)
                    if isinstance(items, dict):
                        items = [items]
                    elif not isinstance(items, list):
                        items = []
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
            except Exception:
                self.ctx.logger.error(f"[群管理] Tool-get-sysmsg 异常: group={group_id}", exc_info=True)
                return {"name": "group_get_system_msg", "content": "获取系统消息未能生效，请稍后重试"}

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
        mute_limit = self.config.safeguard.daily_mute_limit
        kick_limit = self.config.safeguard.daily_kick_limit
        info = (
            f"群 {gid} 管理面板\n"
            f"身份：{role}\n"
            f"状态：{enabled}\n"
            f"今日已禁言 {mute_cnt} 人（上限 {mute_limit}），已踢出 {kick_cnt} 人（上限 {kick_limit}）"
        )
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
            status = "o" if e["success"] else "x"
            ts = e['timestamp'][:16]
            lines.append(f"  [{ts}] {status} {e['action']} @{e['target_user_id']} -- {e['reason']}")
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

    def _clear_runtime_cache(self):
        """清除运行时缓存，用于 /admin reload 时重置与配置相关的缓存。"""
        self._group_roles.clear()
        self._role_refresh_time.clear()
        self._known_roles.clear()
        self._stream_to_group.clear()
        self._disabled_groups.clear()
        self._get_member_called.clear()
        self._last_mute_time.clear()
        self._recent_user_messages.clear()
        for task in list(self._audit_tasks.values()):
            if task and not task.done():
                task.cancel()
        self._audit_tasks.clear()
        self._audit_seen_messages.clear()

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
            self._clear_runtime_cache()
            await self.ctx.send.text("插件配置已刷新（运行时缓存已清空）", stream_id)
        except Exception:
            self.ctx.logger.error("[群管理] 配置刷新异常", exc_info=True)
            await self.ctx.send.text("刷新失败，请查看日志", stream_id)
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
        esc = self._check_escalation(gid, qq)
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
        ok, _ = await self._call_api(api_name="adapter.napcat.group.set_group_ban", group_id=gid, user_id=qq, duration=duration)
        if ok:
            self._last_mute_time[mute_key] = time.time()
            self._daily_mute_count[gid][today] += 1
            dur_min = duration // 60
            dur_str = f"{dur_min}分钟" if dur_min > 0 else f"{duration}秒"
            await self._send_at_text(stream_id, f"已将", qq, f"禁言 {dur_str}" + (f"（{reason}）" if reason else ""))
            self._add_log(gid, "mute", qq, reason or "管理员命令", True)
        else: await self.ctx.send.text("禁言未能生效，请检查权限", stream_id)
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
        ok, _ = await self._call_api(api_name="adapter.napcat.group.set_group_ban", group_id=gid, user_id=qq, duration=0)
        if ok: await self._send_at_text(stream_id, "已解除", qq, "的禁言")
        else: await self.ctx.send.text("解禁未能生效，请检查权限", stream_id)
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
        esc = self._check_escalation(gid, qq)
        if esc and esc.action == "mute":
            await self.ctx.send.text(f"处罚阶梯建议先禁言 {esc.max_duration} 秒而非直接踢出，请使用 /mute", stream_id)
            return True, "", True
        await self._check_daily_reset(gid)
        today = self._today_key()
        self._daily_kick_count.setdefault(gid, {}).setdefault(today, 0)
        if self._daily_kick_count[gid][today] >= self.config.safeguard.daily_kick_limit:
            await self.ctx.send.text(f"今天已经踢出了 {self.config.safeguard.daily_kick_limit} 个用户，已达每日上限", stream_id)
            return True, "", True
        ok, _ = await self._call_api(api_name="adapter.napcat.group.set_group_kick", group_id=gid, user_id=qq, reject_add_request=False)
        if ok:
            await self._send_at_text(stream_id, "已踢出", qq, reason if reason else "")
            self._daily_kick_count[gid][today] += 1
            self._add_log(gid, "kick", qq, reason or "管理员命令", True)
        else: await self.ctx.send.text("踢出未能生效，请检查权限", stream_id)
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
        async with self._lock:
            self._warnings.setdefault(gid, {}).setdefault(qq, {}).setdefault(vtype, []).append((time.time(), 1))
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
        ok, _ = await self._call_action_api(api_name="adapter.napcat.group.set_essence_msg", group_id=gid, message_id=msg_id)
        await self.ctx.send.text("已设为精华消息" if ok else "设精华未能生效，请检查权限", stream_id)
        return True, "", True

    @Command("admin_recall", description="撤回: 回复消息后 /recall", pattern=r"/recall")
    async def cmd_admin_recall(self, stream_id: str = "", **kwargs: Any):
        self.ctx.logger.info(f"[群管理] Cmd-recall: stream={stream_id}")
        gid = self._resolve_group_id(stream_id, kwargs)
        if not await self._check_admin_permission(stream_id, gid, kwargs): return True, "", True
        msg_id = self._get_reply_msg_id(kwargs)
        if not msg_id: await self.ctx.send.text("请先回复目标消息再使用 /recall", stream_id); return True, "", True
        ok, _ = await self._call_api(api_name="adapter.napcat.message.delete_msg", message_id=self._to_int(msg_id))
        await self.ctx.send.text("已撤回" if ok else "撤回未能生效，请检查权限", stream_id)
        return True, "", True

    @Command("admin_shutlist", description="查看禁言列表: /shutlist", pattern=r"/shutlist")
    async def cmd_admin_shutlist(self, stream_id: str = "", **kwargs: Any):
        self.ctx.logger.info(f"[群管理] Cmd-shutlist: stream={stream_id}")
        gid = self._resolve_group_id(stream_id, kwargs)
        if not await self._check_admin_permission(stream_id, gid, kwargs): return True, "", True
        ok, data = await self._call_action_api(api_name="adapter.napcat.group.get_group_shut_list", group_id=gid)
        if ok and isinstance(data, dict): await self.ctx.send.text(f"禁言列表: {data.get('data', data)}", stream_id)
        else: await self.ctx.send.text("查询未能生效，请稍后重试", stream_id)
        return True, "", True

    # =========================================================================
    # EventHandler: auto_moderate_tracker — 映射群号/计数消息/检测@提及 (v1.4: 注入已迁移到 HookHandler)
    # =========================================================================

    @EventHandler("auto_moderate_tracker", description="自动审核追踪: 映射群号、计数消息、检测@提及", event_type=EventType.ON_MESSAGE)
    async def handle_auto_moderate(self, message: Any = None, stream_id: str = "", **kwargs: Any):
        if not self.config.plugin.enabled: return {"continue_processing": True}
        if not self.config.auto_moderate.enabled: return {"continue_processing": True}
        group_id = 0
        if isinstance(message, dict):
            mi = message.get("message_info", {}) or {}
            gi = mi.get("group_info", {}) or {}
            ac = mi.get("additional_config", {}) or {}
            group_id = self._to_int(gi.get("group_id", 0))
            if not group_id:
                mbi = message.get("message_base_info", {}) or {}
                group_id = self._to_int(mbi.get("group_id", 0))
            self_id = ac.get("self_id")
            if self_id and not self._bot_self_id: self._bot_self_id = self._to_int(self_id)
            if group_id:
                if stream_id: self._stream_to_group[stream_id] = group_id
                sid = str(kwargs.get("session_id", ""))
                if sid: self._stream_to_group[sid] = group_id
        if not group_id or not self._is_group_enabled(group_id): return {"continue_processing": True}
        await self._ensure_bot_role(group_id)
        sender_id = self._extract_message_user_id(message, kwargs)
        text = self._extract_message_text(message)
        image_segments = self._extract_image_segments(message)
        forwarded_record_single_message = (
            self.config.auto_moderate.treat_forwarded_records_as_single_message
            and self._is_forwarded_chat_record(message, text)
        )
        if forwarded_record_single_message:
            text, image_segments = await self._expand_forwarded_record_for_audit(message, text, image_segments)
        if self.config.logging.verbose_logging:
            self.ctx.logger.info(
                f"[群管理] 入站消息: group={group_id} user={sender_id} text_len={len(text)} "
                f"images={len(image_segments)} forwarded_record={forwarded_record_single_message} stream={stream_id}"
            )
        bot_id = self._to_int(self.config.identity.bot_qq) or self._bot_self_id or 0
        if sender_id and sender_id != bot_id:
            msg_id = str(message.get("message_id", "")) if isinstance(message, dict) else ""
            self._schedule_llm_moderation(
                group_id,
                sender_id,
                text,
                msg_id,
                stream_id,
                forwarded_record_single_message=forwarded_record_single_message,
            )
            self._schedule_image_moderation(group_id, sender_id, image_segments, msg_id, stream_id)
        if time.time() - self._last_cleanup_time > 3600:
            self._cleanup_memory()
        return {"continue_processing": True}

    # =========================================================================
    # HookHandler: chat.receive.after_process — 缓存 session_id → group_id
    # =========================================================================

    @HookHandler(
        "chat.receive.after_process",
        name="group_admin_session_bind",
        description="在消息处理完成后缓存 session_id → group_id 映射，供后续 before_model_request 注入使用",
        mode=HookMode.BLOCKING,
        order=HookOrder.EARLY,
        error_policy=ErrorPolicy.SKIP,
    )
    async def cache_session_group(self, message: Any = None, **kwargs: Any):
        if not isinstance(message, dict):
            return {"action": "continue"}
        mi = message.get("message_info", {}) or {}
        gi = mi.get("group_info", {}) or {}
        ac = mi.get("additional_config", {}) or {}
        group_id = self._to_int(gi.get("group_id", 0))
        if not group_id:
            mbi = message.get("message_base_info", {}) or {}
            group_id = self._to_int(mbi.get("group_id", 0))
        if group_id <= 0:
            return {"action": "continue"}
        self_id = ac.get("self_id")
        if self_id and not self._bot_self_id:
            self._bot_self_id = self._to_int(self_id)
        msg_id = str(message.get("message_id", ""))
        for key in ("session_id", "stream_id", "chat_id"):
            sid = str(kwargs.get(key, ""))
            if sid:
                self._stream_to_group[sid] = group_id
        if msg_id:
            self._stream_to_group[msg_id] = group_id
        if self.config.plugin.enabled and self.config.auto_moderate.enabled and self._is_group_enabled(group_id):
            sender_id = self._extract_message_user_id(message, kwargs)
            text = self._extract_message_text(message)
            image_segments = self._extract_image_segments(message)
            forwarded_record_single_message = (
                self.config.auto_moderate.treat_forwarded_records_as_single_message
                and self._is_forwarded_chat_record(message, text)
            )
            if forwarded_record_single_message:
                text, image_segments = await self._expand_forwarded_record_for_audit(message, text, image_segments)
            bot_id = self._to_int(self.config.identity.bot_qq) or self._bot_self_id or 0
            stream_for_reply = ""
            for key in ("session_id", "stream_id", "chat_id"):
                sid = str(kwargs.get(key, ""))
                if sid:
                    stream_for_reply = sid
                    break
            if self.config.logging.verbose_logging:
                self.ctx.logger.info(
                    f"[群管理] after_process入站: group={group_id} user={sender_id} "
                    f"text_len={len(text)} images={len(image_segments)} "
                    f"forwarded_record={forwarded_record_single_message} msg_id={msg_id} stream={stream_for_reply}"
                )
            if sender_id and sender_id != bot_id:
                self._schedule_llm_moderation(
                    group_id,
                    sender_id,
                    text,
                    msg_id,
                    stream_for_reply,
                    forwarded_record_single_message=forwarded_record_single_message,
                )
                self._schedule_image_moderation(group_id, sender_id, image_segments, msg_id, stream_for_reply)
        return {"action": "continue"}

    # =========================================================================
    # 注入辅助
    # =========================================================================

    async def _prepare_injection(self, **kwargs: Any) -> tuple[int, str, str] | None:
        """返回 (group_id, role, prompt) 或 None（不应注入时）。

        仅在当前请求来自 enabled_groups 中的群时才注入，
        且使用该群的实际 bot 角色。
        """
        if not self.config.plugin.enabled or not self.config.auto_moderate.enabled:
            return None
        enabled = {int(g) for g in self.config.auto_moderate.enabled_groups if g}
        if not enabled:
            return None
        group_id = 0
        # 从缓存查找（chat.receive.after_process 写入: session_id/stream_id/msg_id → group_id）
        for key in ("reply_message_id", "session_id", "stream_id", "chat_id"):
            sid = str(kwargs.get(key, ""))
            if sid:
                gid = self._stream_to_group.get(sid, 0)
                if gid in enabled:
                    group_id = gid
                    break
        if self.config.logging.verbose_logging:
            self.ctx.logger.info("[群管理] 注入检测: group_id=%s", group_id)
        if group_id <= 0:
            return None
        role = await self._ensure_bot_role(group_id) or "member"
        prompt = self._build_admin_prompt(group_id, role)
        return group_id, role, prompt

    # =========================================================================
    # HookHandler: before_request — 注入 extra_prompt（v1.4）
    # =========================================================================

    @HookHandler(
        "maisaka.replyer.before_request",
        name="group_admin_replyer_prompt",
        description="[v1.4] 向当前启用群的 Replyer extra_prompt 注入管理提示词，让 LLM 回复时具备管理意识。",
        mode=HookMode.BLOCKING,
        order=HookOrder.EARLY,
        error_policy=ErrorPolicy.SKIP,
    )
    async def inject_admin_prompt(self, **kwargs: Any):
        prep = await self._prepare_injection(**kwargs)
        if not prep: return {"action": "continue"}
        group_id, role, prompt = prep
        extra = str(kwargs.get("extra_prompt") or "")
        extra = f"{extra}\n\n{prompt}" if extra else prompt
        self.ctx.logger.debug("[群管理] before_request 注入 extra_prompt: group=%s role=%s", group_id, role)
        return {"action": "continue", "modified_kwargs": {"extra_prompt": extra}}

    # =========================================================================
    # HookHandler: before_model_request — 注入 messages（v1.4）
    # =========================================================================

    @HookHandler(
        "maisaka.replyer.before_model_request",
        name="group_admin_model_prompt",
        description="[v1.4] 向 Planner/Timing Gate/Replyer 的 messages 直注管理提示词，按群精确注入。",
        mode=HookMode.BLOCKING,
        order=HookOrder.EARLY,
        error_policy=ErrorPolicy.SKIP,
    )
    async def inject_admin_model_prompt(self, **kwargs: Any):
        prep = await self._prepare_injection(**kwargs)
        if not prep: return {"action": "continue"}
        group_id, role, prompt = prep
        messages = kwargs.get("messages")
        if not isinstance(messages, list):
            return {"action": "continue"}
        updated: list[dict] = []
        inserted = False
        for item in messages:
            if not isinstance(item, dict):
                updated.append(item)
                continue
            message = dict(item)
            role_name = str(message.get("role") or "").lower()
            content = str(message.get("content") or message.get("content_text") or "")
            if role_name == "system" and not inserted:
                if self.PROMPT_MARKER not in content:
                    content = f"{content.rstrip()}\n\n{prompt}" if content.strip() else prompt
                    message["content"] = content
                    message["content_text"] = content
                inserted = True
            updated.append(message)
        if not inserted:
            updated.insert(0, {"role": "system", "content": prompt, "content_text": prompt})
        self.ctx.logger.debug("[群管理] before_model_request 注入 messages: group=%s role=%s", group_id, role)
        return {"action": "continue", "modified_kwargs": {"messages": updated}}

    # =========================================================================
    # HookHandler: after_response — 守门: 拦截不当管理回复
    # =========================================================================

    @HookHandler(
        "maisaka.replyer.after_response",
        name="group_admin_reply_guard",
        description="守门: 检查 LLM 回复中的不当管理行为（宣称无权限但实际有、编造操作结果等）",
        mode=HookMode.BLOCKING,
        order=HookOrder.LATE,
        error_policy=ErrorPolicy.SKIP,
    )
    async def guard_admin_response(self, **kwargs: Any):
        if not self.config.plugin.enabled or not self.config.auto_moderate.enabled:
            return {"action": "continue"}
        group_id = self._resolve_group_id_from_hook(kwargs)
        if not group_id or not self._is_group_enabled(group_id):
            return {"action": "continue"}
        response_text = ""
        for key in ("response", "reply", "content", "text", "message"):
            val = kwargs.get(key)
            if isinstance(val, str) and val.strip():
                response_text = val
                break
        if not response_text:
            return {"action": "continue"}
        role = self._get_group_role(group_id) or "member"
        if role not in ("owner", "admin"):
            return {"action": "continue"}
        # 检测 Bot(群主/管理员) 错误宣称无权限
        deny_flags = ("我没有权限", "我不能执行", "我无法进行", "我做不到", "权限不足", "无法禁言", "无法踢人", "不能操作")
        if any(flag in response_text for flag in deny_flags):
            if self.config.logging.verbose_logging:
                self.ctx.logger.info(f"[群管理] 守门拦截: Bot(role={role})错误宣称无权限, group={group_id}\n--- 原始回复 ---\n{response_text}\n--- 替换为 ---\n收到，我来处理。")
            else:
                self.ctx.logger.warning(f"[群管理] 守门拦截: Bot(role={role})错误宣称无权限, group={group_id}, text={response_text[:80]}")
            return {"action": "continue", "modified_kwargs": {"response": "收到，我来处理。"}}
        return {"action": "continue"}


def create_plugin() -> GroupAdminPlugin:
    return GroupAdminPlugin()
