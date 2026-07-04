"""群管理助手 v2.1 — LLM 自主管理 QQ 群插件

18 个管理 Tool + 15 条快捷命令 + 5 个 HookHandler，支持禁言/解禁/踢人/警告/设精华/撤回/改名片/
改头衔/改群名/公告发布与删除/入群审批，含 8 步安全护栏 + 按群独立配置。

v2.0 重大更新：多文件模块化架构、Planner 阶段提示词注入、角色感知中文化提示词、
守门回复动态判断角色、Tool 描述规范化。

模块结构：
  config_model.py   配置模型（10 个配置分区，2 个默认提示词）
  plugin_core.py    核心生命周期、后台任务、辅助方法
  tools.py          18 个管理 Tool
  commands.py       15 个管理员命令
  handlers.py       1 个 EventHandler + 5 个 HookHandler
  plugin.py         入口：组合所有模块，导出 create_plugin
"""

from __future__ import annotations

from maibot_sdk import MaiBotPlugin

from .commands import CommandMixin
from .config_model import GroupAdminConfig
from .handlers import HandlerMixin
from .plugin_core import PluginCore
from .tools import ToolMixin


class GroupAdminPlugin(PluginCore, ToolMixin, CommandMixin, HandlerMixin):
    """群管理助手插件 — 组合核心、工具、命令、事件处理器。"""
    config_model = GroupAdminConfig


def create_plugin() -> GroupAdminPlugin:
    """创建群管理助手插件实例。"""
    return GroupAdminPlugin()
