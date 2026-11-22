"""
GitLab 查询能力：提交记录、分支、流水线等。

需要 GitLab Personal Access Token（api 权限）。
"""

import logging
from datetime import datetime
from typing import Optional

import requests

logger = logging.getLogger("gitlab")


class GitLabClient:
    def __init__(self, url: str, token: str, timeout: int = 20):
        self.base = url.rstrip("/")
        self.token = token
        self.timeout = timeout
        self.session = requests.Session()
        if token:
            self.session.headers["PRIVATE-TOKEN"] = token

    def _get(self, path: str, params: dict | None = None) -> tuple[bool, object]:
        """返回 (ok, data_or_error_message)。"""
        try:
            r = self.session.get(f"{self.base}/api/v4{path}", params=params or {}, timeout=self.timeout)
        except Exception as e:
            return False, f"无法连接 GitLab（{self.base}）：{e}"
        if r.status_code == 401:
            return False, "GitLab Token 无效或已过期（401）"
        if r.status_code == 404:
            return False, "未找到该项目或分支（404）"
        if r.status_code >= 400:
            return False, f"GitLab 请求失败：HTTP {r.status_code} {r.text[:200]}"
        return True, r.json()

    # ---------------- 能力 ----------------
    def search_projects(self, keyword: str, limit: int = 10):
        ok, data = self._get("/projects", {"search": keyword, "per_page": limit, "order_by": "last_activity_at"})
        if not ok:
            return ok, data
        return True, [
            {"id": p["id"], "path": p["path_with_namespace"], "desc": (p.get("description") or "")[:60]}
            for p in data
        ]

    def list_commits(self, project: str, branch: str = "master", limit: int = 10):
        """获取指定项目、分支的最近提交记录。"""
        import urllib.parse

        proj = urllib.parse.quote(str(project), safe="")
        ok, data = self._get(
            f"/projects/{proj}/repository/commits",
            {"ref_name": branch, "per_page": limit},
        )
        if not ok:
            return ok, data
        commits = []
        for c in data:
            commits.append(
                {
                    "short_id": c.get("short_id", ""),
                    "title": (c.get("title") or "").strip(),
                    "author": c.get("author_name", ""),
                    "date": c.get("created_at", ""),
                    "web_url": c.get("web_url", ""),
                }
            )
        return True, commits

    def list_branches(self, project: str, limit: int = 20):
        import urllib.parse

        proj = urllib.parse.quote(str(project), safe="")
        ok, data = self._get(f"/projects/{proj}/repository/branches", {"per_page": limit})
        if not ok:
            return ok, data
        return True, [b["name"] for b in data]

    def list_pipelines(self, project: str, branch: str = "master", limit: int = 5):
        import urllib.parse

        proj = urllib.parse.quote(str(project), safe="")
        ok, data = self._get(f"/projects/{proj}/pipelines", {"ref": branch, "per_page": limit})
        if not ok:
            return ok, data
        return True, [
            {"id": p["id"], "status": p.get("status"), "ref": p.get("ref"), "url": p.get("web_url", "")}
            for p in data
        ]


# ---------------- 格式化 ----------------
def fmt_dt(s: str) -> str:
    """把 GitLab ISO 时间转成本地可读格式。"""
    if not s:
        return ""
    try:
        dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
        return dt.astimezone().strftime("%m-%d %H:%M")
    except Exception:
        return s[:16]


def fmt_commits(project: str, branch: str, commits: list[dict]) -> str:
    """把提交记录渲染成企业微信 markdown。"""
    if not commits:
        return f"**{project}** `{branch}` 分支暂无提交记录。"

    lines = [f"### 📦 {project}", f"**分支**：`{branch}` ｜ 最近 {len(commits)} 条提交", ""]
    for i, c in enumerate(commits, 1):
        title = c["title"].replace("\n", " ")
        if len(title) > 60:
            title = title[:60] + "…"
        url = f'[`{c["short_id"]}`]({c["web_url"]})' if c["web_url"] else f'`{c["short_id"]}`'
        lines.append(f"**{i}.** {url} {title}")
        lines.append(f"　　👤 {c['author']} ｜ 🕐 {fmt_dt(c['date'])}")
    return "\n".join(lines)


def fmt_projects(items: list[dict]) -> str:
    if not items:
        return "未找到匹配的项目。"
    lines = ["### 🔍 匹配到的项目", ""]
    for p in items:
        d = f" — {p['desc']}" if p["desc"] else ""
        lines.append(f"• `{p['path']}`{d}")
    return "\n".join(lines)
