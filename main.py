import asyncio
from datetime import datetime, time
from typing import Dict, List, Optional

from astrbot.api.star import register, Star
from astrbot.api.event import filter, AstrMessageEvent
from astrbot.api.all import Context
from astrbot.api.message_components import Plain


@register("proactive_context", "Architect", "上下文感知主动回复插件", "1.0.0")
class ProactiveContextPlugin(Star):
    """
    上下文感知主动回复插件。
    
    核心功能：
    1. 记录群聊历史消息，实现上下文感知。
    2. 群聊空闲超过设定时间后，自动调用 LLM 生成与话题相关的回复并推送。
    3. 支持免打扰时段，在设定时间段内不会主动打扰用户。
    """

    def __init__(self, context: Context):
        super().__init__(context)
        self.context = context

        # ==================== 配置加载区域（带降级防御） ====================
        try:
            config = context.get_config()
            plugin_conf = getattr(config, 'plugin_config', {}) or {}

            self.prompt_template: str = plugin_conf.get(
                'prompt_template',
                "你是群聊里的一员。请根据以下历史对话记录，自然地延续当前话题，"
                "生成一条简短、有上下文关联的回复。不要生硬问候，不要重复别人的话。\n\n"
                "历史对话：\n{history}\n\n请生成回复（不超过{max_length}字）："
            )
            self.idle_timeout: int = int(plugin_conf.get('idle_timeout_seconds', 300))
            self.history_limit: int = int(plugin_conf.get('history_limit', 15))
            self.dnd_start_str: str = plugin_conf.get('do_not_disturb_start', '23:00')
            self.dnd_end_str: str = plugin_conf.get('do_not_disturb_end', '08:00')
            self.max_length: int = int(plugin_conf.get('max_chat_length', 80))
            self.check_interval: int = int(plugin_conf.get('check_interval_seconds', 30))

        except Exception as load_error:
            print(f"[ProactiveContextPlugin] 配置加载异常，使用默认配置: {load_error}")
            self.prompt_template = (
                "你是群聊里的一员。请根据以下历史对话记录，自然地延续当前话题，"
                "生成一条简短、有上下文关联的回复。不要生硬问候。\n\n"
                "历史对话：\n{history}\n\n请生成回复（不超过{max_length}字）："
            )
            self.idle_timeout = 300
            self.history_limit = 15
            self.dnd_start_str = '23:00'
            self.dnd_end_str = '08:00'
            self.max_length = 80
            self.check_interval = 30

        # ==================== 运行时状态 ====================
        # 群聊历史记录存储
        # 结构: {session_id: [{"role": "user", "content": str, "sender": str, "time": datetime}, ...]}
        self.history_storage: Dict[str, List[dict]] = {}

        # 各会话最后收到消息的时间
        # 结构: {session_id: datetime}
        self.last_active_time: Dict[str, datetime] = {}

        # 各会话对应的适配器实例，用于主动推送消息
        # 结构: {session_id: adapter_object}
        self.session_adapters: Dict[str, object] = {}

        # 后台监控任务句柄
        self.monitor_task: Optional[asyncio.Task] = None

        # 解析免打扰时间
        self.dnd_start_time: time = self._parse_time_str(self.dnd_start_str)
        self.dnd_end_time: time = self._parse_time_str(self.dnd_end_str)

    def _parse_time_str(self, time_str: str) -> time:
        """
        将 HH:MM 格式字符串解析为 time 对象。

        Args:
            time_str: 时间字符串，如 "23:00"

        Returns:
            time 对象，解析失败则返回 00:00
        """
        try:
            hour_str, minute_str = time_str.split(':')
            return time(int(hour_str), int(minute_str))
        except Exception as parse_error:
            print(f"[ProactiveContextPlugin] 时间解析失败 '{time_str}': {parse_error}")
            return time(0, 0)

    async def initialize(self):
        """
        插件初始化钩子。
        启动后台空闲监控协程。
        """
        self.monitor_task = asyncio.create_task(
            self._idle_monitor_loop(),
            name="proactive_context_monitor"
        )
        print("[ProactiveContextPlugin] 后台空闲监控已启动")

    @filter.event_message_type(filter.EventMessageType.ALL)
    async def on_all_message(self, event: AstrMessageEvent):
        """
        监听所有消息事件，用于：
        1. 更新会话历史记录
        2. 记录最后活跃时间
        3. 缓存适配器实例以便后续主动发送

        Args:
            event: AstrBot 消息事件对象
        """
        session_id = event.session_id
        message_text = event.message_str

        # 过滤空消息
        if not message_text or not message_text.strip():
            return

        # 初始化该会话的历史列表
        if session_id not in self.history_storage:
            self.history_storage[session_id] = []

        # 获取发送者名称
        sender_name = "未知用户"
        if hasattr(event, 'sender') and event.sender:
            sender_name = getattr(event.sender, 'nickname', None) or getattr(event.sender, 'user_id', '未知用户')
        elif hasattr(event, 'get_sender_name'):
            sender_name = event.get_sender_name() or "未知用户"

        # 记录消息
        self.history_storage[session_id].append({
            "role": "user",
            "content": message_text.strip(),
            "sender": sender_name,
            "time": datetime.now()
        })

        # 限制历史长度，防止内存无限增长
        if len(self.history_storage[session_id]) > self.history_limit:
            self.history_storage[session_id] = self.history_storage[session_id][-self.history_limit:]

        # 更新最后活跃时间
        self.last_active_time[session_id] = datetime.now()

        # 缓存适配器实例
        if hasattr(event, 'adapter') and event.adapter:
            self.session_adapters[session_id] = event.adapter

    def _is_in_do_not_disturb(self) -> bool:
        """
        判断当前时间是否处于免打扰时段。

        Returns:
            True 表示处于免打扰时段，False 表示可以正常主动回复。
        """
        now_time = datetime.now().time()

        if self.dnd_start_time <= self.dnd_end_time:
            # 不跨天的情况，例如 09:00 - 18:00
            return self.dnd_start_time <= now_time <= self.dnd_end_time
        else:
            # 跨天的情况，例如 23:00 - 08:00
            return now_time >= self.dnd_start_time or now_time <= self.dnd_end_time

    def _format_history_text(self, session_id: str) -> str:
        """
        将指定会话的历史消息格式化为文本。

        Args:
            session_id: 会话标识

        Returns:
            格式化后的历史对话文本
        """
        records = self.history_storage.get(session_id, [])
        if not records:
            return "（暂无历史消息）"

        lines = []
        for record in records:
            time_str = record['time'].strftime('%H:%M')
            lines.append(f"[{time_str}] {record['sender']}: {record['content']}")
        return "\n".join(lines)

    async def _call_llm_for_reply(self, session_id: str) -> str:
        """
        调用当前 LLM 提供商生成上下文感知的回复。

        Args:
            session_id: 会话标识，用于 LLM 上下文追踪

        Returns:
            生成的回复文本，失败返回空字符串
        """
        history_text = self._format_history_text(session_id)

        # 替换提示词模板中的占位符
        prompt = self.prompt_template.replace("{history}", history_text)
        prompt = prompt.replace("{max_length}", str(self.max_length))

        try:
            provider = self.context.get_using_provider()
            if not provider:
                print("[ProactiveContextPlugin] 未找到可用的 LLM 提供商")
                return ""

            # 构建消息上下文，帮助模型理解角色
            contexts = [
                {
                    "role": "system",
                    "content": (
                        "你是群聊中的活跃成员，擅长根据上下文自然接话。"
                        "回复必须简短、有信息量，禁止机械问候。"
                    )
                },
                {
                    "role": "user",
                    "content": prompt
                }
            ]

            # 调用 LLM 接口，直接透传参数，严禁使用 inspect 反射
            response = await provider.text_chat(
                prompt=prompt,
                session_id=session_id,
                contexts=contexts
            )

            if response and hasattr(response, 'completion_text'):
                text = response.completion_text.strip()
                if text:
                    # 字数截断保护
                    if len(text) > self.max_length:
                        text = text[:self.max_length] + "..."
                    return text

        except Exception as llm_error:
            print(f"[ProactiveContextPlugin] LLM 调用异常: {llm_error}")

        return ""

    async def _send_proactive_reply(self, session_id: str):
        """
        向指定会话发送主动生成的回复，并将 Bot 回复也记入历史。

        Args:
            session_id: 目标会话标识
        """
        reply_text = await self._call_llm_for_reply(session_id)
        if not reply_text:
            return

        # 尝试发送消息
        adapter = self.session_adapters.get(session_id)
        if not adapter:
            print(f"[ProactiveContextPlugin] 未找到会话 {session_id} 的适配器，无法主动发送")
            return

        try:
            message_chain = [Plain(reply_text)]
            await adapter.send_message(session_id, message_chain)

            # 将 Bot 的回复也记录到历史中，保证上下文连贯性
            if session_id not in self.history_storage:
                self.history_storage[session_id] = []

            self.history_storage[session_id].append({
                "role": "assistant",
                "content": reply_text,
                "sender": "Bot",
                "time": datetime.now()
            })

            # 维持历史长度限制
            if len(self.history_storage[session_id]) > self.history_limit:
                self.history_storage[session_id] = self.history_storage[session_id][-self.history_limit:]

            print(f"[ProactiveContextPlugin] 已向 {session_id} 发送主动回复")

        except Exception as send_error:
            print(f"[ProactiveContextPlugin] 主动发送消息失败: {send_error}")

    async def _idle_monitor_loop(self):
        """
        后台协程：周期性检测各会话是否达到空闲阈值。
        若满足条件且不在免打扰时段，则触发主动回复。
        """
        while True:
            try:
                # 免打扰时段直接跳过
                if not self._is_in_do_not_disturb():
                    current_time = datetime.now()

                    # 遍历所有记录过活跃时间的会话
                    for session_id, last_time in list(self.last_active_time.items()):
                        elapsed_seconds = (current_time - last_time).total_seconds()

                        if elapsed_seconds >= self.idle_timeout:
                            # 重置该会话的活跃时间，防止连续触发
                            self.last_active_time[session_id] = current_time
                            await self._send_proactive_reply(session_id)

            except Exception as loop_error:
                print(f"[ProactiveContextPlugin] 监控循环异常: {loop_error}")

            # 异步等待，避免阻塞事件循环
            await asyncio.sleep(self.check_interval)

    async def terminate(self):
        """
        插件卸载钩子。
        安全取消后台监控任务。
        """
        if self.monitor_task and not self.monitor_task.done():
            self.monitor_task.cancel()
            try:
                await self.monitor_task
            except asyncio.CancelledError:
                pass
            print("[ProactiveContextPlugin] 后台空闲监控已停止")
