#!/usr/bin/env python3
"""
数字分身 · 企业微信智能机器人守护进程

链路：群里 @机器人 → 企微长连接推送 → 解析指令 → 执行（内置能力 / WorkBuddy）→ 结果推送回群

启动：
    .venv/bin/python agent.py
"""

import asyncio
import json
import logging
import os
import signal
import sys
from datetime import datetime
from pathlib import Path

import yaml

BASE = Path(__file__).resolve().parent
sys.path.insert(0, str(BASE))

from executor import WorkBuddyExecutor, wrap_prompt          # noqa: E402
from llm_runner import LLMBackend                            # noqa: E402
from skills.gitlab_skill import GitLabClient, fmt_commits, fmt_projects  # noqa: E402
from skills import router                                     # noqa: E402
from wecom_client import WecomBot                             # noqa: E402

logger = logging.getLogger("agent")
MAX_PROCESSED = 500


# ---------------- 配置 ----------------
def load_config():
    cfg_path = BASE / "config.yaml"
    if not cfg_path.exists():
        sys.exit(f"缺少配置文件：{cfg_path}")
    with open(cfg_path, "r", encoding="utf-8") as f:
        return yaml.safe_load(f)


def setup_logging(cfg):
    log_file = BASE / cfg["bot"].get("log_file", "logs/bot.log")
    log_file.parent.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        handlers=[
            logging.FileHandler(log_file, encoding="utf-8"),
            logging.StreamHandler(sys.stdout),
        ],
    )


# ---------------- 去重 ----------------
class Dedup:
    def __init__(self, path: Path):
        self.path = path
        self.ids: set[str] = set()
        self._load()

    def _load(self):
        if self.path.exists():
            try:
                self.ids = set(json.loads(self.path.read_text(encoding="utf-8")))
            except Exception:
                self.ids = set()

    def _save(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(list(self.ids)[-MAX_PROCESSED:]), encoding="utf-8")

    def seen(self, msgid: str) -> bool:
        if not msgid:
            return False
        if msgid in self.ids:
            return True
        self.ids.add(msgid)
        if len(self.ids) > MAX_PROCESSED:
            self.ids = set(list(self.ids)[-MAX_PROCESSED:])
        self._save()
        return False


