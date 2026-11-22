"""
WorkBuddy（CodeBuddy CLI）执行器

把群里的自然语言指令交给 WorkBuddy 处理，并取回结果。
使用 -p/--print 非交互模式，配合 --permission-mode 与 --allowedTools 收敛权限。
"""

import asyncio
import json
import logging
import os
import shlex
from pathlib import Path

logger = logging.getLogger("executor")

DEFAULT_CLI = "/Applications/WorkBuddy.app/Contents/Resources/app.asar.unpacked/cli/bin/codebuddy"


class WorkBuddyExecutor:
    def __init__(
        self,
        cli_path: str = DEFAULT_CLI,
        model: str = "",
        timeout: int = 150,
        allowed_tools: str = "",
        workdir: str = "",
    ):
        self.cli_path = cli_path
        self.model = model
        self.timeout = timeout
        self.allowed_tools = allowed_tools
        self.workdir = workdir or str(Path.home() / "digital-twin" / "workspace")

    def available(self) -> tuple[bool, str]:
        if not os.path.exists(self.cli_path):
            return False, f"未找到 CodeBuddy CLI：{self.cli_path}"
        if not os.access(self.cli_path, os.X_OK):
            return False, f"CodeBuddy CLI 不可执行：{self.cli_path}"
        return True, "ok"

    def build_cmd(self, prompt: str) -> list[str]:
        cmd = [self.cli_path, "-p", "--output-format", "json"]
        if self.model:
            cmd += ["--model", self.model]
        # 收敛权限：自动接受文件编辑，但不绕过全部安全检查
        cmd += ["--permission-mode", "acceptEdits"]
        if self.allowed_tools:
            cmd += ["--allowedTools", self.allowed_tools]
        cmd.append(prompt)
        return cmd

    async def run(self, prompt: str, on_progress=None) -> dict:
        """
        异步执行一条指令。
        返回 {"ok": bool, "text": str, "error": str|None, "elapsed": float}
        """
        ok, msg = self.available()
        if not ok:
            return {"ok": False, "text": "", "error": msg, "elapsed": 0}

        Path(self.workdir).mkdir(parents=True, exist_ok=True)
        cmd = self.build_cmd(prompt)
        logger.info(f"执行 WorkBuddy: {shlex.join(cmd[:6])} ... <prompt {len(prompt)} chars>")

        env = os.environ.copy()
        env.setdefault("NO_COLOR", "1")
        # 重要：不要设置 CI=1 —— 会让 CLI 在初始化阶段卡死（实测）。
        # CLI 依赖 WorkBuddy 桌面端的登录态，需保留完整的用户环境。
        env.pop("CI", None)
        env.setdefault("CODEBUDDY_FORCE_HEADLESS_BUNDLE", "1")

        start = asyncio.get_event_loop().time()
        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=self.workdir,
                env=env,
            )
            try:
                stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=self.timeout)
            except asyncio.TimeoutError:
                proc.kill()
                await proc.communicate()
                return {
                    "ok": False,
                    "text": "",
                    "error": f"执行超时（>{self.timeout}s），任务可能仍在后台运行。建议把指令拆得更小。",
                    "elapsed": self.timeout,
                }
        except Exception as e:
            return {"ok": False, "text": "", "error": f"启动 WorkBuddy 失败：{e}", "elapsed": 0}

        elapsed = asyncio.get_event_loop().time() - start
        raw = stdout.decode("utf-8", errors="replace").strip()
        err = stderr.decode("utf-8", errors="replace").strip()

        text = self._parse_output(raw)
        if text:
            return {"ok": True, "text": text, "error": None, "elapsed": elapsed}
        if err:
            return {"ok": False, "text": "", "error": err[:800], "elapsed": elapsed}
        return {"ok": False, "text": "", "error": f"WorkBuddy 未返回内容（exit={proc.returncode}）", "elapsed": elapsed}

    @staticmethod
    def _parse_output(raw: str) -> str:
        """优先按 JSON 解析（--output-format json），失败则当作纯文本。"""
        if not raw:
            return ""
        # JSON 模式可能输出单个对象或 NDJSON 多行
        candidates = [raw]
        if "\n" in raw:
            candidates = [l for l in raw.splitlines() if l.strip().startswith("{")] + [raw]
        for c in candidates:
            try:
                data = json.loads(c)
            except Exception:
                continue
            if isinstance(data, dict):
                for key in ("result", "text", "content", "response", "answer"):
                    v = data.get(key)
                    if isinstance(v, str) and v.strip():
                        return v.strip()
                if data.get("is_error"):
                    return ""
                # 兜底：把整个对象压缩输出
                return json.dumps(data, ensure_ascii=False)[:4000]
        return raw


SYSTEM_GUARD = """你是运行在企业微信群里的数字分身助手。请注意：
1. 回答简洁，控制在 300 字以内，适合在聊天窗口阅读。
2. 不要输出代码块里的大段内容，除非用户明确要求。
3. 使用纯文本或简单的 markdown 排版，不要使用 HTML。
4. 如果用户的问题需要访问本机资源，你可以使用工具；不要编造事实。
"""


def wrap_prompt(user_text: str) -> str:
    return f"{SYSTEM_GUARD}\n\n用户指令：{user_text}"
