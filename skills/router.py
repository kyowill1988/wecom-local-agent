"""
指令路由：把群里的自然语言映射到具体能力。

命中内置指令 → 走确定性代码（快、稳、无需 LLM）
未命中       → 交给 WorkBuddy 自由发挥
"""

import re
import shlex

# 指令关键字（支持中英文）
CMD_HELP = {"help", "帮助", "?", "？", "菜单", "能做什么"}
CMD_COMMITS = {"commits", "commit", "提交", "提交记录", "最近提交", "git", "log"}
CMD_PROJECTS = {"projects", "项目", "找项目", "搜项目", "项目列表"}
CMD_BRANCHES = {"branches", "branch", "分支", "分支列表"}
CMD_PIPELINES = {"pipelines", "pipeline", "流水线", "ci", "构建"}
CMD_STATUS = {"status", "状态", "健康检查", "ping"}


def strip_mention(text: str, bot_name: str = "") -> str:
    """去掉消息里的 @机器人 前缀（企微会把 @名字 作为文本的一部分）。"""
    if not text:
        return ""
    t = text.strip()
    # 去掉 @数字分身 / @RobotA 这类前缀（可重复出现）
    t = re.sub(r"^(@[^\s@：:，,]+[\s：:,，]*)", "", t).strip()
    if bot_name:
        t = re.sub(rf"^@?{re.escape(bot_name)}[\s：:,，]*", "", t).strip()
    return t


def parse(text: str) -> tuple[str, list[str]]:
    """
    解析消息文本，返回 (指令名, 参数列表)。
    未识别时返回 ("chat", [原文])。
    """
    t = (text or "").strip()
    if not t:
        return "empty", []

    try:
        parts = shlex.split(t)
    except ValueError:
        parts = t.split()

    if not parts:
        return "empty", []

    head = parts[0].strip("，,。.！!").lower()
    args = parts[1:]

    if head in CMD_HELP:
        return "help", args
    if head in CMD_COMMITS:
        return "commits", args
    if head in CMD_PROJECTS:
        return "projects", args
    if head in CMD_BRANCHES:
        return "branches", args
    if head in CMD_PIPELINES:
        return "pipelines", args
    if head in CMD_STATUS:
        return "status", args

    return "chat", [t]


def parse_commits_args(args: list[str], default_project: str, default_branch: str, default_limit: int):
    """
    commits 指令参数解析：
      commits <项目> [分支] [条数]
      commits                      → 用配置里的默认值
      commits 5                    → 默认项目 + 默认分支 + 5 条
    返回 (project, branch, limit)
    """
    project, branch, limit = default_project, default_branch, default_limit
    positional = []

    for a in args:
        if a.isdigit():
            limit = int(a)
        elif a.startswith("--branch="):
            branch = a.split("=", 1)[1]
        elif a.startswith("--limit="):
            limit = int(a.split("=", 1)[1])
        elif a.startswith("--project="):
            project = a.split("=", 1)[1]
        elif a.startswith("-"):
            continue
        else:
            positional.append(a)

    if positional:
        project = positional[0]
    if len(positional) > 1:
        branch = positional[1]
    if len(positional) > 2 and positional[2].isdigit():
        limit = int(positional[2])

    limit = max(1, min(limit, 30))  # 防止刷屏
    return project, branch, limit


HELP_TEXT = """### 🤖 数字分身 · 可用指令

**内置能力（即时返回）**
• `commits <项目> [分支] [条数]` — 查看最近提交记录
　例：`commits fund-product` ／ `commits fund-product main 5`
• `projects <关键词>` — 搜索 GitLab 项目
• `branches <项目>` — 列出项目分支
• `pipelines <项目> [分支]` — 查看流水线状态
• `status` — 机器人与 GitLab 连通性自检
• `help` — 显示本帮助

**通用能力（交给 WorkBuddy 处理）**
直接把任务描述发给我即可，例如：
　「帮我看看 fund-product 项目最近的改动」
　「总结一下今天的工作重点」

> 直接 @我 并发送指令，我会先回复「已收到」，处理完成后再把结果推送到本群。
"""
