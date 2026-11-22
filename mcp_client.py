"""
通用 stdio MCP 客户端

通过子进程 stdin/stdout 与 MCP server 通信（JSON-RPC 2.0，逐行）。
用于把本机已有的 MCP server（如 metabase-cards-mcp）接进数字分身，
让群里的自然语言指令能调用它们暴露的工具。

设计约束：
- 一个 MCPClient 实例 = 一个长驻子进程。进程复用，避免每次调用都冷启动。
- stdout 只允许出现 JSON-RPC 消息；server 的日志应走 stderr（约定）。
- 请求-响应靠递增的 id 配对，带超时，超时后该连接作废（协议已无法可靠恢复）。

不依赖第三方 MCP SDK，只用标准库 asyncio + json。
"""

import asyncio
import json
import logging
import os
from typing import Any

logger = logging.getLogger("mcp_client")


class MCPError(Exception):
    """MCP 调用失败（协议错误、工具报错、超时、进程异常等）。"""


class MCPClient:
    def __init__(self, command: list[str], cwd: str | None = None, env: dict | None = None):
        """
        command: 启动 MCP server 的完整命令，如
            ["/usr/bin/python3", "/path/to/server.py"]
        cwd:     子进程工作目录
        env:     额外环境变量（会叠加到当前环境上）
        """
        self.command = command
        self.cwd = cwd
        self.extra_env = env or {}
        self._proc: asyncio.subprocess.Process | None = None
        self._next_id = 0
        self._pending: dict[int, asyncio.Future] = {}
        self._reader_task: asyncio.Task | None = None
        self._lock = asyncio.Lock()
        self._tools: list[dict] | None = None

    # ---------- 生命周期 ----------
    async def start(self) -> None:
        """启动子进程并完成 initialize 握手。幂等。"""
        if self._proc is not None and self._proc.returncode is None:
            return
        env = os.environ.copy()
        env.update(self.extra_env)
        logger.info("启动 MCP server: %s", " ".join(self.command))
        self._proc = await asyncio.create_subprocess_exec(
            *self.command,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=self.cwd,
            env=env,
        )
        self._reader_task = asyncio.create_task(self._read_loop())
        await self._call(
            "initialize",
            {
                "protocolVersion": "2025-06-18",
                "capabilities": {},
                "clientInfo": {"name": "digital-twin", "version": "0.1.0"},
            },
        )

    async def stop(self) -> None:
        """关闭子进程并清理。"""
        if self._reader_task:
            self._reader_task.cancel()
            try:
                await self._reader_task
            except (asyncio.CancelledError, Exception):
                pass
            self._reader_task = None
        if self._proc and self._proc.returncode is None:
            try:
                self._proc.stdin.close()
            except Exception:
                pass
            try:
                self._proc.terminate()
                await asyncio.wait_for(self._proc.wait(), timeout=5)
            except Exception:
                try:
                    self._proc.kill()
                except Exception:
                    pass
        self._proc = None
        self._tools = None

    # ---------- 读取循环 ----------
    async def _read_loop(self) -> None:
        assert self._proc and self._proc.stdout
        while True:
            line = await self._proc.stdout.readline()
            if not line:
                # 子进程 stdout 关闭 = 进程退出
                self._fail_all(f"MCP server 已退出（code={self._proc.returncode}）")
                return
            line = line.strip()
            if not line:
                continue
            try:
                msg = json.loads(line)
            except ValueError:
                logger.warning("MCP 输出非法 JSON: %s", line[:200])
                continue
            msg_id = msg.get("id")
            if msg_id is None:
                # 通知（无 id），当前 server 不发通知，忽略
                continue
            fut = self._pending.pop(msg_id, None)
            if fut is None or fut.done():
                continue
            if "error" in msg:
                err = msg["error"]
                fut.set_exception(MCPError(f"{err.get('message')} (code={err.get('code')})"))
            else:
                fut.set_result(msg.get("result"))

    def _fail_all(self, reason: str) -> None:
        for fut in self._pending.values():
            if not fut.done():
                fut.set_exception(MCPError(reason))
        self._pending.clear()

    # ---------- 基础调用 ----------
    async def _call(self, method: str, params: dict | None = None, timeout: float = 60.0) -> Any:
        if self._proc is None or self._proc.returncode is not None:
            raise MCPError("MCP server 未启动")
        self._next_id += 1
        msg_id = self._next_id
        payload = {"jsonrpc": "2.0", "id": msg_id, "method": method, "params": params or {}}
        fut: asyncio.Future = asyncio.get_running_loop().create_future()
        self._pending[msg_id] = fut
        try:
            self._proc.stdin.write((json.dumps(payload, ensure_ascii=False) + "\n").encode("utf-8"))
            await self._proc.stdin.drain()
        except Exception as e:
            self._pending.pop(msg_id, None)
            raise MCPError(f"写入 MCP server 失败：{e}") from e
        try:
            return await asyncio.wait_for(fut, timeout=timeout)
        except asyncio.TimeoutError:
            self._pending.pop(msg_id, None)
            raise MCPError(f"MCP 调用超时（>{timeout}s）") from None

    # ---------- 面向业务的能力 ----------
    async def list_tools(self, force: bool = False) -> list[dict]:
        """返回 server 暴露的工具列表（带缓存）。"""
        if self._tools is not None and not force:
            return self._tools
        result = await self._call("tools/list")
        tools = result.get("tools", [])
        self._tools = tools
        return tools

    async def call_tool(self, name: str, arguments: dict, timeout: float = 60.0) -> str:
        """调用一个工具，返回其文本内容（content[].text 拼接）。"""
        try:
            result = await self._call("tools/call", {"name": name, "arguments": arguments}, timeout=timeout)
        except MCPError as e:
            # 超时/进程异常：包装成对 LLM 有引导意义的信息，避免它反复重试同一张重卡
            raise MCPError(f"工具 {name} 调用失败（{e}）。若为超时，说明该查询太重，请改用更轻量的卡片、"
                           f"缩小时间范围或减少返回行数，不要用相同参数重试。") from e
        texts = []
        for item in result.get("content", []):
            if item.get("type") == "text":
                texts.append(item.get("text", ""))
        text = "\n".join(t for t in texts if t).strip()
        if not text and result.get("isError"):
            raise MCPError("工具返回错误（无文本内容）")
        if not text:
            # 有的 server 只给 structuredContent
            sc = result.get("structuredContent")
            if sc is not None:
                text = json.dumps(sc, ensure_ascii=False, indent=2)
        return text or "(空结果)"

    # ---------- 便捷：schema 转换 ----------
    def tools_as_openai(self, tools: list[dict]) -> list[dict]:
        """
        把 MCP 的 inputSchema 工具转成 OpenAI function-calling 格式，
        供 llm_runner 使用。MCP 名保留原样（如 mb_run_card）。
        """
        out = []
        for t in tools:
            out.append(
                {
                    "type": "function",
                    "function": {
                        "name": t["name"],
                        "description": (t.get("description") or "")[:1000],
                        "parameters": t.get("inputSchema") or {"type": "object", "properties": {}},
                    },
                }
            )
        return out
