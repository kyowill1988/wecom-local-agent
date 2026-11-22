"""
LLM 后端：直连大模型 API（OpenAI 兼容格式）处理自然语言指令。

不依赖 WorkBuddy 桌面端进程，适合作为常驻服务的推理后端。
模型可通过 function calling 调用本机能力（GitLab 查询等），因此
「帮我看看 fund-product 最近的改动」这类自然语言也能正确执行。
"""

import json
import logging
from typing import Callable

import requests

logger = logging.getLogger("llm")

# 暴露给模型的工具（schema 为 OpenAI function calling 格式）
TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "gitlab_commits",
            "description": "查询 GitLab 指定项目、指定分支的最近提交记录",
            "parameters": {
                "type": "object",
                "properties": {
                    "project": {"type": "string", "description": "项目名或项目 ID 或完整路径"},
                    "branch": {"type": "string", "description": "分支名，默认 master"},
                    "limit": {"type": "integer", "description": "返回条数，默认 10，最多 30"},
                },
                "required": ["project"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "gitlab_search_projects",
            "description": "按关键词搜索 GitLab 项目，用于把用户说的项目名解析成真实项目路径",
            "parameters": {
                "type": "object",
                "properties": {"keyword": {"type": "string", "description": "搜索关键词"}},
                "required": ["keyword"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "gitlab_branches",
            "description": "列出指定项目的分支",
            "parameters": {
                "type": "object",
                "properties": {"project": {"type": "string", "description": "项目名或 ID"}},
                "required": ["project"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "gitlab_pipelines",
            "description": "查看指定项目指定分支最近的流水线（CI）运行状态",
            "parameters": {
                "type": "object",
                "properties": {
                    "project": {"type": "string", "description": "项目名或 ID"},
                    "branch": {"type": "string", "description": "分支名，默认 master"},
                },
                "required": ["project"],
            },
        },
    },
]

SYSTEM_PROMPT = """你是运行在企业微信群里、名为「数字分身」的助手。

行为准则：
1. 优先使用工具获取真实数据，绝不编造提交记录、分支名或流水线状态。
2. 用户提到项目名但不确定真实路径时，先用 gitlab_search_projects 搜索确认。
3. 回答简洁，控制在 300 字以内，适合聊天窗口阅读。
4. 使用简单的 markdown 排版，不要用 HTML 和表格。
5. 用中文回答。

调用 Metabase 工具（mb_*）时的取数纪律：
6. 查「数量/汇总」类指标时，先用 mb_search_cards 搜「汇总」「统计」「数量」「列表」等关键词，
   优先选汇总卡，不要选「健康度」「明细」「日志」这类重分析卡——它们查询很慢，可能超时。
7. 执行卡片前必须先 mb_describe_card 确认参数名（大小写敏感）和必填项；缺必填参数就回问用户，不要瞎猜。
8. mb_run_card 默认 limit 50、mask=true 就够；不要拉全量（limit 不要超过 200），不要关脱敏（mask 保持 true）。
   需要全量明细时改用 mb_export_card。
9. 某个工具超时或报错，说明该查询太重，换更轻量的卡片或缩小范围，不要用相同参数重试。
10. 同一张卡连续失败两次就放弃，向用户说明查不到的原因，而不是无限换卡重试。
"""


class LLMBackend:
    def __init__(self, base_url: str, api_key: str, model: str, max_rounds: int = 12, timeout: int = 90):
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.model = model
        self.max_rounds = max_rounds
        self.timeout = timeout
        # 额外的工具 schema（OpenAI function-calling 格式），由 agent 在运行时注入
        # （如本机 MCP server 暴露的工具）。与内置 TOOLS 合并后一起交给模型。
        self.extra_tools: list[dict] = []

    def available(self) -> tuple[bool, str]:
        if not self.api_key:
            return False, "未配置 llm.api_key"
        if not self.base_url:
            return False, "未配置 llm.base_url"
        return True, "ok"

    def _chat(self, messages: list[dict], tools: list[dict] | None) -> dict:
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }
        payload = {"model": self.model, "messages": messages}
        if tools:
            payload["tools"] = tools
            payload["tool_choice"] = "auto"
        r = requests.post(
            f"{self.base_url}/chat/completions",
            headers=headers,
            json=payload,
            timeout=self.timeout,
        )
        if r.status_code >= 400:
            raise RuntimeError(f"LLM 接口返回 HTTP {r.status_code}: {r.text[:300]}")
        return r.json()

    async def run(self, prompt: str, tool_impl: dict[str, Callable[[dict], str]]) -> dict:
        """
        执行一次对话（可多轮工具调用）。
        tool_impl: {工具名: 实现函数}，函数接收参数 dict，返回字符串结果。
        """
        ok, msg = self.available()
        if not ok:
            return {"ok": False, "text": "", "error": msg}

        messages = [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": prompt},
        ]

        import asyncio

        tools = TOOLS + list(self.extra_tools)

        for round_no in range(1, self.max_rounds + 1):
            try:
                data = await asyncio.to_thread(self._chat, messages, tools)
            except Exception as e:
                return {"ok": False, "text": "", "error": f"调用大模型失败：{e}"}

            try:
                choice = data["choices"][0]
                msg = choice["message"]
            except (KeyError, IndexError):
                return {"ok": False, "text": "", "error": f"大模型返回格式异常：{json.dumps(data, ensure_ascii=False)[:300]}"}

            tool_calls = msg.get("tool_calls") or []
            if not tool_calls:
                content = (msg.get("content") or "").strip()
                return {"ok": bool(content), "text": content, "error": None if content else "模型返回空内容"}

            # 执行工具调用
            messages.append(msg)
            for tc in tool_calls:
                fn = tc.get("function", {})
                name = fn.get("name")
                try:
                    args = json.loads(fn.get("arguments") or "{}")
                except json.JSONDecodeError:
                    args = {}
                logger.info(f"LLM 调用工具: {name}({args})")
                impl = tool_impl.get(name)
                if not impl:
                    result = f"错误：未实现的工具 {name}"
                else:
                    try:
                        r = impl(args)
                        # 工具实现可以是同步函数，也可以是协程（如 MCP 异步调用）
                        if asyncio.iscoroutine(r):
                            r = await r
                        result = r
                    except Exception as e:
                        result = f"工具执行出错：{e}"
                messages.append(
                    {"role": "tool", "tool_call_id": tc.get("id"), "content": str(result)[:4000]}
                )

        # 到达轮次上限：不再继续调工具，而是请模型基于已查到的信息给结论
        try:
            messages.append(
                {
                    "role": "user",
                    "content": "工具调用次数已达上限。请基于上面已经查到的数据，直接给出你能确定的结论；"
                    "确实查不到的，明确说明还缺什么信息，不要继续调用工具。",
                }
            )
            data = await asyncio.to_thread(self._chat, messages, None)
            content = (data["choices"][0]["message"].get("content") or "").strip()
            if content:
                return {"ok": True, "text": content, "error": None}
        except Exception:
            pass
        return {"ok": False, "text": "", "error": f"工具调用轮数超过上限（{self.max_rounds}）"}
