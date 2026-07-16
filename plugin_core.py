"""群管理助手 — 核心生命周期、辅助方法与后台任务"""

from __future__ import annotations

import asyncio
import os
import re
import time
from collections import deque
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, ClassVar, Optional

import tomlkit

from maibot_sdk import MaiBotPlugin, PluginConfigBase

from .config_model import (
    EscalationStepConfig,
    GroupAdminConfig,
)


class PluginCore(MaiBotPlugin):
    config_model: ClassVar[type[PluginConfigBase] | None] = GroupAdminConfig

    def __init__(self) -> None:
        super().__init__()
        self._group_roles: dict[int, str] = {}
        self._role_refresh_time: dict[int, float] = {}
        self._known_roles: dict[tuple[int, int], tuple[str, float]] = {}
        self._bot_self_id: Optional[int] = None
        self._stream_to_group: dict[str, int] = {}
        self._message_to_group: dict[str, int] = {}
        self._disabled_groups: set[int] = set()
        self._daily_mute_count: dict[int, dict[str, int]] = {}
        self._daily_kick_count: dict[int, dict[str, int]] = {}
        self._daily_approve_count: dict[int, dict[str, int]] = {}
        self._daily_reject_count: dict[int, dict[str, int]] = {}
        self._warnings: dict[int, dict[int, dict[str, list[tuple[float, int]]]]] = {}
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

    async def on_config_update(self, scope: str, config_data: dict[str, Any], version: str) -> None:
        if scope != "self":
            return
        self.set_plugin_config(config_data)
        self._ensure_op_log_capacity()
        self._stop_auto_check()
        self._start_auto_check()
        if version:
            self.ctx.logger.debug(f"群管理插件配置更新: {version}")

    # ===== 后台任务 =====

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
        if len(self._message_to_group) > 2000:
            keys = list(self._message_to_group.keys())
            for k in keys[:-1000]:
                del self._message_to_group[k]
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

    def _check_escalation(self, group_id: int, user_id: int, pending_count: int = 0) -> Optional[EscalationStepConfig]:
        if not self.config.escalation.enabled: return None
        steps = self.config.escalation.escalation_steps
        if not steps: return None
        matched: Optional[EscalationStepConfig] = None
        for step in steps:
            if self._count_ops_in_window(group_id, user_id, float(step.within_hours)) + max(0, pending_count) >= int(step.count):
                if matched is None or int(step.count) > int(matched.count):
                    matched = step
        return matched

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
        if not gid:
            gid = self._message_to_group.get(stream_id, 0)
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
            members = data.get("data") if isinstance(data, dict) else data
            if ok and isinstance(members, list):
                for m in members:
                    if not isinstance(m, dict): continue
                    nick = str(m.get("nickname", ""))
                    card = str(m.get("card", ""))
                    uid = str(m.get("user_id") or m.get("userId") or m.get("qq") or m.get("uin") or "")
                    if target.lower() in (nick.lower(), card.lower()):
                        return self._to_int(uid)
        except Exception as e:
            self.ctx.logger.warning(f"[群管理] 通过昵称查找成员失败: group={gid} target={target}: {e}")
        await self.ctx.send.text(f"未找到成员: {target} (请使用QQ号)", stream_id)
        return 0

    async def _send_at_text(self, stream_id: str, prefix: str, qq: int, suffix: str = ""):
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

    async def _check_admin_permission(self, stream_id: str, group_id: int, user_id: str | int = "", command_text: str = "") -> bool:
        sender_str = str(self._to_int(user_id)) if user_id else ""
        if not sender_str:
            return False
        admins = self.config.admin.admins
        deny_mode = self.config.admin.deny_response
        if sender_str in admins:
            return True
        if group_id <= 0:
            if deny_mode == "reply":
                await self.ctx.send.text(self.config.prompts.command_denied_message, stream_id)
            return False
        is_owner = False
        role = await self._check_target_role(group_id, self._to_int(sender_str))
        if role == "owner":
            is_owner = True
        if is_owner:
            if not self.config.admin.allow_group_owner:
                pass
            else:
                allowed = self.config.admin.owner_allowed_commands
                if not allowed:
                    return True
                for cmd in allowed:
                    if re.search(r'\b' + re.escape(cmd) + r'\b', command_text):
                        return True
                if deny_mode == "reply":
                    await self.ctx.send.text(self.config.prompts.command_denied_message, stream_id)
                return False
        if deny_mode == "reply":
            await self.ctx.send.text(self.config.prompts.command_denied_message, stream_id)
        return False

    async def _save_exempt_users(self):
        try:
            config_path = os.path.join(Path(__file__).parent, "config.toml")
            with open(config_path, "r", encoding="utf-8") as f:
                config_data = tomlkit.load(f)
            if "safeguard" not in config_data:
                config_data["safeguard"] = tomlkit.table()
            exempt_table = tomlkit.table()
            for gid, users in self.config.safeguard.exempt_users.items():
                arr = tomlkit.array()
                for u in users:
                    arr.append(u)
                exempt_table[gid] = arr
            config_data["safeguard"]["exempt_users"] = exempt_table
            with open(config_path, "w", encoding="utf-8") as f:
                tomlkit.dump(config_data, f)
            self.ctx.logger.info("[群管理] 豁免名单已持久化到 config.toml")
        except Exception as e:
            self.ctx.logger.error(f"[群管理] 持久化豁免名单失败: {e}", exc_info=True)

    async def _save_enabled_groups(self):
        try:
            config_path = os.path.join(Path(__file__).parent, "config.toml")
            with open(config_path, "r", encoding="utf-8") as f:
                config_data = tomlkit.load(f)
            if "auto_moderate" not in config_data:
                config_data["auto_moderate"] = tomlkit.table()
            arr = tomlkit.array()
            for g in self.config.auto_moderate.enabled_groups:
                arr.append(g)
            config_data["auto_moderate"]["enabled_groups"] = arr
            with open(config_path, "w", encoding="utf-8") as f:
                tomlkit.dump(config_data, f)
            self.ctx.logger.info(f"[群管理] enabled_groups 已持久化到 config.toml")
        except Exception as e:
            self.ctx.logger.error(f"[群管理] 持久化 enabled_groups 失败: {e}", exc_info=True)

    def _clear_runtime_cache(self):
        self._group_roles.clear()
        self._role_refresh_time.clear()
        self._known_roles.clear()
        self._stream_to_group.clear()
        self._message_to_group.clear()
        self._disabled_groups.clear()
        self._get_member_called.clear()
        self._last_mute_time.clear()
