#!/usr/bin/env python3
"""
自测工具：不依赖企业微信 Secret，逐项验证各组件。

用法：
    .venv/bin/python selftest.py           # 全部测试
    .venv/bin/python selftest.py parse     # 只测指令解析
    .venv/bin/python selftest.py gitlab    # 只测 GitLab
    .venv/bin/python selftest.py cli       # 只测 WorkBuddy CLI
    .venv/bin/python selftest.py ws        # 只测长连接鉴权（需要 secret）
"""

import asyncio
import logging
import sys
from pathlib import Path

BASE = Path(__file__).resolve().parent
sys.path.insert(0, str(BASE))

OK, FAIL, WARN = "✅", "❌", "⚠️"


def load_cfg():
    import yaml

    with open(BASE / "config.yaml", "r", encoding="utf-8") as f:
        return yaml.safe_load(f)


# ---------------- 1. 指令解析 ----------------
def test_parse():
    print("\n─── 1. 指令解析 ───")
    from skills import router

    cases = [
        ("commits fund-product", "commits", ["fund-product"]),
        ("commits fund-product main 5", "commits", ["fund-product", "main", "5"]),
        ("提交 my-proj", "commits", ["my-proj"]),
        ("projects fund", "projects", ["fund"]),
        ("help", "help", []),
        ("帮我看看今天的天气", "chat", ["帮我看看今天的天气"]),
        ("@数字分身 commits demo", "commits", ["demo"]),
    ]
    passed = 0
    for text, want_cmd, want_args in cases:
        got_cmd, got_args = router.parse(router.strip_mention(text, "数字分身"))
        ok = got_cmd == want_cmd and got_args == want_args
        print(f"  {OK if ok else FAIL} {text!r} → ({got_cmd}, {got_args})")
        passed += ok

    g = {"default_project": "demo/proj", "default_branch": "master", "default_limit": 10}
    p, b, l = router.parse_commits_args(["myproj", "dev", "3"], g["default_project"], g["default_branch"], g["default_limit"])
    print(f"  {OK if (p, b, l) == ('myproj', 'dev', 3) else FAIL} 参数解析 commits myproj dev 3 → ({p}, {b}, {l})")
    p, b, l = router.parse_commits_args(["5"], g["default_project"], g["default_branch"], g["default_limit"])
    print(f"  {OK if (p, b, l) == ('demo/proj', 'master', 5) else FAIL} 参数解析 commits 5 → ({p}, {b}, {l})")
    # 数字参数识别为「条数」而非分支，并被上限截断到 30
    p, b, l = router.parse_commits_args(["myproj", "99"], g["default_project"], g["default_branch"], g["default_limit"])
    print(f"  {OK if (p, b, l) == ('myproj', 'master', 30) else FAIL} 参数解析 commits myproj 99 → ({p}, {b}, {l})（数字=条数，截断到 30）")
    # 显式分支写法
    p, b, l = router.parse_commits_args(["myproj", "--branch=dev", "--limit=3"], g["default_project"], g["default_branch"], g["default_limit"])
    print(f"  {OK if (p, b, l) == ('myproj', 'dev', 3) else FAIL} 参数解析 commits myproj --branch=dev --limit=3 → ({p}, {b}, {l})")
    print(f"  {OK if l <= 30 else FAIL} 条数上限保护（{l} ≤ 30）")


# ---------------- 2. GitLab ----------------
def test_gitlab(cfg):
    print("\n─── 2. GitLab 连通性 ───")
    from skills.gitlab_skill import GitLabClient, fmt_commits

    g = cfg["gitlab"]
    print(f"  地址: {g['url']}")
    if not g.get("token"):
        print(f"  {WARN} 未配置 gitlab.token，跳过")
        return
    gl = GitLabClient(g["url"], g["token"])
    project = g.get("default_project") or ""
    if not project:
        print(f"  {WARN} 未配置 gitlab.default_project，跳过提交查询")
        return
    ok, data = gl.list_commits(project, g.get("default_branch", "master"), 3)
    if ok:
        print(f"  {OK} 查询成功，{len(data)} 条提交")
        print("  " + "-" * 50)
        print("\n".join("  " + x for x in fmt_commits(project, g.get("default_branch", "master"), data).split("\n")))
        print("  " + "-" * 50)
    else:
        print(f"  {FAIL} {data}")


