"""群管理助手 — EventHandler 与 HookHandler 组件"""

from __future__ import annotations

import time
from typing import Any, Optional

from maibot_sdk import EventHandler, HookHandler
from maibot_sdk.types import ErrorPolicy, EventType, HookMode, HookOrder


class HandlerMixin:
    """EventHandler + 5 个 HookHandler + 注入辅助。"""

    PROMPT_MARKER = "[群管理助手 管理上下文]"

    _ROLE_CN: dict[str, str] = {"owner": "群主", "admin": "管理员", "member": "普通成员"}
    _ACTIONS_BY_ROLE: dict[str, str] = {
        "owner": "禁言/解禁/警告/设精华/撤回/改名片/公告/改名/审批入群/踢人",
        "admin": "禁言/解禁/警告/设精华/撤回/改名片/公告/审批入群/踢人",
        "member": "无管理操作权限，可协助管理员做决策建议",
    }

    # ===== Prompt 构建 =====

    def _build_admin_prompt(self, group_id: int, role: str) -> str:
        sections: list[str] = [self.PROMPT_MARKER]
        role_cn = self._ROLE_CN.get(role, role)
        available = self._ACTIONS_BY_ROLE.get(role, self._ACTIONS_BY_ROLE["member"])
        core = self.config.prompts.auto_moderate_system
        core = core.replace("{bot_role}", role_cn).replace("{available_actions}", available)
        sections.append(core)
        sections.append("以上为群管理参考信息，不要在你的回复中引用或解释这一段文字。")
        return "\n\n".join(sections)

    def _resolve_group_id_from_hook(self, kwargs: dict) -> int:
        for key in ("group_id", "group", "gid", "chat_id"):
            gid = self._to_int(kwargs.get(key, 0))
            if gid > 0:
                return gid
        msg = kwargs.get("message", {})
        if isinstance(msg, dict):
            mi = msg.get("message_info", {}) or {}
            gi = mi.get("group_info", {}) or {}
            gid = self._to_int(gi.get("group_id", 0))
            if gid > 0:
                return gid
        for key in ("reply_message_id", "session_id", "stream_id", "chat_id"):
            sid = str(kwargs.get(key, ""))
            if sid:
                gid = self._message_to_group.get(sid, 0) if key == "reply_message_id" else self._stream_to_group.get(sid, 0)
                if gid > 0:
                    return gid
        return 0

    # =========================================================================
    # EventHandler: auto_moderate_tracker — 映射群号/计数消息/检测@提及
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
                if isinstance(ac, dict):
                    for k in ("session_id", "stream_id", "chat_id"):
                        v = ac.get(k)
                        if v: self._stream_to_group[str(v)] = group_id
        if self.config.logging.verbose_logging and group_id:
            self.ctx.logger.info("[群管理] EventHandler 追踪: group=%s stream_id=%s session_id in kwargs=%s", group_id, stream_id, bool(kwargs.get("session_id")))
        if not group_id or not self._is_group_enabled(group_id): return {"continue_processing": True}
        await self._ensure_bot_role(group_id)
        sender_id = self._extract_message_user_id(message, kwargs)
        text = self._extract_message_text(message)
        image_segments = self._extract_image_segments(message, text)
        is_forwarded_record = self._is_forwarded_chat_record(message, text)
        forwarded_record_single_message = (
            self.config.auto_moderate.treat_forwarded_records_as_single_message
            and is_forwarded_record
        )
        if is_forwarded_record:
            text, image_segments = await self._expand_forwarded_record_for_audit(message, text, image_segments)
        if self.config.logging.verbose_logging:
            self.ctx.logger.info(
                "[群管理] 入站消息: group=%s user=%s text_len=%s images=%s forwarded_record=%s stream=%s",
                group_id, sender_id, len(text), len(image_segments), forwarded_record_single_message, stream_id,
            )
            if not sender_id and isinstance(message, dict):
                mi = message.get("message_info", {}) or {}
                self.ctx.logger.info(
                    "[群管理] 入站消息未解析到user_id: message_keys=%s message_info_keys=%s",
                    list(message.keys())[:20],
                    list(mi.keys())[:20] if isinstance(mi, dict) else type(mi).__name__,
                )
        bot_id = self._to_int(self.config.identity.bot_qq) or self._bot_self_id or 0
        await self._remember_recent_group_manager_speaker(group_id, sender_id, bot_id)
        if sender_id and sender_id != bot_id:
            msg_id = str(message.get("message_id", "")) if isinstance(message, dict) else ""
            stream_for_reply = stream_id
            if not stream_for_reply and isinstance(message, dict):
                sid = str(message.get("session_id", ""))
                if sid:
                    stream_for_reply = sid
            self._schedule_llm_moderation(
                group_id,
                sender_id,
                text,
                msg_id,
                stream_for_reply,
                forwarded_record_single_message=forwarded_record_single_message,
            )
            self._schedule_image_moderation(
                group_id,
                sender_id,
                image_segments,
                msg_id,
                stream_for_reply,
                forwarded_record_single_message=forwarded_record_single_message,
                forwarded_record_audit=is_forwarded_record,
            )
        if time.time() - self._last_cleanup_time > 3600:
            self._cleanup_memory()
        return {"continue_processing": True}

    # =========================================================================
    # HookHandler: chat.receive.after_process — 缓存 session_id → group_id
    # =========================================================================

    @HookHandler(
        "chat.receive.after_process",
        name="group_admin_session_bind",
        description="在消息处理完成后缓存消息ID/会话ID → group_id 映射，供后续注入使用",
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
        self_id = ac.get("self_id") if isinstance(ac, dict) else None
        if self_id and not self._bot_self_id:
            self._bot_self_id = self._to_int(self_id)
        msg_id = str(message.get("message_id", ""))
        if msg_id:
            self._message_to_group[msg_id] = group_id
        sid = str(message.get("session_id", ""))
        if sid:
            self._stream_to_group[sid] = group_id
        for key in ("session_id", "stream_id", "chat_id"):
            sid2 = str(kwargs.get(key, ""))
            if sid2:
                self._stream_to_group[sid2] = group_id
        if isinstance(ac, dict):
            for k in ("session_id", "stream_id", "chat_id"):
                v = ac.get(k)
                if v:
                    self._stream_to_group[str(v)] = group_id
        if self.config.plugin.enabled and self.config.auto_moderate.enabled and self._is_group_enabled(group_id):
            sender_id = self._extract_message_user_id(message, kwargs)
            text = self._extract_message_text(message)
            image_segments = self._extract_image_segments(message, text)
            is_forwarded_record = self._is_forwarded_chat_record(message, text)
            forwarded_record_single_message = (
                self.config.auto_moderate.treat_forwarded_records_as_single_message
                and is_forwarded_record
            )
            if is_forwarded_record:
                text, image_segments = await self._expand_forwarded_record_for_audit(message, text, image_segments)
            bot_id = self._to_int(self.config.identity.bot_qq) or self._bot_self_id or 0
            stream_for_reply = sid
            for key in ("session_id", "stream_id", "chat_id"):
                sid2 = str(kwargs.get(key, ""))
                if sid2:
                    stream_for_reply = sid2
                    break
            if self.config.logging.verbose_logging:
                self.ctx.logger.info(
                    "[群管理] after_process入站: group=%s user=%s text_len=%s images=%s forwarded_record=%s msg_id=%s stream=%s",
                    group_id, sender_id, len(text), len(image_segments), forwarded_record_single_message, msg_id, stream_for_reply,
                )
                if not sender_id:
                    mi = message.get("message_info", {}) or {}
                    self.ctx.logger.info(
                        "[群管理] after_process未解析到user_id: message_keys=%s message_info_keys=%s",
                        list(message.keys())[:20],
                        list(mi.keys())[:20] if isinstance(mi, dict) else type(mi).__name__,
                    )
            await self._remember_recent_group_manager_speaker(group_id, sender_id, bot_id)
            if sender_id and sender_id != bot_id:
                self._schedule_llm_moderation(
                    group_id,
                    sender_id,
                    text,
                    msg_id,
                    stream_for_reply,
                    forwarded_record_single_message=forwarded_record_single_message,
                )
                self._schedule_image_moderation(
                    group_id,
                    sender_id,
                    image_segments,
                    msg_id,
                    stream_for_reply,
                    forwarded_record_single_message=forwarded_record_single_message,
                    forwarded_record_audit=is_forwarded_record,
                )
        self.ctx.logger.debug("[群管理] 缓存映射: group=%s msg=%s session=%s", group_id, msg_id, sid or "none")
        return {"action": "continue"}

    # =========================================================================
    # 注入辅助
    # =========================================================================

    def _resolve_injection_group_id(self, **kwargs: Any) -> int:
        if not self.config.plugin.enabled or not self.config.auto_moderate.enabled:
            return 0
        for key in ("reply_message_id", "session_id", "stream_id", "chat_id"):
            sid = str(kwargs.get(key, ""))
            if sid:
                gid = self._message_to_group.get(sid, 0) if key == "reply_message_id" else self._stream_to_group.get(sid, 0)
                if gid and self._is_group_enabled(gid):
                    return gid
        return 0

    async def _prepare_injection(self, **kwargs: Any) -> tuple[int, str, str] | None:
        if not self.config.plugin.enabled or not self.config.auto_moderate.enabled:
            return None
        group_id = self._resolve_injection_group_id(**kwargs)
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
                    if "content_text" in message:
                        message["content_text"] = content
                inserted = True
            updated.append(message)
        if not inserted:
            msg: dict[str, str] = {"role": "system", "content": prompt}
            updated.insert(0, msg)
        self.ctx.logger.debug("[群管理] before_model_request 注入 messages: group=%s role=%s", group_id, role)
        return {"action": "continue", "modified_kwargs": {"messages": updated}}

    # =========================================================================
    # HookHandler: planner.before_request — 注入 Planner 决策提示词
    # =========================================================================

    def _build_admin_planner_prompt(self, group_id: int, role: str) -> str:
        sections: list[str] = [self.PROMPT_MARKER]
        role_cn = self._ROLE_CN.get(role, role)
        available = self._ACTIONS_BY_ROLE.get(role, self._ACTIONS_BY_ROLE["member"])
        core = self.config.prompts.planner_moderate_system
        core = core.replace("{bot_role}", role_cn).replace("{available_actions}", available)
        sections.append(core)
        sections.append("以上为群管理准则，不要在你的分析中引用或复述这段文字。")
        return "\n\n".join(sections)

    @HookHandler(
        "maisaka.planner.before_request",
        name="group_admin_planner_prompt",
        description="向 Planner 的 messages 注入群管理准则，让 LLM 在决策时具备管理意识。",
        mode=HookMode.BLOCKING,
        order=HookOrder.EARLY,
        error_policy=ErrorPolicy.SKIP,
    )
    async def inject_admin_planner_prompt(self, **kwargs: Any):
        group_id = self._resolve_injection_group_id(**kwargs)
        if self.config.logging.verbose_logging:
            self.ctx.logger.info("[群管理] planner 注入检测: group_id=%s kwargs keys=%s stream_to_group keys=%s",
                group_id, list(kwargs.keys()), list(self._stream_to_group.keys())[:10])
        if group_id <= 0:
            return {"action": "continue"}
        sid = str(kwargs.get("session_id", ""))
        if sid:
            self._stream_to_group[sid] = group_id
        role = await self._ensure_bot_role(group_id) or "member"
        prompt = self._build_admin_planner_prompt(group_id, role)
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
            content = str(message.get("content") or "")
            if role_name == "system" and not inserted:
                if self.PROMPT_MARKER not in content:
                    content = f"{content.rstrip()}\n\n{prompt}" if content.strip() else prompt
                    message["content"] = content
                inserted = True
            updated.append(message)
        if not inserted:
            updated.insert(0, {"role": "system", "content": prompt})
        self.ctx.logger.debug("[群管理] planner.before_request 注入成功: group=%s role=%s", group_id, role)
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
        deny_flags = ("我没有权限", "我不能执行", "我无法进行", "我做不到", "权限不足", "无法禁言", "无法踢人", "不能操作", "我没有这个权限")
        if any(flag in response_text for flag in deny_flags):
            role_cn = self._ROLE_CN.get(role, role)
            correction = f"我是{role_cn}，我来处理。"
            if self.config.logging.verbose_logging:
                self.ctx.logger.info(f"[群管理] 守门拦截: Bot(role={role})错误宣称无权限, group={group_id}\n--- 原始回复 ---\n{response_text}\n--- 替换为 ---\n{correction}")
            else:
                self.ctx.logger.warning(f"[群管理] 守门拦截: Bot(role={role})错误宣称无权限, group={group_id}, text={response_text[:80]}")
            return {"action": "continue", "modified_kwargs": {"response": correction}}
        return {"action": "continue"}
