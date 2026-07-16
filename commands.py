"""群管理助手 — 15 个管理员命令"""

from __future__ import annotations

import re
import time
from typing import Any

from maibot_sdk import Command


class CommandMixin:
    """15 个管理员命令（8 个 /admin 系列 + 7 个快捷操作）。"""

    def _permission_command_text(self, kwargs: dict[str, Any], fallback: str) -> str:
        for key in ("text", "raw_message", "message_text"):
            value = kwargs.get(key)
            if value:
                return str(value)
        return fallback

    # =========================================================================
    # Command: /admin 系列 (8个)
    # =========================================================================

    @Command("admin_status", description="查看群管理运行状态", pattern=r"^/admin\s+status(?:\s+(?P<group_id>\d+))?")
    async def cmd_admin_status(self, stream_id: str = "", user_id: str = "", matched_groups: dict | None = None, **kwargs: Any):
        self.ctx.logger.info(f"[群管理] Cmd-status: stream={stream_id}")
        matched = matched_groups or {}
        gid = int(matched.get("group_id", 0) or 0) or self._resolve_group_id(stream_id, kwargs)
        command_text = self._permission_command_text(kwargs, "admin status")
        if not await self._check_admin_permission(stream_id, gid, user_id, command_text): return True, "", True
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
    async def cmd_admin_off(self, stream_id: str = "", user_id: str = "", matched_groups: dict | None = None, **kwargs: Any):
        self.ctx.logger.info(f"[群管理] Cmd-off: stream={stream_id}")
        matched = matched_groups or {}
        gid = int(matched.get("group_id", 0) or 0) or self._resolve_group_id(stream_id, kwargs)
        command_text = self._permission_command_text(kwargs, "admin off")
        if not await self._check_admin_permission(stream_id, gid, user_id, command_text): return True, "", True
        enabled_groups = [int(x) for x in self.config.auto_moderate.enabled_groups]
        if gid in enabled_groups:
            self.config.auto_moderate.enabled_groups = [str(g) for g in enabled_groups if g != gid]
            await self._save_enabled_groups()
        self._disabled_groups.add(gid)
        await self._send_persona_text(stream_id, f"已关闭群 {gid} 的自动管理", "command_success", f"已关闭群 {gid} 的自动管理")
        return True, "", True

    @Command("admin_on", description="重新开启指定群的自动管理", pattern=r"^/admin\s+on(?:\s+(?P<group_id>\d+))?")
    async def cmd_admin_on(self, stream_id: str = "", user_id: str = "", matched_groups: dict | None = None, **kwargs: Any):
        self.ctx.logger.info(f"[群管理] Cmd-on: stream={stream_id}")
        matched = matched_groups or {}
        gid = int(matched.get("group_id", 0) or 0) or self._resolve_group_id(stream_id, kwargs)
        enabled_groups = [int(x) for x in self.config.auto_moderate.enabled_groups]
        command_text = self._permission_command_text(kwargs, "admin on")
        if not await self._check_admin_permission(stream_id, gid, user_id, command_text): return True, "", True
        if gid not in enabled_groups:
            self.config.auto_moderate.enabled_groups.append(str(gid))
            enabled_groups.append(gid)
            await self._save_enabled_groups()
        self._disabled_groups.discard(gid)
        await self._send_persona_text(stream_id, f"已恢复群 {gid} 的自动管理", "command_success", f"已恢复群 {gid} 的自动管理")
        return True, "", True

    @Command("admin_undo", description="强制解禁", pattern=r"^/admin\s+undo(?:\s+(?P<group_id>\d+))?\s+@?(?P<qq>\d+)")
    async def cmd_admin_undo(self, stream_id: str = "", user_id: str = "", matched_groups: dict | None = None, **kwargs: Any):
        self.ctx.logger.info(f"[群管理] Cmd-undo: stream={stream_id}")
        matched = matched_groups or {}
        raw_text = str(kwargs.get("text", ""))
        if not isinstance(matched, dict) or not matched.get("qq"):
            m = re.search(r"@?(\d+)", raw_text.split("undo", 1)[-1] if "undo" in raw_text else raw_text)
            if m: matched = {"qq": m.group(1)}
        gid = int(matched.get("group_id", 0) or 0) or self._resolve_group_id(stream_id, kwargs)
        qq = int(matched.get("qq", 0))
        if not qq: await self._send_persona_text(stream_id, "用法: /admin undo [群号] @qq", "usage_hint", "缺少目标QQ，提示 /admin undo 用法"); return True, "", True
        command_text = self._permission_command_text(kwargs, "admin undo")
        if not await self._check_admin_permission(stream_id, gid, user_id, command_text): return True, "", True
        ok, _ = await self._call_api(api_name="adapter.napcat.group.set_group_ban", group_id=gid, user_id=qq, duration=0)
        if not ok:
            await self._send_persona_text(stream_id, "强制解禁未能生效，请检查权限", "command_failure", "强制解禁未能生效，请检查权限")
            return True, "", True
        exempt = self.config.safeguard.exempt_users
        gid_str = str(gid)
        if gid_str in exempt and str(qq) in exempt[gid_str]:
            exempt[gid_str] = [u for u in exempt[gid_str] if u != str(qq)]
            if not exempt[gid_str]: del exempt[gid_str]
        await self._send_persona_at_text(stream_id, "已强制解禁", qq, "，同时移出豁免名单", "command_success", f"已强制解禁 {qq}，同时移出豁免名单")
        return True, "", True

    @Command("admin_log", description="查看操作记录 /admin log [群号] [行数]", pattern=r"^/admin\s+log(?:\s+(?P<group_id>\d+))?(?:\s+(?P<lines>\d+))?")
    async def cmd_admin_log(self, stream_id: str = "", user_id: str = "", matched_groups: dict | None = None, **kwargs: Any):
        self.ctx.logger.info(f"[群管理] Cmd-log: stream={stream_id}")
        matched = matched_groups or {}
        gid = int(matched.get("group_id", 0) or 0)
        n_arg = int(matched.get("lines", 0) or 0)
        n = n_arg if n_arg > 0 else self.config.logging.default_log_lines
        if not gid:
            gid = self._resolve_group_id(stream_id, kwargs)
        command_text = self._permission_command_text(kwargs, "admin log")
        if not await self._check_admin_permission(stream_id, gid, user_id, command_text): return True, "", True
        entries = list(self._op_log)
        if gid: entries = [e for e in entries if e["group_id"] == gid]
        entries = entries[-n:]
        if not entries: await self._send_persona_text(stream_id, "暂无操作记录", "command_success", "暂无操作记录"); return True, "", True
        lines = [f"群 {gid or '全部'} 最近 {len(entries)} 条操作记录:"]
        for e in entries:
            status = "o" if e["success"] else "x"
            ts = e['timestamp'][:16]
            lines.append(f"  [{ts}] {status} {e['action']} @{e['target_user_id']} -- {e['reason']}")
        await self.ctx.send.text("\n".join(lines), stream_id)
        return True, "", True

    @Command("admin_ban", description="添加豁免", pattern=r"^/admin\s+ban(?:\s+(?P<group_id>\d+))?\s+@?(?P<qq>\d+)")
    async def cmd_admin_ban(self, stream_id: str = "", user_id: str = "", matched_groups: dict | None = None, **kwargs: Any):
        self.ctx.logger.info(f"[群管理] Cmd-ban: stream={stream_id}")
        matched = matched_groups or {}
        raw_text = str(kwargs.get("text", ""))
        if not isinstance(matched, dict) or not matched.get("qq"):
            m = re.search(r"@?(\d+)", raw_text.split("ban", 1)[-1] if "ban" in raw_text else raw_text)
            if m: matched = {"qq": m.group(1)}
        gid = int(matched.get("group_id", 0) or 0) or self._resolve_group_id(stream_id, kwargs)
        qq = int(matched.get("qq", 0))
        if not qq: await self._send_persona_text(stream_id, "用法: /admin ban [群号] @qq", "usage_hint", "缺少目标QQ，提示 /admin ban 用法"); return True, "", True
        command_text = self._permission_command_text(kwargs, "admin ban")
        if not await self._check_admin_permission(stream_id, gid, user_id, command_text): return True, "", True
        exempt = self.config.safeguard.exempt_users
        gid_str = str(gid); exempt.setdefault(gid_str, [])
        if str(qq) not in exempt[gid_str]: exempt[gid_str].append(str(qq))
        await self._send_persona_text(stream_id, f"已将 {qq} 添加到群 {gid} 的豁免名单", "command_success", f"已将 {qq} 添加到群 {gid} 的豁免名单")
        return True, "", True

    @Command("admin_unban", description="移除豁免", pattern=r"^/admin\s+unban(?:\s+(?P<group_id>\d+))?\s+@?(?P<qq>\d+)")
    async def cmd_admin_unban(self, stream_id: str = "", user_id: str = "", matched_groups: dict | None = None, **kwargs: Any):
        self.ctx.logger.info(f"[群管理] Cmd-unban: stream={stream_id}")
        matched = matched_groups or {}
        raw_text = str(kwargs.get("text", ""))
        if not isinstance(matched, dict) or not matched.get("qq"):
            m = re.search(r"@?(\d+)", raw_text.split("unban", 1)[-1] if "unban" in raw_text else raw_text)
            if m: matched = {"qq": m.group(1)}
        gid = int(matched.get("group_id", 0) or 0) or self._resolve_group_id(stream_id, kwargs)
        qq = int(matched.get("qq", 0))
        if not qq: await self._send_persona_text(stream_id, "用法: /admin unban [群号] @qq", "usage_hint", "缺少目标QQ，提示 /admin unban 用法"); return True, "", True
        command_text = self._permission_command_text(kwargs, "admin unban")
        if not await self._check_admin_permission(stream_id, gid, user_id, command_text): return True, "", True
        exempt = self.config.safeguard.exempt_users
        gid_str = str(gid)
        if gid_str in exempt and str(qq) in exempt[gid_str]:
            exempt[gid_str] = [u for u in exempt[gid_str] if u != str(qq)]
            if not exempt[gid_str]: del exempt[gid_str]
        await self._send_persona_text(stream_id, f"已将 {qq} 从群 {gid} 的豁免名单移除", "command_success", f"已将 {qq} 从群 {gid} 的豁免名单移除")
        return True, "", True

    @Command("admin_reload", description="热重载配置", pattern=r"^/admin\s+reload")
    async def cmd_admin_reload(self, stream_id: str = "", user_id: str = "", **kwargs: Any):
        self.ctx.logger.info(f"[群管理] Cmd-reload: stream={stream_id}")
        admins = self.config.admin.admins
        if str(sender_id) not in admins:
            if self.config.admin.deny_response == "reply": await self._send_persona_text(stream_id, self.config.prompts.command_denied_message, "permission_denied", "管理员权限不足")
            return True, "", True
        try:
            self.set_plugin_config(self.config.model_dump())
            self._clear_runtime_cache()
            await self._send_persona_text(stream_id, "插件配置已刷新（运行时缓存已清空）", "command_success", "插件配置已刷新，运行时缓存已清空")
        except Exception:
            self.ctx.logger.error("[群管理] 配置刷新异常", exc_info=True)
            await self._send_persona_text(stream_id, "刷新失败，请查看日志", "command_failure", "插件配置刷新失败，请查看日志")
        return True, "", True

    # =========================================================================
    # Command: 管理员快捷操作 (7个)
    # =========================================================================

    @Command("admin_mute", description="管理员禁言: /mute @qq|昵称 N分钟 原因", pattern=r"^/mute\s+@?(?P<target>\S+)\s+(?P<duration>\d+)\s*(?P<unit>分钟|小时|秒|min|h|s)?\s*(?P<reason>.*)?")
    async def cmd_admin_mute(self, stream_id: str = "", user_id: str = "", matched_groups: dict | None = None, **kwargs: Any):
        self.ctx.logger.info(f"[群管理] Cmd-mute: stream={stream_id}")
        matched = matched_groups or {}
        gid = self._resolve_group_id(stream_id, kwargs)
        target = (matched.get("target") or "").strip(); duration = int(matched.get("duration", 0) or 0)
        unit = (matched.get("unit") or "分钟").strip(); reason = (matched.get("reason") or "").strip()
        if not target or duration <= 0: await self._send_persona_text(stream_id, "用法: /mute @qq或昵称 N分钟 [原因]", "usage_hint", "缺少禁言目标或时长，提示 /mute 用法"); return True, "", True
        command_text = self._permission_command_text(kwargs, "mute")
        if not await self._check_admin_permission(stream_id, gid, user_id, command_text): return True, "", True
        qq = await self._resolve_target(gid, target, stream_id)
        if not qq: return True, "", True
        if unit in ("小时", "h"): duration *= 3600
        elif unit in ("秒", "s"): pass
        else: duration *= 60
        is_protected, msg = await self._is_protected(gid, qq)
        if is_protected: await self._send_persona_text(stream_id, f"操作被拦截: {msg}", "command_failure", f"禁言操作被拦截：{msg}"); return True, "", True
        sf = self.config.safeguard
        mute_key = (gid, qq)
        last_mute = self._last_mute_time.get(mute_key, 0)
        if sf.mute_cooldown > 0 and (time.time() - last_mute) < sf.mute_cooldown:
            remain = int(sf.mute_cooldown - (time.time() - last_mute))
            await self._send_persona_text(stream_id, f"该用户 {remain} 秒前刚被禁言，请稍后再试", "command_failure", f"目标用户仍在禁言冷却中，剩余 {remain} 秒")
            return True, "", True
        esc = self._check_escalation(gid, qq)
        if esc and esc.action == "kick":
            await self._send_persona_text(stream_id, f"该用户 {esc.within_hours}h 内已被处罚 {esc.count} 次，建议使用 /kick 踢出", "command_failure", f"目标用户已触发处罚阶梯，建议改用 /kick")
            return True, "", True
        if esc and esc.action == "mute":
            duration = min(duration, esc.max_duration)
        duration = min(duration, sf.max_mute_duration)
        await self._check_daily_reset(gid)
        today = self._today_key()
        self._daily_mute_count.setdefault(gid, {}).setdefault(today, 0)
        if self._daily_mute_count[gid][today] >= sf.daily_mute_limit:
            await self._send_persona_text(stream_id, f"今天已经禁言了 {sf.daily_mute_limit} 个用户，已达每日上限", "command_failure", f"今日禁言已达每日上限 {sf.daily_mute_limit}")
            return True, "", True
        ok, _ = await self._call_api(api_name="adapter.napcat.group.set_group_ban", group_id=gid, user_id=qq, duration=duration)
        if ok:
            self._last_mute_time[mute_key] = time.time()
            self._daily_mute_count[gid][today] += 1
            dur_min = duration // 60
            dur_str = f"{dur_min}分钟" if dur_min > 0 else f"{duration}秒"
            await self._send_persona_at_text(stream_id, f"已将", qq, f"禁言 {dur_str}" + (f"（{reason}）" if reason else ""), "command_success", f"已将 {qq} 禁言 {dur_str}" + (f"，原因：{reason}" if reason else ""))
            self._add_log(gid, "mute", qq, reason or "管理员命令", True)
        else: await self._send_persona_text(stream_id, "禁言未能生效，请检查权限", "command_failure", "禁言未能生效，请检查权限")
        return True, "", True

    @Command("admin_unmute", description="管理员解禁: /unmute @qq|昵称", pattern=r"^/unmute\s+@?(?P<target>\S+)")
    async def cmd_admin_unmute(self, stream_id: str = "", user_id: str = "", matched_groups: dict | None = None, **kwargs: Any):
        self.ctx.logger.info(f"[群管理] Cmd-admin-unmute: stream={stream_id}")
        matched = matched_groups or {}
        gid = self._resolve_group_id(stream_id, kwargs)
        target = (matched.get("target") or "").strip()
        if not target: await self._send_persona_text(stream_id, "用法: /unmute @qq或昵称", "usage_hint", "缺少解禁目标，提示 /unmute 用法"); return True, "", True
        command_text = self._permission_command_text(kwargs, "unmute")
        if not await self._check_admin_permission(stream_id, gid, user_id, command_text): return True, "", True
        qq = await self._resolve_target(gid, target, stream_id)
        if not qq: return True, "", True
        ok, _ = await self._call_api(api_name="adapter.napcat.group.set_group_ban", group_id=gid, user_id=qq, duration=0)
        if ok: await self._send_persona_at_text(stream_id, "已解除", qq, "的禁言", "command_success", f"已解除 {qq} 的禁言")
        else: await self._send_persona_text(stream_id, "解禁未能生效，请检查权限", "command_failure", "解禁未能生效，请检查权限")
        return True, "", True

    @Command("admin_kick", description="管理员踢人: /kick @qq|昵称 原因", pattern=r"^/kick\s+@?(?P<target>\S+)\s*(?P<reason>.*)?")
    async def cmd_admin_kick(self, stream_id: str = "", user_id: str = "", matched_groups: dict | None = None, **kwargs: Any):
        self.ctx.logger.info(f"[群管理] Cmd-kick: stream={stream_id}")
        matched = matched_groups or {}
        gid = self._resolve_group_id(stream_id, kwargs)
        target = (matched.get("target") or "").strip(); reason = (matched.get("reason") or "").strip()
        if not target: await self._send_persona_text(stream_id, "用法: /kick @qq或昵称 [原因]", "usage_hint", "缺少踢出目标，提示 /kick 用法"); return True, "", True
        command_text = self._permission_command_text(kwargs, "kick")
        if not await self._check_admin_permission(stream_id, gid, user_id, command_text): return True, "", True
        qq = await self._resolve_target(gid, target, stream_id)
        if not qq: return True, "", True
        is_protected, msg = await self._is_protected(gid, qq)
        if is_protected: await self._send_persona_text(stream_id, f"操作被拦截: {msg}", "command_failure", f"踢出操作被拦截：{msg}"); return True, "", True
        esc = self._check_escalation(gid, qq)
        if esc and esc.action == "mute":
            await self._send_persona_text(stream_id, f"处罚阶梯建议先禁言 {esc.max_duration} 秒而非直接踢出，请使用 /mute", "command_failure", f"处罚阶梯建议先禁言 {esc.max_duration} 秒，不要直接踢出")
            return True, "", True
        await self._check_daily_reset(gid)
        today = self._today_key()
        self._daily_kick_count.setdefault(gid, {}).setdefault(today, 0)
        if self._daily_kick_count[gid][today] >= self.config.safeguard.daily_kick_limit:
            await self._send_persona_text(stream_id, f"今天已经踢出了 {self.config.safeguard.daily_kick_limit} 个用户，已达每日上限", "command_failure", f"今日踢出已达每日上限 {self.config.safeguard.daily_kick_limit}")
            return True, "", True
        ok, _ = await self._call_api(api_name="adapter.napcat.group.set_group_kick", group_id=gid, user_id=qq, reject_add_request=False)
        if ok:
            await self._send_persona_at_text(stream_id, "已踢出", qq, reason if reason else "", "command_success", f"已踢出 {qq}" + (f"，原因：{reason}" if reason else ""))
            self._daily_kick_count[gid][today] += 1
            self._add_log(gid, "kick", qq, reason or "管理员命令", True)
        else: await self._send_persona_text(stream_id, "踢出未能生效，请检查权限", "command_failure", "踢出未能生效，请检查权限")
        return True, "", True

    @Command("admin_warn", description="管理员警告: /warn @qq|昵称 spam/abuse/ad/sexual/illegal 原因", pattern=r"^/warn\s+@?(?P<target>\S+)\s+(?P<type>spam|abuse|ad|sexual|illegal)\s*(?P<reason>.*)?")
    async def cmd_admin_warn(self, stream_id: str = "", user_id: str = "", matched_groups: dict | None = None, **kwargs: Any):
        self.ctx.logger.info(f"[群管理] Cmd-warn: stream={stream_id}")
        matched = matched_groups or {}
        gid = self._resolve_group_id(stream_id, kwargs)
        target = (matched.get("target") or "").strip(); vtype = (matched.get("type") or "").strip(); reason = (matched.get("reason") or "").strip()
        if not target or not vtype: await self._send_persona_text(stream_id, "用法: /warn @qq或昵称 spam/abuse/ad/sexual/illegal [原因]", "usage_hint", "缺少警告目标或类型，提示 /warn 用法"); return True, "", True
        command_text = self._permission_command_text(kwargs, "warn")
        if not await self._check_admin_permission(stream_id, gid, user_id, command_text): return True, "", True
        qq = await self._resolve_target(gid, target, stream_id)
        if not qq: return True, "", True
        async with self._lock:
            self._warnings.setdefault(gid, {}).setdefault(qq, {}).setdefault(vtype, []).append((time.time(), 1))
        self._add_log(gid, "warn", qq, reason or "管理员命令", True)
        type_cn = {"spam": "刷屏", "abuse": "辱骂", "ad": "广告", "sexual": "色情低俗", "illegal": "违法风险"}.get(vtype, vtype)
        await self._send_persona_at_text(stream_id, f"已提醒", qq, f"[{type_cn}]" + (f"（{reason}）" if reason else ""), "command_success", f"已提醒 {qq}，类型：{type_cn}" + (f"，原因：{reason}" if reason else ""))
        return True, "", True

    def _get_reply_msg_id(self, kwargs: dict) -> str:
        for key in ("reply_message_id", "msg_id", "target_msg_id"):
            val = kwargs.get(key)
            if val: return str(val)
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

    @Command("admin_essence", description="设精华: 回复消息后 /essence", pattern=r"^/essence$")
    async def cmd_admin_essence(self, stream_id: str = "", user_id: str = "", **kwargs: Any):
        self.ctx.logger.info(f"[群管理] Cmd-essence: stream={stream_id}")
        gid = self._resolve_group_id(stream_id, kwargs)
        command_text = self._permission_command_text(kwargs, "essence")
        if not await self._check_admin_permission(stream_id, gid, user_id, command_text): return True, "", True
        msg_id = self._get_reply_msg_id(kwargs)
        if not msg_id: await self._send_persona_text(stream_id, "请先回复目标消息再使用 /essence", "usage_hint", "缺少回复目标消息，提示先回复目标消息再使用 /essence"); return True, "", True
        ok, _ = await self._call_action_api(api_name="adapter.napcat.group.set_essence_msg", group_id=gid, message_id=msg_id)
        await self._send_persona_text(stream_id, "已设为精华消息" if ok else "设精华未能生效，请检查权限", "command_success" if ok else "command_failure", "已设为精华消息" if ok else "设精华未能生效，请检查权限")
        return True, "", True

    @Command("admin_recall", description="撤回: 回复消息后 /recall", pattern=r"^/recall$")
    async def cmd_admin_recall(self, stream_id: str = "", user_id: str = "", **kwargs: Any):
        self.ctx.logger.info(f"[群管理] Cmd-recall: stream={stream_id}")
        gid = self._resolve_group_id(stream_id, kwargs)
        command_text = self._permission_command_text(kwargs, "recall")
        if not await self._check_admin_permission(stream_id, gid, user_id, command_text): return True, "", True
        msg_id = self._get_reply_msg_id(kwargs)
        if not msg_id: await self._send_persona_text(stream_id, "请先回复目标消息再使用 /recall", "usage_hint", "缺少回复目标消息，提示先回复目标消息再使用 /recall"); return True, "", True
        ok, _ = await self._call_api(api_name="adapter.napcat.message.delete_msg", message_id=self._to_int(msg_id))
        await self._send_persona_text(stream_id, "已撤回" if ok else "撤回未能生效，请检查权限", "command_success" if ok else "command_failure", "已撤回目标消息" if ok else "撤回未能生效，请检查权限")
        return True, "", True

    @Command("admin_shutlist", description="查看禁言列表: /shutlist", pattern=r"^/shutlist$")
    async def cmd_admin_shutlist(self, stream_id: str = "", user_id: str = "", **kwargs: Any):
        self.ctx.logger.info(f"[群管理] Cmd-shutlist: stream={stream_id}")
        gid = self._resolve_group_id(stream_id, kwargs)
        command_text = self._permission_command_text(kwargs, "shutlist")
        if not await self._check_admin_permission(stream_id, gid, user_id, command_text): return True, "", True
        ok, data = await self._call_action_api(api_name="adapter.napcat.group.get_group_shut_list", group_id=gid)
        if ok and isinstance(data, dict): await self.ctx.send.text(f"禁言列表: {data.get('data', data)}", stream_id)
        else: await self._send_persona_text(stream_id, "查询未能生效，请稍后重试", "command_failure", "查询禁言列表未能生效，请稍后重试")
        return True, "", True
