"""群管理助手 — 18 个 LLM Tool"""

from __future__ import annotations

import time
from datetime import datetime
from typing import Any

from maibot_sdk import Tool
from maibot_sdk.types import ToolParameterInfo, ToolParamType


class ToolMixin:
    """18 个群管理 Tool。"""

    # =========================================================================
    # Tool: group_warn_user
    # =========================================================================

    @Tool("group_warn_user", description="向群成员发出正式警告并记录违规类型(spam/abuse/ad)", parameters=[
        ToolParameterInfo(name="group_id", param_type=ToolParamType.INTEGER, description="群号", required=True),
        ToolParameterInfo(name="user_id", param_type=ToolParamType.INTEGER, description="用户QQ号", required=True),
        ToolParameterInfo(name="violation_type", param_type=ToolParamType.STRING, description="违规类型: spam(刷屏)/abuse(辱骂)/ad(广告)", required=True),
        ToolParameterInfo(name="reason", param_type=ToolParamType.STRING, description="警告原因", required=True),
    ])
    async def tool_warn_user(self, group_id: int = 0, user_id: int = 0, violation_type: str = "", reason: str = "", **kwargs: Any) -> dict[str, Any]:
        stream_id = str(kwargs.get("stream_id", ""))
        del kwargs
        group_id = self._to_int(group_id)
        user_id = self._to_int(user_id)
        if group_id <= 0 or user_id <= 0:
            return {"name": "group_warn_user", "content": "无效的 group_id 或 user_id"}
        self.ctx.logger.info(f"[群管理] Tool-warn: group={group_id} user={user_id} type={violation_type}")
        async with self._lock:
            await self._check_daily_reset(group_id)
            is_protected, msg = await self._is_protected(group_id, user_id)
            if is_protected: return {"name": "group_warn_user", "content": f"无法警告: {msg}"}
            self._warnings.setdefault(group_id, {}).setdefault(user_id, {}).setdefault(violation_type, []).append((time.time(), 1))
            type_cn = {"spam": "刷屏", "abuse": "辱骂", "ad": "广告"}.get(violation_type, violation_type)
            warn_text = f"⚠ 提醒: {reason}"
            await self.ctx.send.text(warn_text, stream_id if stream_id else str(group_id))
            over, current, thresh = self._check_warning_threshold(group_id, user_id, violation_type)
            self._add_log(group_id, "warn", user_id, reason, True)
            extra = f"\n该用户 {type_cn} 类提醒已达 {current}/{thresh}，请注意是否需要升级处理。" if over else ""
            return {"name": "group_warn_user", "content": f"已向 {user_id} 发出正式提醒（{type_cn}），原因：{reason}{extra}"}

    # =========================================================================
    # Tool: group_mute_user
    # =========================================================================

    @Tool("group_mute_user", description="禁言指定群成员（管理员/群主可用）", parameters=[
        ToolParameterInfo(name="group_id", param_type=ToolParamType.INTEGER, description="群号", required=True),
        ToolParameterInfo(name="user_id", param_type=ToolParamType.INTEGER, description="用户QQ号", required=True),
        ToolParameterInfo(name="duration", param_type=ToolParamType.INTEGER, description="禁言秒数(600=10分钟, 3600=1小时)", required=True),
        ToolParameterInfo(name="reason", param_type=ToolParamType.STRING, description="禁言原因", required=True),
    ])
    async def tool_mute_user(self, group_id: int = 0, user_id: int = 0, duration: int = 0, reason: str = "", **kwargs: Any) -> dict[str, Any]:
        del kwargs
        group_id = self._to_int(group_id)
        user_id = self._to_int(user_id)
        duration = self._to_int(duration)
        if group_id <= 0 or user_id <= 0:
            return {"name": "group_mute_user", "content": "无效的 group_id 或 user_id"}
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

    @Tool("group_unmute_user", description="解除指定群成员的禁言（管理员/群主可用）", parameters=[
        ToolParameterInfo(name="group_id", param_type=ToolParamType.INTEGER, description="群号", required=True),
        ToolParameterInfo(name="user_id", param_type=ToolParamType.INTEGER, description="用户QQ号", required=True),
    ])
    async def tool_unmute_user(self, group_id: int = 0, user_id: int = 0, **kwargs: Any) -> dict[str, Any]:
        del kwargs
        group_id = self._to_int(group_id)
        user_id = self._to_int(user_id)
        if group_id <= 0 or user_id <= 0:
            return {"name": "group_unmute_user", "content": "无效的 group_id 或 user_id"}
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

    @Tool("group_kick_user", description="踢出指定群成员（群主可直接踢；管理员需先征求群主同意后使用），踢人前先调 group_get_member 确认身份", parameters=[
        ToolParameterInfo(name="group_id", param_type=ToolParamType.INTEGER, description="群号", required=True),
        ToolParameterInfo(name="user_id", param_type=ToolParamType.INTEGER, description="用户QQ号", required=True),
        ToolParameterInfo(name="reason", param_type=ToolParamType.STRING, description="踢出原因", required=True),
    ])
    async def tool_kick_user(self, group_id: int = 0, user_id: int = 0, reason: str = "", **kwargs: Any) -> dict[str, Any]:
        del kwargs
        group_id = self._to_int(group_id)
        user_id = self._to_int(user_id)
        if group_id <= 0 or user_id <= 0:
            return {"name": "group_kick_user", "content": "无效的 group_id 或 user_id"}
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
        group_id = self._to_int(group_id)
        user_id = self._to_int(user_id)
        if group_id <= 0 or user_id <= 0:
            return {"name": "group_set_user_card", "content": "无效的 group_id 或 user_id"}
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

    @Tool("group_set_title", description="设置群成员的专属头衔（仅群主，最长6字符）", parameters=[
        ToolParameterInfo(name="group_id", param_type=ToolParamType.INTEGER, description="群号", required=True),
        ToolParameterInfo(name="user_id", param_type=ToolParamType.INTEGER, description="用户QQ号", required=True),
        ToolParameterInfo(name="title", param_type=ToolParamType.STRING, description="专属头衔(最长6字符)", required=True),
    ])
    async def tool_set_title(self, group_id: int = 0, user_id: int = 0, title: str = "", **kwargs: Any) -> dict[str, Any]:
        del kwargs
        group_id = self._to_int(group_id)
        user_id = self._to_int(user_id)
        if group_id <= 0 or user_id <= 0:
            return {"name": "group_set_title", "content": "无效的 group_id 或 user_id"}
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

    @Tool("group_set_name", description="修改群名称（仅群主）", parameters=[
        ToolParameterInfo(name="group_id", param_type=ToolParamType.INTEGER, description="群号", required=True),
        ToolParameterInfo(name="name", param_type=ToolParamType.STRING, description="新群名称", required=True),
    ])
    async def tool_set_name(self, group_id: int = 0, name: str = "", **kwargs: Any) -> dict[str, Any]:
        del kwargs
        group_id = self._to_int(group_id)
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

    @Tool("group_approve_join", description="通过入群申请，request_id 从 group_get_system_msg 获取", parameters=[
        ToolParameterInfo(name="group_id", param_type=ToolParamType.INTEGER, description="群号", required=True),
        ToolParameterInfo(name="request_id", param_type=ToolParamType.STRING, description="申请ID (来自 group_get_system_msg)", required=True),
        ToolParameterInfo(name="reason", param_type=ToolParamType.STRING, description="通过原因(可选)", required=False),
    ])
    async def tool_approve_join(self, group_id: int = 0, request_id: str = "", reason: str = "", **kwargs: Any) -> dict[str, Any]:
        del kwargs
        group_id = self._to_int(group_id)
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

    @Tool("group_reject_join", description="拒绝入群申请，request_id 从 group_get_system_msg 获取", parameters=[
        ToolParameterInfo(name="group_id", param_type=ToolParamType.INTEGER, description="群号", required=True),
        ToolParameterInfo(name="request_id", param_type=ToolParamType.STRING, description="申请ID (来自 group_get_system_msg)", required=True),
        ToolParameterInfo(name="reason", param_type=ToolParamType.STRING, description="拒绝原因", required=True),
    ])
    async def tool_reject_join(self, group_id: int = 0, request_id: str = "", reason: str = "", **kwargs: Any) -> dict[str, Any]:
        del kwargs
        group_id = self._to_int(group_id)
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
    # Tool: group_post_notice / group_delete_notice / group_set_essence /
    #       group_unset_essence / group_recall_msg / group_get_member /
    #       group_get_shut_list / group_get_notice / group_get_system_msg
    # =========================================================================

    @Tool("group_post_notice", description="发布群公告（仅群主），返回 notice_id 供后续删除", parameters=[
        ToolParameterInfo(name="group_id", param_type=ToolParamType.INTEGER, description="群号", required=True),
        ToolParameterInfo(name="content", param_type=ToolParamType.STRING, description="公告内容", required=True),
    ])
    async def tool_post_notice(self, group_id: int = 0, content: str = "", **kwargs: Any) -> dict[str, Any]:
        del kwargs
        group_id = self._to_int(group_id)
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

    @Tool("group_delete_notice", description="删除群公告（仅群主），notice_id 从 group_get_notice 获取", parameters=[
        ToolParameterInfo(name="group_id", param_type=ToolParamType.INTEGER, description="群号", required=True),
        ToolParameterInfo(name="notice_id", param_type=ToolParamType.STRING, description="公告ID (来自 group_get_notice)", required=True),
    ])
    async def tool_delete_notice(self, group_id: int = 0, notice_id: str = "", **kwargs: Any) -> dict[str, Any]:
        del kwargs
        group_id = self._to_int(group_id)
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

    @Tool("group_set_essence", description="将消息设为群精华，需用户回复目标消息后获取 message_id", parameters=[
        ToolParameterInfo(name="group_id", param_type=ToolParamType.INTEGER, description="群号", required=True),
        ToolParameterInfo(name="message_id", param_type=ToolParamType.STRING, description="消息ID (用户回复目标消息后提取)", required=True),
    ])
    async def tool_set_essence(self, group_id: int = 0, message_id: str = "", **kwargs: Any) -> dict[str, Any]:
        del kwargs
        group_id = self._to_int(group_id)
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

    @Tool("group_unset_essence", description="取消消息的精华状态，需用户回复目标消息后获取 message_id", parameters=[
        ToolParameterInfo(name="group_id", param_type=ToolParamType.INTEGER, description="群号", required=True),
        ToolParameterInfo(name="message_id", param_type=ToolParamType.STRING, description="消息ID (用户回复目标消息后提取)", required=True),
    ])
    async def tool_unset_essence(self, group_id: int = 0, message_id: str = "", **kwargs: Any) -> dict[str, Any]:
        del kwargs
        group_id = self._to_int(group_id)
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

    @Tool("group_recall_msg", description="撤回指定消息（管理员/群主可用），需用户回复目标消息后获取 message_id", parameters=[
        ToolParameterInfo(name="group_id", param_type=ToolParamType.INTEGER, description="群号", required=True),
        ToolParameterInfo(name="message_id", param_type=ToolParamType.STRING, description="消息ID (用户回复目标消息后提取)", required=True),
        ToolParameterInfo(name="reason", param_type=ToolParamType.STRING, description="撤回原因", required=True),
    ])
    async def tool_recall_msg(self, group_id: int = 0, message_id: str = "", reason: str = "", **kwargs: Any) -> dict[str, Any]:
        del kwargs
        group_id = self._to_int(group_id)
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

    @Tool("group_get_member", description="查询群成员的身份、昵称和群名片，操作前先调此工具确认目标身份", parameters=[
        ToolParameterInfo(name="group_id", param_type=ToolParamType.INTEGER, description="群号", required=True),
        ToolParameterInfo(name="user_id", param_type=ToolParamType.INTEGER, description="用户QQ号", required=True),
    ])
    async def tool_get_member(self, group_id: int = 0, user_id: int = 0, **kwargs: Any) -> dict[str, Any]:
        del kwargs
        group_id = self._to_int(group_id)
        user_id = self._to_int(user_id)
        if group_id <= 0 or user_id <= 0:
            return {"name": "group_get_member", "content": "无效的 group_id 或 user_id"}
        self.ctx.logger.info(f"[群管理] Tool-get-member: group={group_id} user={user_id}")
        async with self._lock:
            self._get_member_called.setdefault(group_id, {})[user_id] = time.time()
            try:
                ok, data = await self._call_api(api_name="adapter.napcat.group.get_group_member_info", group_id=group_id, user_id=user_id, no_cache=True)
                if ok and isinstance(data, dict):
                    role = data.get("role", "unknown"); card = data.get("card", ""); nick = data.get("nickname", "")
                    self._known_roles[(group_id, user_id)] = (role, time.time())
                    return {"name": "group_get_member", "content": f"@{user_id}: 昵称={nick}, 群名片={card}, 身份={role}"}
                return {"name": "group_get_member", "content": f"未找到 @{user_id} 的信息"}
            except Exception:
                self.ctx.logger.error(f"[群管理] Tool-get-member 异常: group={group_id} user={user_id}", exc_info=True)
                return {"name": "group_get_member", "content": "查询成员信息未能生效，请稍后重试"}

    @Tool("group_get_shut_list", description="查看当前群被禁言的成员列表", parameters=[
        ToolParameterInfo(name="group_id", param_type=ToolParamType.INTEGER, description="群号", required=True),
    ])
    async def tool_get_shut_list(self, group_id: int = 0, **kwargs: Any) -> dict[str, Any]:
        del kwargs
        group_id = self._to_int(group_id)
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

    @Tool("group_get_notice", description="获取群公告列表（含 notice_id），删除公告前先调此工具", parameters=[
        ToolParameterInfo(name="group_id", param_type=ToolParamType.INTEGER, description="群号", required=True),
    ])
    async def tool_get_notice(self, group_id: int = 0, **kwargs: Any) -> dict[str, Any]:
        del kwargs
        group_id = self._to_int(group_id)
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
        group_id = self._to_int(group_id)
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
