"""群管理助手 — 核心生命周期、辅助方法与后台任务"""

from __future__ import annotations

import asyncio
import os
import base64
import json
import re
import time
import urllib.request
from collections import deque
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, ClassVar, Literal, Optional

import tomlkit

from maibot_sdk import MaiBotPlugin, PluginConfigBase

from .config_model import (
    EscalationStepConfig,
    GroupAdminConfig,
    PromptsSectionConfig,
)


EmojiReviewState = Literal["passed", "banned", "needs_recheck", "unknown"]


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
        self._recent_user_messages: dict[tuple[Any, ...], deque[tuple[float, str]]] = {}
        self._recent_group_managers: dict[int, deque[tuple[float, int, str]]] = {}
        self._audit_tasks: dict[tuple[Any, ...], asyncio.Task] = {}
        self._audit_seen_messages: dict[str, float] = {}
        self._seen_emoji_hashes: dict[tuple[int, str], float] = {}
        self._last_spam_action_time: dict[tuple[Any, ...], float] = {}
        self._host_persona_context: str = ""
        self._host_reply_style: str = ""
        self._host_persona_cached_at: float = 0.0

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
        recent_maxlen = self._recent_message_history_maxlen()
        for key in list(self._recent_user_messages.keys()):
            self._recent_user_messages[key] = deque(
                ((ts, text) for ts, text in self._recent_user_messages[key] if now - ts <= 900),
                maxlen=recent_maxlen,
            )
            if not self._recent_user_messages[key]:
                del self._recent_user_messages[key]
        for gid in list(self._recent_group_managers.keys()):
            keep: list[tuple[float, int, str]] = []
            for item in self._recent_group_managers[gid]:
                if len(item) == 2:
                    ts, uid = item
                    role = "admin"
                else:
                    ts, uid, role = item
                keep.append((ts, uid, role))
            self._recent_group_managers[gid] = deque(keep, maxlen=12)
            if not self._recent_group_managers[gid]:
                del self._recent_group_managers[gid]
        for key, task in list(self._audit_tasks.items()):
            if task.done():
                del self._audit_tasks[key]
        for msg_id, ts in list(self._audit_seen_messages.items()):
            if now - ts > 900:
                del self._audit_seen_messages[msg_id]
        for image_key, ts in list(self._seen_emoji_hashes.items()):
            if now - ts > 86400:
                del self._seen_emoji_hashes[image_key]
        if len(self._seen_emoji_hashes) > 5000:
            keys = sorted(self._seen_emoji_hashes.keys(), key=lambda k: self._seen_emoji_hashes[k])
            for image_key in keys[:len(keys) - 3000]:
                del self._seen_emoji_hashes[image_key]
        for key, ts in list(self._last_spam_action_time.items()):
            if now - ts > max(self.config.warning.spam_warn_window, 600):
                del self._last_spam_action_time[key]
        self._last_cleanup_time = now

    # ===== 入站审核辅助 =====

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
        for key in ("message_segments", "segments", "message"):
            parts = self._extract_segment_text_parts(message.get(key))
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
            if isinstance(data, str) and data.strip():
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
            text = " ".join(self._extract_segment_text_parts(segments, skip_reply_context=True)).strip()
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
                if nested is not value:
                    yield from self._iter_message_segments(nested, skip_reply_context=skip_reply_context)
        elif isinstance(value, list):
            for item in value:
                yield from self._iter_message_segments(item, skip_reply_context=skip_reply_context)

    def _extract_rendered_media_descriptions(self, text: str) -> list[dict[str, str]]:
        descriptions: list[dict[str, str]] = []
        for media_type, desc in re.findall(r"\[(表情包|图片)[:：]\s*([^\]]+)\]", str(text or "")):
            normalized_desc = str(desc or "").strip()
            if normalized_desc:
                descriptions.append({
                    "type": "emoji" if media_type == "表情包" else "image",
                    "description": normalized_desc,
                })
        return descriptions

    def _extract_media_segments(self, message: Any, rendered_text: str = "") -> list[dict[str, str]]:
        if not isinstance(message, dict):
            return []
        images: list[dict[str, str]] = []
        seen: set[tuple[str, str, str, str]] = set()
        for seg in self._iter_message_segments(message, skip_reply_context=True):
            if not isinstance(seg, dict):
                continue
            seg_type = str(seg.get("type") or seg.get("message_type") or "").strip().lower()
            data = seg.get("data", {})
            data_dict = data if isinstance(data, dict) else {}
            has_image_data = any(
                data_dict.get(key) or seg.get(key)
                for key in ("binary_data_base64", "base64", "image_base64", "emoji_base64", "hash", "url", "file")
            )
            if seg_type not in ("image", "emoji") and not has_image_data:
                continue
            image_hash = str(seg.get("hash") or seg.get("binary_hash") or data_dict.get("hash") or data_dict.get("file_hash") or data_dict.get("image_hash") or "").strip()
            image_base64 = str(seg.get("binary_data_base64") or seg.get("base64") or seg.get("image_base64") or seg.get("emoji_base64") or data_dict.get("binary_data_base64") or data_dict.get("base64") or data_dict.get("image_base64") or data_dict.get("emoji_base64") or "").strip()
            image_url = str(seg.get("url") or seg.get("file") or data_dict.get("url") or data_dict.get("file") or "").strip()
            image_format = str(seg.get("image_format") or seg.get("format") or data_dict.get("image_format") or data_dict.get("format") or "").strip().lower()
            rendered_description = str(seg.get("description") or seg.get("desc") or seg.get("content") or data_dict.get("description") or data_dict.get("desc") or data_dict.get("content") or "").strip()
            if rendered_description in ("[表情包]", "[图片]", "表情包", "图片"):
                rendered_description = ""
            if not image_format and "." in image_url:
                suffix = image_url.rsplit(".", 1)[-1].lower()
                image_format = suffix if suffix in ("jpg", "jpeg", "png", "gif", "webp") else ""
            if not (image_hash or image_base64 or image_url.startswith(("http://", "https://"))):
                continue
            key = (image_hash, image_base64[:64], image_url, image_format)
            if key in seen:
                continue
            seen.add(key)
            images.append({
                "type": "emoji" if seg_type == "emoji" else "image",
                "hash": image_hash,
                "base64": image_base64,
                "url": image_url,
                "format": image_format,
                "description": rendered_description,
            })
        rendered_descriptions = self._extract_rendered_media_descriptions(rendered_text or self._extract_message_text(message))
        if rendered_descriptions:
            queues: dict[str, list[str]] = {"image": [], "emoji": []}
            for item in rendered_descriptions:
                queues.setdefault(item["type"], []).append(item["description"])
            for image in images:
                image_type = image.get("type", "image")
                if image.get("description"):
                    continue
                queue = queues.get(image_type, [])
                if queue:
                    image["description"] = queue.pop(0)
        return images

    def _is_forwarded_chat_record(self, message: Any, text: str = "") -> bool:
        if isinstance(message, dict):
            for seg in self._iter_message_segments(message, skip_reply_context=True):
                if not isinstance(seg, dict):
                    continue
                seg_type = str(seg.get("type") or seg.get("message_type") or "").strip().lower()
                data = seg.get("data", {})
                data_dict = data if isinstance(data, dict) else {}
                if seg_type in ("forward", "merged_forward", "forward_msg", "node"):
                    return True
                if any(data_dict.get(key) for key in ("forward_id", "resid", "node_id")):
                    return True
        normalized = re.sub(r"\s+", "", str(text or ""))
        return any(marker in normalized for marker in ("合并转发", "转发聊天记录", "转发的聊天记录", "聊天记录", "[聊天记录]", "【聊天记录】"))

    def _extract_forward_record_ids(self, message: Any, text: str = "") -> list[str]:
        ids: list[str] = []

        def add(value: Any) -> None:
            token = str(value or "").strip()
            if token and token not in ids:
                ids.append(token)

        if isinstance(message, dict):
            for seg in self._iter_message_segments(message, skip_reply_context=True):
                if not isinstance(seg, dict):
                    continue
                data = seg.get("data", {})
                data_dict = data if isinstance(data, dict) else {}
                for key in ("forward_id", "resid", "node_id", "id", "file"):
                    add(seg.get(key) or data_dict.get(key))
        for pattern in (r"(?:forward_id|resid)\s*[:=]\s*['\"]?([A-Za-z0-9_\-+/=]{8,})", r'"(?:forward_id|resid)"\s*:\s*"([^"]+)"'):
            for match in re.finditer(pattern, str(text or "")):
                add(match.group(1))
        return ids

    def _merge_media_segments(self, first: list[dict[str, str]], second: list[dict[str, str]]) -> list[dict[str, str]]:
        merged: list[dict[str, str]] = []
        seen: set[tuple[str, str, str, str, str]] = set()
        for image in [*first, *second]:
            if not isinstance(image, dict):
                continue
            normalized = {
                "type": str(image.get("type") or "image").strip().lower() or "image",
                "hash": str(image.get("hash") or "").strip(),
                "base64": str(image.get("base64") or "").strip(),
                "url": str(image.get("url") or "").strip(),
                "format": str(image.get("format") or "").strip(),
                "description": str(image.get("description") or "").strip(),
            }
            if normalized["type"] not in ("image", "emoji"):
                normalized["type"] = "image"
            key = (normalized["type"], normalized["hash"], normalized["base64"][:64], normalized["url"], normalized["format"])
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
            return "\n".join(part for item in value if (part := self._render_forward_payload_text(item, depth + 1))).strip()
        if not isinstance(value, dict):
            return ""
        data = value.get("data", {})
        data_dict = data if isinstance(data, dict) else {}
        for key in ("text", "content", "summary", "title", "desc", "message"):
            for content in (value.get(key), data_dict.get(key)):
                if content:
                    rendered = self._render_forward_payload_text(content, depth + 1)
                    if rendered:
                        return rendered
        return ""

    def _render_forward_payload(self, payload: Any, depth: int = 0) -> tuple[str, list[dict[str, str]]]:
        if depth > 8:
            return "", []
        media_items: list[dict[str, str]] = []
        lines: list[str] = []
        if isinstance(payload, dict):
            media_items = self._merge_media_segments(media_items, self._extract_media_segments(payload))
            for list_key in ("messages", "nodes", "forward", "forward_messages"):
                items = payload.get(list_key)
                if isinstance(items, list):
                    for item in items:
                        text, nested_media = self._render_forward_payload(item, depth + 1)
                        if text:
                            lines.append(text)
                        media_items = self._merge_media_segments(media_items, nested_media)
                    return "\n".join(lines).strip(), media_items
            content = payload.get("content") or payload.get("message")
            if content:
                text, nested_media = self._render_forward_payload(content, depth + 1)
                media_items = self._merge_media_segments(media_items, nested_media)
                if text:
                    sender = str(payload.get("name") or payload.get("nickname") or payload.get("user_id") or "").strip()
                    return (f"{sender}: {text}" if sender else text), media_items
            return self._render_forward_payload_text(payload, depth + 1), media_items
        if isinstance(payload, list):
            for item in payload:
                text, nested_media = self._render_forward_payload(item, depth + 1)
                if text:
                    lines.append(text)
                media_items = self._merge_media_segments(media_items, nested_media)
            return "\n".join(lines).strip(), media_items
        return self._render_forward_payload_text(payload, depth + 1), media_items

    async def _expand_forwarded_record_for_audit(self, message: Any, text: str, media_items: list[dict[str, str]]) -> tuple[str, list[dict[str, str]]]:
        if not getattr(self.config.moderation_behavior, "expand_forwarded_records", True):
            return text, media_items
        rendered_text, rendered_media = self._render_forward_payload(message)
        merged_media = self._merge_media_segments(media_items, rendered_media)
        forward_ids = self._extract_forward_record_ids(message, text)
        for forward_id in forward_ids[:3]:
            for api_name in ("adapter.napcat.message.get_forward_msg", "adapter.napcat.message.get_forward_message"):
                ok, data = await self._call_action_api(api_name=api_name, id=forward_id)
                if not ok:
                    ok, data = await self._call_action_api(api_name=api_name, message_id=forward_id)
                if ok:
                    extra_text, extra_media = self._render_forward_payload(data)
                    if extra_text:
                        rendered_text = f"{rendered_text}\n{extra_text}".strip()
                    merged_media = self._merge_media_segments(merged_media, extra_media)
                    break
        if rendered_text:
            return f"[QQ合并转发]\n{rendered_text}", merged_media
        return text, merged_media

    def _decode_image_base64(self, image_base64: str) -> bytes:
        payload = str(image_base64 or "").strip()
        if "," in payload and payload.lower().startswith("data:"):
            payload = payload.split(",", 1)[1]
        return base64.b64decode(payload, validate=False)

    def _download_image_url_sync(self, image_url: str, timeout: float = 8.0, max_bytes: int = 10 * 1024 * 1024) -> bytes:
        request = urllib.request.Request(image_url, headers={"User-Agent": "MaiBotGroupAdmin/2.0"})
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return response.read(max_bytes + 1)[:max_bytes]

    def _extract_message_user_id(self, message: Any, kwargs: dict[str, Any]) -> int:
        for key in ("user_id", "sender_id", "sender", "from_user_id"):
            uid = self._to_int(kwargs.get(key, 0))
            if uid:
                return uid
        for key in ("user_info", "sender_info", "message_user_info"):
            info = kwargs.get(key)
            if isinstance(info, dict):
                for id_key in ("user_id", "sender_id", "id", "qq", "uin"):
                    uid = self._to_int(info.get(id_key, 0))
                    if uid:
                        return uid
        if isinstance(message, dict):
            for key in ("user_id", "sender_id", "from_user_id"):
                uid = self._to_int(message.get(key, 0))
                if uid:
                    return uid
            mi = message.get("message_info", {}) or {}
            if isinstance(mi, dict):
                ui = mi.get("user_info") or mi.get("sender_info") or {}
                if isinstance(ui, dict):
                    for key in ("user_id", "sender_id", "id", "qq", "uin"):
                        uid = self._to_int(ui.get(key, 0))
                        if uid:
                            return uid
                ac = mi.get("additional_config", {}) or {}
                if isinstance(ac, dict):
                    for key in ("user_id", "sender_id", "platform_io_target_user_id", "account_id"):
                        uid = self._to_int(ac.get(key, 0))
                        if uid:
                            return uid
            sender = message.get("sender") or message.get("user") or {}
            if isinstance(sender, dict):
                for key in ("user_id", "id", "qq", "uin"):
                    uid = self._to_int(sender.get(key, 0))
                    if uid:
                        return uid
            mbi = message.get("message_base_info", {}) or {}
            if isinstance(mbi, dict):
                uid = self._to_int(mbi.get("user_id") or mbi.get("sender_id") or 0)
                if uid:
                    return uid
        message_info = getattr(message, "message_info", None)
        user_info = getattr(message_info, "user_info", None)
        for key in ("user_id", "sender_id", "id", "qq", "uin"):
            uid = self._to_int(getattr(user_info, key, 0))
            if uid:
                return uid
        return 0

    def _extract_json_object(self, text: str) -> dict[str, Any]:
        raw = str(text or "").strip()
        raw = re.sub(r"^```(?:json)?", "", raw, flags=re.I).strip()
        raw = re.sub(r"```$", "", raw).strip()
        try:
            data = json.loads(raw)
            return data if isinstance(data, dict) else {}
        except Exception:
            pass
        match = re.search(r"\{.*\}", raw, flags=re.S)
        if not match:
            return {}
        try:
            data = json.loads(match.group(0))
            return data if isinstance(data, dict) else {}
        except Exception:
            return {}

    def _build_audit_prompt(self, group_id: int, user_id: int, text: str, history: list[tuple[float, str]], forwarded_record_single_message: bool = False) -> str:
        now = time.time()
        recent_lines = [f"- {max(0, int(now - ts))}s ago: {item}" for ts, item in history[-6:]]
        recent = "\n".join(recent_lines) if recent_lines else "- none"
        role = self._get_group_role(group_id) or "member"
        available = {
            "owner": "禁言/解禁/踢人/警告/设精华/撤回/公告/改名/审批入群",
            "admin": "禁言/解禁/踢人/警告/设精华/撤回/改名片/审批入群",
        }.get(role, "管理操作受限，可协助管理员做决策建议")
        policy_prompt = str(self.config.prompts.auto_moderate_system or "").strip()
        policy_prompt = policy_prompt.replace("{bot_role}", role).replace("{available_actions}", available)
        forwarded_note = (
            "current_message 是一条QQ合并转发聊天记录。刷屏判定时只按当前发送者发了一条消息计数，"
            "不要把转发内部多条记录当成该用户连续刷屏；但仍需审核转发内部可见内容。\n\n"
            if forwarded_record_single_message else ""
        )
        return (
            "你是QQ群入站消息审核器。根据语义判断是否需要群管理动作，不要按关键词机械判定。\n"
            "普通口癖、玩笑、轻微情绪、无明确对象的吐槽、正常聊天，一律不要处罚；不确定必须返回 none。\n"
            "只允许自动返回 warn 或 mute，不要返回 kick。recall 表示是否建议撤回当前消息。\n\n"
            f"群内处罚策略：\n{policy_prompt or '- 未配置'}\n\n"
            "识别范围：连续刷屏、辱骂/人身攻击、广告/诈骗/赌博/交易诱导、色情/违法/血腥惊吓、恶意引战。\n"
            "图片描述模型拒答、描述为空或超时只能作为风险线索，必须结合其他内容判断。\n"
            f"{forwarded_note}"
            "Do not classify a single file share or a single link as spam. Only classify spam when same_user_recent_messages shows repeated similar content.\n"
            f"group_id: {group_id}\nuser_id: {user_id}\n"
            f"same_user_recent_messages:\n{recent}\n\ncurrent_message:\n{text}\n\n"
            "只输出一个 JSON 对象，不要解释：\n"
            '{"action":"none|warn|mute","violation_type":"none|spam|abuse|ad|sexual|illegal|conflict",'
            '"confidence":0.0,"duration":0,"recall":false,"reason":"简短中文原因"}'
        )

    async def _llm_generate(self, prompt: str, model: str, temperature: float = 0.0, max_tokens: int = 220) -> dict[str, Any]:
        if hasattr(self.ctx, "llm") and hasattr(self.ctx.llm, "generate"):
            result = await self.ctx.llm.generate(prompt=prompt, model=model, temperature=temperature, max_tokens=max_tokens)
            return result if isinstance(result, dict) else {"success": True, "response": str(result)}
        ok, result = await self._call_api(
            api_name="llm.generate",
            prompt=prompt,
            model=model,
            temperature=temperature,
            max_tokens=max_tokens,
        )
        if isinstance(result, dict):
            return result
        return {"success": ok, "response": str(result)}

    async def _classify_vlm_description_status(self, description: str, image_type: str = "image", image_hash: str = "") -> str:
        text = str(description or "").strip()
        if not text:
            return "timeout"
        if not bool(getattr(self.config.media_audit, "violation_media_text_judge_enabled", True)):
            return "ok"
        model = str(getattr(self.config.auto_moderate, "audit_model", "utils") or "utils").strip() or "utils"
        prompt = (
            "你是图片审核流水线的元判定器，只判断一段 VLM 图片描述文本的性质。\n"
            "任务：判断这段文本属于正常图片内容描述、模型明确拒绝识图，还是普通识图失败。\n"
            "不要根据图片是否违规来判断；即使描述了色情、暴力、违法内容，只要仍在描述图片内容，就属于 ok。\n"
            "rejected 仅用于：文本明确表达模型因安全、政策、违规、拒答边界而拒绝描述或分析图片。\n"
            "failure 用于：文本表示看不清、无法识别、没有识别出内容、无法判断图片内容等非拒答失败。\n"
            "如果不确定，返回 ok。\n\n"
            f"image_type: {image_type}\n"
            f"image_hash_prefix: {str(image_hash or '')[:12]}\n"
            f"description:\n{text[:1200]}\n\n"
            '只输出 JSON：{"status":"ok|rejected|failure","reason":"一句很短的中文理由"}'
        )
        try:
            result = await self._llm_generate(prompt, model, temperature=0.0, max_tokens=80)
            raw = str(result.get("response", "") if isinstance(result, dict) else result).strip()
            match = re.search(r"\{.*\}", raw, re.S)
            data = json.loads(match.group(0) if match else raw)
            status = str(data.get("status", "ok") or "ok").strip().lower()
            if status in ("rejected", "failure"):
                if self.config.logging.verbose_logging:
                    reason = str(data.get("reason", "") or "").strip()
                    self.ctx.logger.info(f"[群管理] VLM描述状态判定: hash={str(image_hash or '')[:12]} status={status} reason={reason}")
                return status
        except Exception as e:
            self.ctx.logger.warning(f"[群管理] VLM描述状态判定失败，按正常描述处理: hash={str(image_hash or '')[:12]} error={e}")
        return "ok"

    def _persona_comments_enabled(self) -> bool:
        return bool(getattr(self.config.moderation_behavior, "persona_managed_comments_enabled", False))

    def _persona_comments_model(self) -> str:
        return str(getattr(self.config.moderation_behavior, "persona_managed_comments_model", "replyer") or "replyer").strip() or "replyer"

    @staticmethod
    def _build_notice_task_context() -> str:
        return "\n".join([
            "【群管理发言任务边界】",
            "你正在生成由 MaiBot 当前角色本人对外发送的群管理文本。",
            "不要自称群管理助手、系统、插件、审核员或管理员。",
            "不要编造没有发生的操作结果，不要改变给定事实。",
            "不要输出 @；插件会在发送时自动 @ 目标用户。",
        ])

    async def _build_host_persona_context(self) -> str:
        base_context = self._build_notice_task_context()
        now = time.monotonic()
        if self._host_persona_context and self._host_persona_cached_at and now - self._host_persona_cached_at < 60.0:
            return f"{self._host_persona_context}\n\n{base_context}"
        if not hasattr(self.ctx, "config") or not hasattr(self.ctx.config, "get"):
            self._host_persona_context = ""
            self._host_reply_style = ""
            self._host_persona_cached_at = 0.0
            return base_context
        config_keys = ("bot.nickname", "bot.alias_names", "personality.personality", "personality.reply_style")
        defaults: tuple[Any, ...] = ("", [], "", "")
        try:
            results = await asyncio.gather(
                *(self.ctx.config.get(key, default) for key, default in zip(config_keys, defaults, strict=True)),
                return_exceptions=True,
            )
        except Exception as e:
            self.ctx.logger.debug(f"[群管理] 读取MaiBot人格配置失败: {e}")
            self._host_persona_context = ""
            self._host_reply_style = ""
            self._host_persona_cached_at = 0.0
            return base_context
        values: list[Any] = []
        for result, default in zip(results, defaults, strict=True):
            values.append(default if isinstance(result, BaseException) else result)
        nickname, aliases, personality, reply_style = values
        persona_lines = [
            "【必须遵守的 MaiBot 身份与表达方式】",
            "下面的人格和表达风格决定最终文本的措辞、句式、语气与态度。",
        ]
        nickname_text = str(nickname or "").strip()
        if nickname_text:
            persona_lines.append(f"你的名字：{nickname_text}")
        if isinstance(aliases, (list, tuple, set)):
            alias_text = "、".join(str(alias).strip() for alias in aliases if str(alias).strip())
        else:
            alias_text = str(aliases or "").strip()
        if alias_text:
            persona_lines.append(f"你的别名：{alias_text[:300]}")
        personality_text = str(personality or "").strip()
        if personality_text:
            persona_lines.append(f"人格设定：{personality_text[:2000]}")
        reply_style_text = str(reply_style or "").strip()
        self._host_reply_style = reply_style_text[:1600]
        if self._host_reply_style:
            persona_lines.append(f"表达风格：{self._host_reply_style}")
        if len(persona_lines) == 2:
            self._host_persona_context = ""
            self._host_persona_cached_at = 0.0
            return base_context
        persona_lines.extend([
            "请直接以这个角色本人说话，不要切换成通用 AI、客服或系统腔调。",
            "群管理任务只能限制要表达的事实，不得覆盖这里的人格与表达风格。",
        ])
        self._host_persona_context = "\n".join(persona_lines)
        self._host_persona_cached_at = now
        return f"{self._host_persona_context}\n\n{base_context}"

    @staticmethod
    def _normalize_persona_comment(text: str, max_chars: int = 120) -> str:
        normalized = re.sub(r"^```.*?```$", "", str(text or ""), flags=re.S).strip()
        normalized = normalized.replace("\n", " ").strip(" \"'“”「」")
        normalized = re.sub(r"@\S+", "", normalized).strip()
        return normalized[:max(1, max_chars)]

    async def _generate_persona_managed_comment(self, kind: str, context: str, fallback: str, max_chars: int = 120) -> str:
        fallback = str(fallback or "").strip()
        if not self._persona_comments_enabled():
            return fallback
        system_prompt = await self._build_host_persona_context()
        prompt = "\n".join([
            "请按 MaiBot 当前人设和表达风格，生成一句可以直接发到群里的中文短句。",
            f"发言类型：{kind}",
            f"必须表达的事实：{context}",
            f"原始固定文本：{fallback}",
            "要求：不得改变操作结果、目标、时长、原因或错误含义；不要输出@；不要提插件、系统、审核、JSON；不要解释生成过程。",
            f"最多 {max_chars} 个中文字符，只输出最终文本。",
        ])
        try:
            result = await self._llm_generate(f"{system_prompt}\n\n{prompt}", self._persona_comments_model(), temperature=0.5, max_tokens=100)
            if isinstance(result, dict) and result.get("success", True):
                text = self._normalize_persona_comment(str(result.get("response", "")), max_chars=max_chars)
                if text:
                    return text
        except Exception as e:
            self.ctx.logger.warning(f"[群管理] 生成人设化管理评论失败: kind={kind}: {e}")
        return fallback

    async def _send_persona_text(self, stream_id: str, fallback: str, kind: str = "command_success", context: str = "", max_chars: int = 120) -> None:
        if kind in {"command_success", "command_failure", "usage_hint", "permission_denied"}:
            await self.ctx.send.text(str(fallback or ""), stream_id)
            return
        text = await self._generate_persona_managed_comment(kind, context or fallback, fallback, max_chars=max_chars)
        await self.ctx.send.text(text, stream_id)

    async def _send_persona_at_text(self, stream_id: str, prefix: str, qq: int, suffix: str = "", kind: str = "command_success", context: str = "", max_chars: int = 120) -> None:
        if kind in {"command_success", "command_failure", "usage_hint", "permission_denied"}:
            await self._send_at_text(stream_id, prefix, qq, suffix)
            return
        if not self._persona_comments_enabled():
            await self._send_at_text(stream_id, prefix, qq, suffix)
            return
        fallback = f"{prefix} @{qq}{suffix}".strip()
        text = await self._generate_persona_managed_comment(kind, context or fallback, fallback, max_chars=max_chars)
        await self._send_at_text(stream_id, "", qq, f" {text}")

    def _apply_warning_reply_prefix(self, text: str) -> str:
        prefix = str(getattr(self.config.warning, "warning_reply_prefix", "") or "")
        body = str(text or "").strip()
        if not prefix:
            return body
        if not body:
            return prefix
        return f"{prefix}{body}" if prefix.endswith((" ", "\t", "\n")) else f"{prefix} {body}"

    async def _generate_basic_moderation_notice(self, violation_type: str, reason: str) -> str:
        prompt = (
            "请以当前群聊角色口吻，对刚刚的群管理处理自然接一句短话。"
            "不要说已警告/已禁言/系统判定/插件/审核，不要@任何人。\n"
            f"违规类型：{violation_type}\n原因：{reason}\n只输出一句中文短回复。"
        )
        try:
            result = await self._llm_generate(prompt, "replyer", temperature=0.6, max_tokens=80)
            if isinstance(result, dict) and result.get("success", True):
                text = self._normalize_persona_comment(str(result.get("response", "")), max_chars=120)
                if text:
                    return text
        except Exception as e:
            self.ctx.logger.warning(f"[群管理] 生成人设提醒失败: {e}")
        return "先停一下，这个不太适合继续刷。"

    async def _generate_moderation_notice(self, violation_type: str, reason: str) -> str:
        if not self._persona_comments_enabled():
            return await self._generate_basic_moderation_notice(violation_type, reason)
        context = f"刚刚进行了群管理处理。违规类型={violation_type}；原因={reason}。请自然提醒边界，维持群聊氛围。"
        return await self._generate_persona_managed_comment(
            "moderation_notice",
            context,
            "先停一下，这个不太适合继续刷。",
            max_chars=120,
        )

    async def _resolve_group_stream_id(self, group_id: int, stream_id: str = "") -> str:
        if stream_id:
            return stream_id
        for sid, gid in self._stream_to_group.items():
            if gid == group_id:
                return sid
        for api_name in ("chat.get_stream_by_group_id", "chat.open_session"):
            ok, data = await self._call_api(api_name=api_name, group_id=group_id)
            if ok:
                if isinstance(data, dict):
                    sid = str(data.get("stream_id") or data.get("session_id") or data.get("chat_id") or data.get("id") or "")
                else:
                    sid = str(data or "")
                if sid:
                    self._stream_to_group[sid] = group_id
                    return sid
        return ""

    async def _trigger_native_moderation_reply(self, stream_id: str, group_id: int, action: str, violation_type: str, reason: str):
        target_stream = await self._resolve_group_stream_id(group_id, stream_id)
        if not self._persona_comments_enabled():
            notice = await self._generate_moderation_notice(violation_type, reason)
            if target_stream:
                await self.ctx.send.text(notice, target_stream)
                return
            self.ctx.logger.warning(f"[群管理] 未找到群聊stream，跳过管理评论发送: group={group_id} action={action} type={violation_type}")
            return
        if target_stream:
            notice = await self._generate_moderation_notice(violation_type, reason)
            await self.ctx.send.text(notice, target_stream)
            if self.config.logging.verbose_logging:
                self.ctx.logger.info(f"[群管理] 已发送人设化管理评论: group={group_id} stream={target_stream} action={action} type={violation_type}")
            return
        self.ctx.logger.warning(f"[群管理] 未找到群聊stream，跳过人设化管理评论发送: group={group_id} action={action} type={violation_type}")

    async def _maybe_recall_audited_message(self, group_id: int, message_id: str, reason: str) -> None:
        if not message_id:
            return
        try:
            await self.tool_recall_msg(group_id=group_id, message_id=message_id, reason=reason)
        except Exception as e:
            self.ctx.logger.warning(f"[群管理] 自动撤回失败: group={group_id} message={message_id}: {e}")

    async def _get_media_description_for_audit(self, image_info: dict[str, str], timeout: float = 12.0, image_index: int = 0, notify_on_description_failure: bool = False) -> tuple[str, str]:
        image_type = str(image_info.get("type") or "image").strip().lower()
        image_hash = str(image_info.get("hash") or "").strip()
        image_base64 = str(image_info.get("base64") or "").strip()
        image_url = str(image_info.get("url") or "").strip()
        rendered_description = str(image_info.get("description") or "").strip()
        retry_unrecognized_media = bool(getattr(self.config.media_audit, "retry_violation_media_with_image_audit", True))

        emoji_review_state: EmojiReviewState = "unknown"
        emoji_recheck_image_bytes: bytes | None = None
        if retry_unrecognized_media and image_type == "emoji" and image_hash:
            emoji_review_state, emoji_recheck_image_bytes = await self._get_emoji_review_state(image_hash)

        emoji_needs_recheck = emoji_review_state == "needs_recheck" and emoji_recheck_image_bytes is not None

        def cacheable_status(status: str) -> str:
            if image_type == "emoji" and retry_unrecognized_media and image_hash and emoji_review_state == "unknown" and status == "ok":
                return "ok_uncached"
            return status

        if image_type == "emoji" and not emoji_needs_recheck:
            if rendered_description:
                status = await self._classify_vlm_description_status(rendered_description, image_type, image_hash)
                if status != "rejected":
                    return rendered_description, cacheable_status(status)
                return rendered_description, "rejected"
            emoji_desc = await self._get_emoji_description_for_audit(image_hash, image_base64)
            if emoji_desc:
                status = await self._classify_vlm_description_status(emoji_desc, image_type, image_hash)
                if status != "rejected":
                    return emoji_desc, cacheable_status(status)
                return emoji_desc, "rejected"
        if image_type != "emoji" and rendered_description:
            status = await self._classify_vlm_description_status(rendered_description, image_type, image_hash)
            if status != "rejected":
                return rendered_description, status
            return rendered_description, "rejected"

        if notify_on_description_failure:
            self.ctx.logger.info(f"[group-admin] media description unavailable; notify_on_media_description_failure enabled, skip forced VLM/LLM recheck: index={image_index} hash={image_hash[:12]}")
            return "", "timeout"

        stored_image_bytes: bytes | None = None
        if retry_unrecognized_media and image_type != "emoji" and image_hash:
            stored_image_bytes = await self._get_stored_image_bytes_for_audit(image_hash, "image")

        try:
            from src.chat.image_system.image_manager import image_manager
        except Exception as e:
            self.ctx.logger.warning(f"[群管理] 无法导入图片描述管理器: {e}")
            return "", "timeout"

        forced_description_sources: set[str] = set()

        async def force_describe_bytes(image_bytes: bytes, source_key: str) -> str:
            if not image_bytes:
                return ""
            if source_key in forced_description_sources:
                return ""
            forced_description_sources.add(source_key)
            try:
                saved_image = await image_manager.ensure_image_saved(image_bytes)
                if not getattr(saved_image, "image_format", ""):
                    await saved_image.calculate_hash_format()
                image_format = str(getattr(saved_image, "image_format", "") or "")
                if not image_format:
                    return ""
                desc = await image_manager._generate_image_description(image_bytes, image_format)
                return str(desc or "").strip()
            except Exception as e:
                if self.config.logging.verbose_logging:
                    self.ctx.logger.info(f"[group-admin] violation media VLM forced recheck failed: index={image_index} hash={image_hash[:12]} error={e}")
                return ""

        async def read_once() -> tuple[str, bool]:
            if emoji_recheck_image_bytes:
                text = await force_describe_bytes(emoji_recheck_image_bytes, "emoji_recheck")
                if text:
                    return text, True
            if stored_image_bytes:
                text = await force_describe_bytes(stored_image_bytes, "stored")
                if text:
                    return text, True
            if image_base64:
                text = await force_describe_bytes(self._decode_image_base64(image_base64), "base64")
                if text:
                    return text, True
            if image_url:
                image_bytes = await asyncio.to_thread(self._download_image_url_sync, image_url)
                if image_bytes:
                    text = await force_describe_bytes(image_bytes, "url")
                    if text:
                        return text, True
            if image_hash:
                desc = await image_manager.get_image_description(image_hash=image_hash, wait_for_build=True)
                if desc:
                    return str(desc).strip(), False
            return "", False

        deadline = time.time() + timeout
        while time.time() < deadline:
            try:
                desc, forced_recheck = await asyncio.wait_for(read_once(), timeout=max(0.5, min(3.0, deadline - time.time())))
                if desc:
                    status = await self._classify_vlm_description_status(desc, image_type, image_hash)
                    if status == "rejected":
                        self.ctx.logger.info(f"[group-admin] violation media VLM recheck rejected: index={image_index} hash={image_hash[:12]} forced={forced_recheck} text={desc[:500]}")
                        return desc, "rejected"
                    if status == "failure":
                        self.ctx.logger.info(f"[group-admin] violation media VLM recheck failed to describe: index={image_index} hash={image_hash[:12]} forced={forced_recheck} text={desc[:500]}")
                    return desc, cacheable_status(status)
            except Exception:
                if not image_hash:
                    break
            await asyncio.sleep(0.5)
        if emoji_needs_recheck:
            self.ctx.logger.info(f"[group-admin] violation media recheck timed out or empty: index={image_index} hash={image_hash[:12]} state={emoji_review_state}")
            return "", "timeout"
        self.ctx.logger.info(f"[群管理] 图片描述为空或超时，跳过人工通知: index={image_index} hash={image_hash[:12]}")
        return "", "timeout"

    async def _get_emoji_review_state(self, image_hash: str) -> tuple[EmojiReviewState, bytes | None]:
        image_hash = str(image_hash or "").strip()
        if not image_hash:
            return "unknown", None
        try:
            from sqlmodel import select
            from src.common.database.database import get_db_session
            from src.common.database.database_model import Images, ImageType
            from src.common.utils.image_path import resolve_stored_image_path
        except Exception as e:
            if self.config.logging.verbose_logging:
                self.ctx.logger.info(f"[group-admin] failed to read emoji review database state: {e}")
            return "unknown", None

        try:
            with get_db_session() as session:
                statement = select(Images).filter_by(image_hash=image_hash, image_type=ImageType.EMOJI).limit(1)
                record = session.exec(statement).first()
                if record is None:
                    return "unknown", None

                if bool(record.is_banned):
                    return "banned", None
                if bool(record.is_registered):
                    return "passed", None
                if not bool(record.vlm_processed):
                    return "unknown", None
                if bool(record.no_file_flag) or not str(record.full_path or "").strip():
                    return "unknown", None
                image_path = resolve_stored_image_path(record.full_path)

            if not image_path.is_file():
                return "unknown", None
            image_bytes = await asyncio.to_thread(image_path.read_bytes)
            if not image_bytes:
                return "unknown", None
            return "needs_recheck", image_bytes
        except Exception as e:
            if self.config.logging.verbose_logging:
                self.ctx.logger.info(f"[group-admin] failed to read emoji review state: hash={image_hash[:12]} error={e}")
            return "unknown", None

    async def _get_stored_image_bytes_for_audit(self, image_hash: str, image_type: str = "image") -> bytes | None:
        image_hash = str(image_hash or "").strip()
        if not image_hash:
            return None
        try:
            from sqlmodel import select
            from src.common.database.database import get_db_session
            from src.common.database.database_model import Images, ImageType
            from src.common.utils.image_path import resolve_stored_image_path
        except Exception as e:
            if self.config.logging.verbose_logging:
                self.ctx.logger.info(f"[群管理] 无法读取图片数据库状态: {e}")
            return None
        try:
            db_image_type = ImageType.EMOJI if str(image_type).strip().lower() == "emoji" else ImageType.IMAGE
            with get_db_session() as session:
                statement = select(Images).filter_by(image_hash=image_hash, image_type=db_image_type).limit(1)
                record = session.exec(statement).first()
                if record is None or bool(record.no_file_flag) or not str(record.full_path or "").strip():
                    return None
                image_path = resolve_stored_image_path(record.full_path)
            if not image_path.is_file():
                return None
            return await asyncio.to_thread(image_path.read_bytes)
        except Exception as e:
            if self.config.logging.verbose_logging:
                self.ctx.logger.info(f"[群管理] 读取图片原图失败: hash={image_hash[:12]} type={image_type} error={e}")
            return None

    async def _get_emoji_description_for_audit(self, image_hash: str, image_base64: str = "") -> str:
        try:
            from src.emoji_system.emoji_manager import emoji_manager
        except Exception as e:
            self.ctx.logger.warning(f"[群管理] 无法导入表情包管理器: {e}")
            return ""
        emoji_bytes: bytes | None = None
        if image_base64:
            try:
                emoji_bytes = self._decode_image_base64(image_base64)
            except Exception:
                emoji_bytes = None
        try:
            result = await emoji_manager.get_emoji_description(
                emoji_hash=image_hash or None,
                emoji_bytes=emoji_bytes,
                wait_for_build=True,
            )
        except Exception as e:
            if self.config.logging.verbose_logging:
                self.ctx.logger.info(f"[群管理] 表情包描述读取失败: hash={image_hash[:12]} error={e}")
            return ""
        if not result:
            return ""
        desc, tags = result
        text = str(desc or "").strip()
        if text:
            return text
        if isinstance(tags, (list, tuple)):
            tag_text = "，".join(str(tag).strip() for tag in tags if str(tag).strip())
            if tag_text:
                return tag_text
        return ""

    async def _run_media_moderation(self, group_id: int, user_id: int, media_items: list[dict[str, str]], stream_id: str = "", message_id: str = "", forwarded_record_single_message: bool = False, forwarded_record_audit: bool = False) -> None:
        try:
            is_protected, protected_reason = await self._is_protected(group_id, user_id)
            if is_protected:
                if self.config.logging.verbose_logging:
                    self.ctx.logger.info(f"[群管理] 图片审核跳过受保护用户: group={group_id} user={user_id} reason={protected_reason}")
                return
            media_limit = self._to_int(getattr(self.config.media_audit, "forwarded_media_audit_max_items" if forwarded_record_audit else "media_audit_max_items", 4))
            if media_limit <= 0:
                media_limit = 8 if forwarded_record_audit else 4
            try:
                description_timeout = float(getattr(self.config.media_audit, "media_description_timeout", 12.0))
            except Exception:
                description_timeout = 12.0
            description_timeout = max(3.0, min(description_timeout, 60.0))
            violation_media_policy = str(getattr(self.config.media_audit, "violation_media_policy", "none") or "none").strip().lower()
            notify_on_media_description_failure = bool(getattr(self.config.media_audit, "notify_on_media_description_failure", False))
            descriptions: list[str] = []
            review_required: list[str] = []
            counts = {"图片": 0, "表情包": 0}
            audited_count = 0
            seen_media_keys: set[tuple[str, str]] = set()
            for index, image_info in enumerate(media_items, start=1):
                image_type = str(image_info.get("type") or "image").strip().lower()
                label = "表情包" if image_type == "emoji" else "图片"
                image_hash = str(image_info.get("hash") or "").strip()
                media_identity = (
                    image_hash
                    or str(image_info.get("url") or "").strip()
                    or str(image_info.get("base64") or "").strip()[:96]
                    or str(image_info.get("description") or "").strip()[:96]
                )
                if media_identity:
                    media_key = (image_type, media_identity)
                    if media_key in seen_media_keys:
                        continue
                    seen_media_keys.add(media_key)
                emoji_seen_key = (group_id, image_hash)
                if image_type == "emoji" and image_hash and emoji_seen_key in self._seen_emoji_hashes:
                    continue
                if audited_count >= media_limit:
                    break
                audited_count += 1
                desc, desc_status = await self._get_media_description_for_audit(image_info, timeout=description_timeout, image_index=index, notify_on_description_failure=notify_on_media_description_failure)
                if image_type == "emoji" and image_hash and desc_status in ("ok", "rejected"):
                    self._seen_emoji_hashes[emoji_seen_key] = time.time()
                if desc and desc_status != "failure":
                    descriptions.append(f"{index}. type={label} hash={image_hash[:12] or 'unknown'} 描述：{desc}")
                if desc_status == "rejected":
                    review_required.append(f"{index}. type={label} hash={image_hash[:12] or 'unknown'} 描述状态：模型明确拒绝识图，需人工确认{label}内容")
                    counts[label] = counts.get(label, 0) + 1
                elif desc_status in ("failure", "timeout") and notify_on_media_description_failure:
                    review_required.append(f"{index}. type={label} hash={image_hash[:12] or 'unknown'} 描述状态：识图失败/空返回/超时，需人工确认{label}内容")
                    counts[label] = counts.get(label, 0) + 1
            if review_required and violation_media_policy == "notify":
                await self._notify_violation_media_review_target(
                    group_id,
                    user_id,
                    stream_id,
                    message_id,
                    len(review_required),
                    ",".join(review_required),
                    self._format_review_required_kind(counts),
                    self._format_review_required_kind_detail(counts),
                )
            if not descriptions and violation_media_policy != "warn":
                return
            audit_lines = []
            if descriptions:
                audit_lines.append("[图片消息] 图片描述：\n" + "\n".join(descriptions))
            if review_required:
                audit_lines.append("[图片/表情需要人工复核]\n" + "\n".join(review_required))
            if audit_lines:
                self._schedule_llm_moderation(
                    group_id,
                    user_id,
                    "\n\n".join(audit_lines),
                    message_id,
                    stream_id,
                    audit_kind="image",
                    forwarded_record_single_message=forwarded_record_single_message,
                )
        except Exception as e:
            self.ctx.logger.error(f"[群管理] 图片审核异常: {e}", exc_info=True)

    def _schedule_media_moderation(self, group_id: int, user_id: int, media_items: list[dict[str, str]], message_id: str = "", stream_id: str = "", forwarded_record_single_message: bool = False, forwarded_record_audit: bool = False) -> None:
        audit_regular_images = bool(getattr(self.config.media_audit, "audit_regular_images", True))
        audit_emojis = bool(getattr(self.config.media_audit, "audit_emojis", True))
        if not audit_regular_images and not audit_emojis:
            return
        if not media_items or group_id <= 0 or user_id <= 0:
            return
        media_items = [
            image
            for image in media_items
            if (audit_emojis if str(image.get("type") or "image").strip().lower() == "emoji" else audit_regular_images)
        ]
        if not media_items:
            return
        seen_key = f"media:{message_id}" if message_id else ""
        if seen_key:
            if seen_key in self._audit_seen_messages:
                return
            self._audit_seen_messages[seen_key] = time.time()
        key: tuple[Any, ...] = (group_id, user_id, "image", message_id or str(time.time()))
        if key in self._audit_tasks and not self._audit_tasks[key].done():
            return
        self._audit_tasks[key] = asyncio.create_task(
            self._run_media_moderation(group_id, user_id, media_items, stream_id, message_id, forwarded_record_single_message, forwarded_record_audit)
        )

    def _recent_message_history_maxlen(self) -> int:
        threshold = self._to_int(getattr(self.config.warning, "spam_warn_threshold", 3))
        return max(8, threshold + 3)

    def _recent_message_count_for_spam(self, group_id: int, user_id: int, now: float, window: int) -> int:
        history = self._recent_user_messages.get((group_id, user_id), deque())
        return sum(1 for ts, _text in history if now - ts <= window)

    def _treat_forwarded_records_as_single_message(self) -> bool:
        return bool(getattr(self.config.warning, "treat_forwarded_records_as_single_message", True))

    def _has_recent_spam_warning(self, group_id: int, user_id: int, now: float, window: int) -> bool:
        warnings = self._warnings.get(group_id, {}).get(user_id, {}).get("spam", [])
        return any(now - ts <= window for ts, _count in warnings)

    def _schedule_spam_threshold_moderation(self, group_id: int, user_id: int, stream_id: str = "") -> None:
        if group_id <= 0 or user_id <= 0 or not getattr(self.config.warning, "enabled", True):
            return
        asyncio.create_task(self._run_spam_threshold_moderation(group_id, user_id, stream_id))

    async def _run_spam_threshold_moderation(self, group_id: int, user_id: int, stream_id: str = "") -> None:
        wc = self.config.warning
        if not wc.enabled:
            return
        threshold = self._to_int(getattr(wc, "spam_warn_threshold", 3))
        window = self._to_int(getattr(wc, "spam_warn_window", 600))
        if threshold <= 0 or window <= 0:
            return
        now = time.time()
        count = self._recent_message_count_for_spam(group_id, user_id, now, window)
        if count < threshold:
            return
        is_protected, protected_reason = await self._is_protected(group_id, user_id)
        if is_protected:
            if self.config.logging.verbose_logging:
                self.ctx.logger.info(f"[群管理] 本地刷屏计数跳过受保护用户: group={group_id} user={user_id} reason={protected_reason}")
            return
        reason = f"{window}秒内连续发送{count}条消息，超过刷屏阈值{threshold}"
        warn_key = (group_id, user_id, "spam_warn")
        escalation_key = (group_id, user_id, "spam_escalation")
        if count > threshold and self._has_recent_spam_warning(group_id, user_id, now, window):
            action_cooldown = max(self._to_int(getattr(self.config.safeguard, "mute_cooldown", 0)), 30)
            if now - self._last_spam_action_time.get(escalation_key, 0.0) >= action_cooldown:
                self._last_spam_action_time[escalation_key] = now
                duration = min(600, self._to_int(getattr(self.config.safeguard, "max_mute_duration", 600)) or 600)
                await self.tool_mute_user(group_id=group_id, user_id=user_id, duration=duration, reason=reason)
                return
            if now - self._last_spam_action_time.get(warn_key, 0.0) >= min(max(window, 60), 300):
                self._last_spam_action_time[warn_key] = now
                await self.tool_warn_user(group_id=group_id, user_id=user_id, violation_type="spam", reason=reason, stream_id=stream_id)
            return
        last_warn = self._last_spam_action_time.get(warn_key, 0.0)
        if now - last_warn < min(max(window, 60), 300):
            return
        self._last_spam_action_time[warn_key] = now
        await self.tool_warn_user(group_id=group_id, user_id=user_id, violation_type="spam", reason=reason, stream_id=stream_id)

    def _normalize_audit_mute_duration(self, violation_type: str, duration: int) -> int:
        if 0 < duration < 60:
            duration *= 60
        defaults = {
            "spam": 600,
            "ad": 900,
            "abuse": 1800,
            "sexual": 600,
            "illegal": 3600,
        }
        ranges = {
            "spam": (300, 600),
            "ad": (600, 1800),
            "abuse": (600, 3600),
            "sexual": (300, 3600),
            "illegal": (3600, 3600),
        }
        if duration <= 0:
            duration = defaults.get(violation_type, 1800)
        min_duration, max_duration = ranges.get(violation_type, (60, 1800))
        duration = max(duration, min_duration)
        duration = min(duration, max_duration)
        max_allowed = self._to_int(getattr(self.config.safeguard, "max_mute_duration", 3600)) or 3600
        return min(duration, max_allowed)

    async def _run_llm_moderation(self, group_id: int, user_id: int, text: str, stream_id: str = "", message_id: str = "", task_key: tuple[Any, ...] | None = None, forwarded_record_single_message: bool = False, history_snapshot: list[tuple[float, str]] | None = None) -> None:
        try:
            is_protected, msg = await self._is_protected(group_id, user_id)
            if is_protected:
                if self.config.logging.verbose_logging:
                    self.ctx.logger.info(f"[群管理] 入站审核跳过受保护用户: group={group_id} user={user_id} reason={msg}")
                return
            history = history_snapshot if history_snapshot is not None else list(self._recent_user_messages.get((group_id, user_id), []))
            prompt = self._build_audit_prompt(group_id, user_id, text, history, forwarded_record_single_message)
            audit_model = str(getattr(self.config.auto_moderate, "audit_model", "planner") or "planner").strip()
            audit_max_tokens = self._to_int(getattr(self.config.auto_moderate, "audit_max_tokens", 220)) or 220
            result = await self._llm_generate(prompt, audit_model, temperature=0.0, max_tokens=audit_max_tokens)
            if not isinstance(result, dict) or not result.get("success", True):
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
            try:
                threshold = float(getattr(self.config.auto_moderate, "audit_confidence_threshold", 0.72))
            except Exception:
                threshold = 0.72
            threshold = max(0.0, min(threshold, 1.0))
            recall_raw = verdict.get("recall", False)
            recall_message = recall_raw is True or str(recall_raw).strip().lower() in ("true", "1", "yes")
            if self.config.logging.verbose_logging:
                self.ctx.logger.info(f"[群管理] 入站LLM审核结论: group={group_id} user={user_id} action={action} type={violation_type} confidence={confidence:.2f} recall={recall_message} reason={reason}")
            if action not in ("warn", "mute"):
                return
            if bool(getattr(self.config.auto_moderate, "audit_confidence_gate", True)) and confidence < threshold:
                return
            if violation_type not in ("spam", "abuse", "ad", "sexual", "illegal", "conflict"):
                return
            if any(marker in reason for marker in ("未涉及违规", "不涉及违规", "无违规", "没有违规", "未发现违规", "正常聊天", "正常内容", "普通聊天", "无需处理", "无需处罚", "不需要处理", "不应处罚")):
                return
            if violation_type == "conflict":
                violation_type = "abuse"
            if action == "warn":
                await self.tool_warn_user(group_id=group_id, user_id=user_id, violation_type=violation_type, reason=reason, stream_id=stream_id)
            else:
                duration = self._to_int(verdict.get("duration", 0))
                duration = self._normalize_audit_mute_duration(violation_type, duration)
                await self.tool_mute_user(group_id=group_id, user_id=user_id, duration=duration, reason=reason)
            if recall_message and getattr(self.config.moderation_behavior, "auto_recall", False):
                await self._maybe_recall_audited_message(group_id, message_id, reason)
            if getattr(self.config.moderation_behavior, "trigger_moderation_reply", True):
                await self._trigger_native_moderation_reply(stream_id, group_id, action, violation_type, reason)
        except Exception as e:
            self.ctx.logger.error(f"[群管理] 入站LLM审核异常: {e}", exc_info=True)
        finally:
            self._audit_tasks.pop(task_key or (group_id, user_id), None)

    def _schedule_llm_moderation(self, group_id: int, user_id: int, text: str, message_id: str = "", stream_id: str = "", audit_kind: str = "text", forwarded_record_single_message: bool = False) -> None:
        if not text or group_id <= 0 or user_id <= 0:
            return
        seen_key = f"{audit_kind}:{message_id}" if message_id else ""
        if seen_key:
            if seen_key in self._audit_seen_messages:
                return
            self._audit_seen_messages[seen_key] = time.time()
        key: tuple[Any, ...] = (group_id, user_id) if audit_kind == "text" else (group_id, user_id, audit_kind, message_id or str(time.time()))
        history_key = key if audit_kind != "text" else (group_id, user_id)
        history_maxlen = self._recent_message_history_maxlen()
        history = self._recent_user_messages.setdefault(history_key, deque(maxlen=history_maxlen))
        if history.maxlen is not None and history.maxlen < history_maxlen:
            history = deque(history, maxlen=history_maxlen)
            self._recent_user_messages[history_key] = history
        history_snapshot = list(history)
        history_text = "[QQ合并转发聊天记录，按单条消息计入近期历史]" if forwarded_record_single_message else text
        existing = self._audit_tasks.get(key)
        if existing and not existing.done():
            history.append((time.time(), history_text))
            if audit_kind == "text":
                self._schedule_spam_threshold_moderation(group_id, user_id, stream_id)
            return
        history.append((time.time(), history_text))
        if audit_kind == "text":
            self._schedule_spam_threshold_moderation(group_id, user_id, stream_id)
        self._audit_tasks[key] = asyncio.create_task(
            self._run_llm_moderation(group_id, user_id, text, stream_id, message_id, key, forwarded_record_single_message, history_snapshot)
        )

    async def _remember_recent_group_manager_speaker(self, group_id: int, user_id: int, bot_id: int = 0) -> None:
        if group_id <= 0 or user_id <= 0 or (bot_id and user_id == bot_id):
            return
        role = await self._check_target_role(group_id, user_id)
        if role not in ("owner", "admin"):
            return
        self._recent_group_managers.setdefault(group_id, deque(maxlen=12)).append((time.time(), user_id, role))

    def _find_recent_group_manager_for_notice(self, group_id: int, exclude_user_id: int = 0, bot_id: int = 0) -> int:
        speakers = self._recent_group_managers.get(group_id)
        if not speakers:
            return 0
        for item in reversed(speakers):
            uid = item[1]
            if uid > 0 and uid != exclude_user_id and (not bot_id or uid != bot_id):
                return uid
        return 0

    async def _find_group_owner_for_notice(self, group_id: int, bot_id: int = 0) -> int:
        ok, data = await self._call_api(api_name="adapter.napcat.group.get_group_member_list", group_id=group_id)
        members = data.get("data") if isinstance(data, dict) else data
        if ok and isinstance(members, list):
            for member in members:
                if not isinstance(member, dict):
                    continue
                if str(member.get("role", "")).strip().lower() != "owner":
                    continue
                owner_id = self._to_int(member.get("user_id") or member.get("userId") or member.get("qq") or member.get("uin"))
                if owner_id > 0 and (not bot_id or owner_id != bot_id):
                    self._known_roles[(group_id, owner_id)] = ("owner", time.time())
                    return owner_id
        return 0

    @staticmethod
    def _format_review_required_kind(type_counts: dict[str, int]) -> str:
        image_count = int(type_counts.get("图片", 0) or 0)
        emoji_count = int(type_counts.get("表情包", 0) or 0)
        if image_count and emoji_count:
            return "图片/表情包"
        if emoji_count:
            return "表情包"
        return "图片"

    @staticmethod
    def _format_review_required_kind_detail(type_counts: dict[str, int]) -> str:
        parts: list[str] = []
        image_count = int(type_counts.get("图片", 0) or 0)
        emoji_count = int(type_counts.get("表情包", 0) or 0)
        if image_count:
            parts.append(f"{image_count}张图片")
        if emoji_count:
            parts.append(f"{emoji_count}个表情包")
        return "和".join(parts) if parts else "0张图片/表情包"

    async def _generate_violation_media_review_notice(self, review_required_count: int, review_required_kind: str = "图片", review_required_kind_detail: str = "") -> str:
        review_required_kind = str(review_required_kind or "图片").strip() or "图片"
        review_required_kind_detail = str(review_required_kind_detail or "").strip() or f"{review_required_count}个{review_required_kind}"
        fallback = f" 有{review_required_kind_detail}需要人工复核，麻烦看一下要不要处理。"
        if not self._persona_comments_enabled():
            return fallback
        prompt_template = str(getattr(self.config.prompts, "violation_media_notice_prompt", "") or "").strip()
        if not prompt_template:
            prompt_template = PromptsSectionConfig().violation_media_notice_prompt
        system_prompt = await self._build_host_persona_context()
        try:
            prompt = prompt_template.format(
                bot_style_context="请严格遵守系统消息中的 MaiBot 身份、人格设定、表达方式与群管理呼叫任务边界。",
                bot_nickname=str(getattr(self.config.identity, "bot_nickname", "") or "麦麦"),
                review_required_count=review_required_count,
                review_required_kind=review_required_kind,
                review_required_kind_detail=review_required_kind_detail,
            )
        except Exception:
            prompt = PromptsSectionConfig().violation_media_notice_prompt.format(
                bot_style_context="请严格遵守系统消息中的 MaiBot 身份、人格设定、表达方式与群管理呼叫任务边界。",
                bot_nickname=str(getattr(self.config.identity, "bot_nickname", "") or "麦麦"),
                review_required_count=review_required_count,
                review_required_kind=review_required_kind,
                review_required_kind_detail=review_required_kind_detail,
            )
        try:
            model = self._persona_comments_model()
            result = await self._llm_generate(f"{system_prompt}\n\n{prompt}", model, temperature=0.5, max_tokens=100)
            if isinstance(result, dict) and result.get("success", True):
                notice = self._normalize_persona_comment(str(result.get("response", "")), max_chars=100)
                if notice:
                    return " " + notice
        except Exception as e:
            self.ctx.logger.warning(f"[群管理] 生成管理员通知失败: {e}")
        return fallback

    async def _resolve_violation_media_notify_target(self, group_id: int, user_id: int, bot_id: int = 0) -> int:
        target = str(getattr(self.config.media_audit, "violation_media_notify_target", "") or "").strip().lower()
        if not target:
            target = "admin_or_owner"
        if target.isdigit():
            return self._to_int(target)
        if target == "admin":
            return self._find_recent_group_manager_for_notice(group_id, exclude_user_id=user_id, bot_id=bot_id)
        if target == "owner":
            return await self._find_group_owner_for_notice(group_id, bot_id=bot_id)
        if target == "admin_or_owner":
            manager_id = self._find_recent_group_manager_for_notice(group_id, exclude_user_id=user_id, bot_id=bot_id)
            if manager_id:
                return manager_id
            return await self._find_group_owner_for_notice(group_id, bot_id=bot_id)
        self.ctx.logger.warning(f"[群管理] violation_media_notify_target 无效: {target}")
        return 0

    async def _notify_violation_media_review_target(self, group_id: int, user_id: int, stream_id: str, message_id: str, review_required_count: int, review_required_summary: str = "", review_required_kind: str = "图片", review_required_kind_detail: str = "") -> bool:
        bot_id = self._to_int(self.config.identity.bot_qq) or self._bot_self_id or 0
        manager_id = await self._resolve_violation_media_notify_target(group_id, user_id, bot_id=bot_id)
        if not manager_id:
            return False
        target_stream = await self._resolve_group_stream_id(group_id, stream_id)
        if not target_stream:
            return False
        suffix = await self._generate_violation_media_review_notice(review_required_count, review_required_kind, review_required_kind_detail)
        await self._send_at_text(target_stream, "", manager_id, suffix)
        if self.config.logging.verbose_logging:
            self.ctx.logger.info(f"[群管理] 已通知人工复核违规图片/表情: group={group_id} user={user_id} manager={manager_id} mid={message_id} review_required={review_required_count} details={review_required_summary}")
        return True

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
        await self._send_persona_text(stream_id, f"未找到成员: {target} (请使用QQ号)", "command_failure", f"未找到成员 {target}，提示使用QQ号")
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
                await self._send_persona_text(stream_id, self.config.prompts.command_denied_message, "permission_denied", "没有权限执行该管理命令")
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
                    await self._send_persona_text(stream_id, self.config.prompts.command_denied_message, "permission_denied", "没有权限执行该管理命令")
                return False
        if deny_mode == "reply":
            await self._send_persona_text(stream_id, self.config.prompts.command_denied_message, "permission_denied", "没有权限执行该管理命令")
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
        self._recent_user_messages.clear()
        self._recent_group_managers.clear()
        for task in list(self._audit_tasks.values()):
            if task and not task.done():
                task.cancel()
        self._audit_tasks.clear()
        self._audit_seen_messages.clear()
        self._seen_emoji_hashes.clear()
        self._last_spam_action_time.clear()
        self._host_persona_context = ""
        self._host_reply_style = ""
        self._host_persona_cached_at = 0.0