# ---------------- 3. WorkBuddy CLI ----------------
def test_cli(cfg):
    print("\n─── 3. WorkBuddy CLI ───")
    from executor import WorkBuddyExecutor, wrap_prompt

    wb = cfg["workbuddy"]
    ex = WorkBuddyExecutor(
        cli_path=wb.get("cli_path", ""),
        model=wb.get("model", ""),
        timeout=min(wb.get("timeout", 150), 120),
        allowed_tools=wb.get("allowed_tools", ""),
    )
    ok, msg = ex.available()
    print(f"  {OK if ok else FAIL} CLI 可用性: {msg}")
    if not ok:
        return

    print("  执行测试指令（最长等待 120s）…")
    res = asyncio.run(ex.run(wrap_prompt("请只回复四个字：分身就绪")))
    if res["ok"]:
        print(f"  {OK} 执行成功（{res['elapsed']:.1f}s）")
        print(f"  返回: {res['text'][:200]!r}")
    else:
        print(f"  {FAIL} 执行失败: {res['error'][:300]}")
        print("        → 群内自然语言指令将无法处理，内置指令不受影响")


# ---------------- 4. 长连接鉴权 ----------------
def test_ws(cfg):
    print("\n─── 4. 企业微信长连接 ───")
    from wecom_client import WecomBot

    w = cfg["wecom"]
    if not w.get("secret"):
        print(f"  {WARN} 未配置 wecom.secret，跳过")
        return
    bot = WecomBot(w["bot_id"], w["secret"], w.get("ws_url"), w.get("heartbeat_interval", 30))
    result = {"auth": False, "error": None}

    # 捕获鉴权失败的具体错误码，便于定位
    class _ErrCatcher(logging.Handler):
        def emit(self, record):
            if record.levelno >= logging.ERROR:
                result["error"] = record.getMessage()

    logger = logging.getLogger("wecom")
    catcher = _ErrCatcher()
    logger.addHandler(catcher)

    async def run():
        task = asyncio.create_task(bot.connect_async())
        # 注意：不要用 'connected' 事件判断鉴权结果。
        # 该事件在发出订阅请求后立即触发，此时服务端响应还没回来，
        # bot.authenticated 必然仍是 False，会误报失败。
        # 正确做法是轮询 authenticated 直到成功或超时。
        for _ in range(30):
            await asyncio.sleep(0.5)
            if bot.authenticated:
                result["auth"] = True
                break
        bot.disconnect()
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

    try:
        asyncio.run(run())
    finally:
        logger.removeHandler(catcher)

    if result["auth"]:
        print(f"  {OK} 长连接鉴权成功")
    else:
        print(f"  {FAIL} 鉴权未成功，请检查 Bot ID / Secret 及机器人「消息」权限授权状态")
        if result["error"]:
            print(f"       服务端返回: {result['error']}")


# ---------------- main ----------------
def main():
    only = sys.argv[1] if len(sys.argv) > 1 else None
    print("=" * 60)
    print("数字分身 · 自检")
    print("=" * 60)

    # parse 不依赖配置，单独跑时不需要 config.yaml
    if only in (None, "parse"):
        test_parse()

    if only == "parse":
        print("\n" + "=" * 60)
        print("自检结束（仅指令解析，未读取配置）。")
        print("=" * 60)
        return

    try:
        cfg = load_cfg()
    except FileNotFoundError:
        print("\n⚠️  未找到 config.yaml，跳过后续测试。")
        print("   先执行：cp config.example.yaml config.yaml")
        print("   然后跑：python selftest.py")
        return

    if only in (None, "gitlab"):
        test_gitlab(cfg)
    if only in (None, "cli"):
        test_cli(cfg)
    if only in (None, "ws"):
        test_ws(cfg)

    print("\n" + "=" * 60)
    print("自检结束。未通过项按提示补充配置后重跑。")
    print("=" * 60)


if __name__ == "__main__":
    main()