# ---------------- 数字分身 ----------------
class DigitalTwin:
    def __init__(self, cfg: dict):
        self.cfg = cfg
        w = cfg["wecom"]
        self.bot = WecomBot(
            bot_id=w["bot_id"],
            secret=w["secret"],
            ws_url=w.get("ws_url", "wss://openws.work.weixin.qq.com"),
            heartbeat_interval=w.get("heartbeat_interval", 30),
        )
        g = cfg["gitlab"]
        self.gl = GitLabClient(g["url"], g["token"])
        wb = cfg["workbuddy"]
        self.exec = WorkBuddyExecutor(
            cli_path=wb.get("cli_path", ""),
            model=wb.get("model", ""),
            timeout=wb.get("timeout", 150),
            allowed_tools=wb.get("allowed_tools", ""),
        )
        llm_cfg = cfg.get("llm", {})
        self.llm = LLMBackend(
            base_url=llm_cfg.get("base_url", ""),
            api_key=llm_cfg.get("api_key", ""),
            model=llm_cfg.get("model", ""),
            max_rounds=llm_cfg.get("max_tool_rounds", 5),
            timeout=llm_cfg.get("timeout", 90),
        )
        self.runner_mode = cfg.get("runner", {}).get("mode", "auto")
        self.dedup = Dedup(BASE / cfg["bot"].get("state_file", "state/processed.json"))
        self.bot_name = cfg["bot"].get("name", "数字分身")
        self.bot.on("message.text", self.on_text)
        self.bot.on("connected", self.on_connected)

    async def on_connected(self, frame):
        logger.info("长连接建立完成，等待群消息…")

    # ---------- 入口 ----------
    async def on_text(self, frame: dict):
        body = frame.get("body", {})
        msgid = body.get("msgid")
        if self.dedup.seen(msgid):
            logger.info(f"消息 {msgid} 已处理，跳过")
            return

        raw = body.get("text", {}).get("content", "")
        text = router.strip_mention(raw, self.bot_name)
        logger.info(f"指令原文: {raw!r} → 解析后: {text!r}")

        cmd, args = router.parse(text)
        if cmd == "empty":
            return

        # 企微机器人回复窗口 3 分钟：先 ACK，再异步处理
        try:
            await self.bot.reply(frame, "⏳ 收到，正在处理…")
        except Exception as e:
            logger.warning(f"ACK 发送失败: {e}")

        asyncio.create_task(self.handle(frame, cmd, args))

    # ---------- 分发 ----------
    async def handle(self, frame: dict, cmd: str, args: list[str]):
        try:
            if cmd == "help":
                text = router.HELP_TEXT
            elif cmd == "commits":
                text = await self.do_commits(args)
            elif cmd == "projects":
                text = await self.do_projects(args)
            elif cmd == "branches":
                text = await self.do_branches(args)
            elif cmd == "pipelines":
                text = await self.do_pipelines(args)
            elif cmd == "status":
                text = await self.do_status()
            else:
                text = await self.do_chat(args[0] if args else "")
        except Exception as e:
            logger.exception("处理指令时出错")
            text = f"❌ 处理指令时出错：{e}"

        await self.push(frame, text)

    async def push(self, frame: dict, text: str):
        body = frame.get("body", {})
        chat_id = body.get("chatid") or body.get("from", {}).get("userid")
        chat_type = 2 if body.get("chattype") == "group" else 1
        try:
            await self.bot.send_markdown(chat_id, text, chat_type)
        except Exception as e:
            logger.error(f"结果推送失败: {e}")

    # ---------- 内置能力 ----------
    async def do_commits(self, args):
        g = self.cfg["gitlab"]
        project, branch, limit = router.parse_commits_args(
            args, g.get("default_project", ""), g.get("default_branch", "master"), g.get("default_limit", 10)
        )
        if not project:
            return "❌ 未指定项目。用法：`commits <项目名或ID> [分支] [条数]`\n可用 `projects <关键词>` 先搜一下项目。"

        ok, data = await asyncio.to_thread(self.gl.list_commits, project, branch, limit)
        if not ok:
            return f"❌ {data}"
        return fmt_commits(project, branch, data)

    async def do_projects(self, args):
        kw = " ".join(args).strip()
        if not kw:
            return "用法：`projects <关键词>`，例如 `projects fund`"
        ok, data = await asyncio.to_thread(self.gl.search_projects, kw)
        if not ok:
            return f"❌ {data}"
        return fmt_projects(data)

    async def do_branches(self, args):
        project = args[0] if args else self.cfg["gitlab"].get("default_project", "")
        if not project:
            return "用法：`branches <项目名或ID>`"
        ok, data = await asyncio.to_thread(self.gl.list_branches, project)
        if not ok:
            return f"❌ {data}"
        return "### 🌿 分支列表\n\n" + "\n".join(f"• `{b}`" for b in data) if data else f"`{project}` 暂无分支"

    async def do_pipelines(self, args):
        g = self.cfg["gitlab"]
        project = args[0] if args else g.get("default_project", "")
        branch = args[1] if len(args) > 1 else g.get("default_branch", "master")
        if not project:
            return "用法：`pipelines <项目名或ID> [分支]`"
        ok, data = await asyncio.to_thread(self.gl.list_pipelines, project, branch)
        if not ok:
            return f"❌ {data}"
        icon = {"success": "🟢", "failed": "🔴", "running": "🟡", "pending": "⚪️", "canceled": "⚫️"}
        lines = [f"### 🔧 流水线 · {project} `{branch}`", ""]
        for p in data:
            lines.append(f"{icon.get(p['status'], '⚪️')} #{p['id']} — {p['status']}")
        return "\n".join(lines) if data else f"`{project}` 暂无流水线记录"

    async def do_status(self):
        lines = ["### 🩺 自检报告", ""]
        lines.append(f"• 长连接：{'✅ 已连接' if self.bot.authenticated else '❌ 未连接'}")
        ok, msg = self.exec.available()
        lines.append(f"• CLI 后端：{'✅ 可用' if ok else '⚠️ ' + msg}")
        llm_ok, llm_msg = self.llm.available()
        lines.append(f"• LLM 后端：{'✅ 可用（%s）' % self.llm.model if llm_ok else '⚠️ ' + llm_msg}")
        lines.append(f"• 当前模式：`{self.runner_mode}`")
        g = self.cfg["gitlab"]
        if g.get("token"):
            ok2, data = await asyncio.to_thread(self.gl.list_branches, g.get("default_project") or "")
            lines.append(f"• GitLab（{g['url']}）：{'✅ 连通' if ok2 else '⚠️ ' + str(data)}")
        else:
            lines.append(f"• GitLab：⚠️ 未配置 Token")
        lines.append(f"\n> {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
        return "\n".join(lines)

    # ---------- 通用能力 ----------
    def _tool_commits(self, args: dict) -> str:
        g = self.cfg["gitlab"]
        project = args.get("project") or g.get("default_project", "")
        branch = args.get("branch") or g.get("default_branch", "master")
        limit = int(args.get("limit") or g.get("default_limit", 10))
        limit = max(1, min(limit, 30))
        if not project:
            return "错误：未指定项目，请先用 gitlab_search_projects 搜索项目"
        ok, data = self.gl.list_commits(project, branch, limit)
        return fmt_commits(project, branch, data) if ok else f"错误：{data}"

    def _tool_search_projects(self, args: dict) -> str:
        kw = args.get("keyword", "")
        if not kw:
            return "错误：缺少关键词"
        ok, data = self.gl.search_projects(kw)
        return fmt_projects(data) if ok else f"错误：{data}"

    def _tool_branches(self, args: dict) -> str:
        project = args.get("project") or self.cfg["gitlab"].get("default_project", "")
        if not project:
            return "错误：未指定项目"
        ok, data = self.gl.list_branches(project)
        return ("分支：" + ", ".join(data)) if ok else f"错误：{data}"

    def _tool_pipelines(self, args: dict) -> str:
        g = self.cfg["gitlab"]
        project = args.get("project") or g.get("default_project", "")
        branch = args.get("branch") or g.get("default_branch", "master")
        if not project:
            return "错误：未指定项目"
        ok, data = self.gl.list_pipelines(project, branch)
        if not ok:
            return f"错误：{data}"
        return "\n".join(f"#{p['id']} {p['status']} ({p['ref']})" for p in data) or "暂无流水线记录"

    def _tools(self) -> dict:
        return {
            "gitlab_commits": self._tool_commits,
            "gitlab_search_projects": self._tool_search_projects,
            "gitlab_branches": self._tool_branches,
            "gitlab_pipelines": self._tool_pipelines,
        }

    async def do_chat(self, text: str):
        """自然语言指令：按 runner.mode 选择执行后端。"""
        mode = self.runner_mode
        llm_ok, llm_msg = self.llm.available()
        cli_ok, cli_msg = self.exec.available()

        if mode == "llm" or (mode == "auto" and llm_ok):
            if not llm_ok:
                return f"❌ 已指定 llm 后端但未配置：{llm_msg}"
            logger.info("使用 LLM 后端处理")
            res = await self.llm.run(text, self._tools())
            backend = "LLM"
        elif mode == "cli" or (mode == "auto" and cli_ok):
            if not cli_ok:
                return f"❌ 已指定 cli 后端但不可用：{cli_msg}"
            logger.info("使用 CodeBuddy CLI 后端处理")
            res = await self.exec.run(wrap_prompt(text))
            backend = "WorkBuddy"
        else:
            return (
                "⚠️ 通用指令后端均未就绪。\n"
                f"• LLM 后端：{llm_msg}\n"
                f"• CLI 后端：{cli_msg}\n\n"
                "内置指令（`commits` / `projects` / `branches` / `pipelines` / `help`）不受影响，可直接使用。"
            )

        if not res["ok"]:
            return f"❌ 处理失败：{res.get('error')}"
        out = res["text"]
        if len(out.encode("utf-8")) > 3500:
            out = out[:3000] + "\n\n…（内容过长已截断）"
        return f"**🤖 {backend} 回复**\n\n{out}"


# ---------------- 主流程 ----------------
async def main():
    cfg = load_config()
    setup_logging(cfg)

    missing = []
    if not cfg["wecom"].get("bot_id"):
        missing.append("wecom.bot_id")
    if not cfg["wecom"].get("secret"):
        missing.append("wecom.secret")
    if missing:
        logger.error(f"配置缺失：{', '.join(missing)}。请编辑 config.yaml 后重试。")
        sys.exit(1)

    twin = DigitalTwin(cfg)
    logger.info("=" * 60)
    logger.info(f"数字分身启动中 | Bot ID: {cfg['wecom']['bot_id'][:12]}…")
    logger.info(f"GitLab: {cfg['gitlab']['url']} | 默认项目: {cfg['gitlab'].get('default_project') or '(未设置)'}")
    logger.info("=" * 60)

    loop = asyncio.get_running_loop()
    stop = asyncio.Event()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, stop.set)

    task = asyncio.create_task(twin.bot.connect_async())
    await stop.wait()
    logger.info("收到退出信号，正在关闭…")
    twin.bot.disconnect()
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass
    logger.info("已退出")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
