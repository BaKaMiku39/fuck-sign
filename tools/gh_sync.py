#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
gh_sync.py —— 用 GitHub REST API 把工作区同步成一个提交

用途：本机 `git push` 连不上 github.com（被墙 / 公司网络拦截）时，仍然能把代码传上去。
原理：blobs → tree → commit → 更新 ref，整棵树一次替换，历史只多一个提交。

用法：
    export GH_TOKEN=ghp_xxx            # 需要 repo 权限的 token
    python tools/gh_sync.py --repo 用户名/仓库名
    python tools/gh_sync.py --repo 用户名/仓库名 --message "fix: xxx" --dry

依赖：只用标准库（urllib），不需要 GitPython。
"""
from __future__ import annotations

import argparse
import base64
import json
import os
import subprocess
import sys
import urllib.error
import urllib.request

API = "https://api.github.com"


def call(token: str, method: str, path: str, payload=None):
    data = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(API + path, data=data, method=method, headers={
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "User-Agent": "qr-sign-kit/gh_sync",
        "Content-Type": "application/json",
    })
    try:
        with urllib.request.urlopen(req, timeout=90) as r:
            return r.status, json.loads(r.read().decode() or "{}")
    except urllib.error.HTTPError as e:
        return e.code, {"error": e.read().decode()[:400]}


def tracked_files(root: str) -> list[str]:
    """优先用 git ls-files（尊重 .gitignore），没有 git 就退回遍历。"""
    try:
        out = subprocess.run(["git", "ls-files"], cwd=root, capture_output=True,
                             text=True, encoding="utf-8", timeout=60)
        if out.returncode == 0:
            files = [f.strip() for f in out.stdout.split("\n") if f.strip()]
            if files:
                return files
    except Exception:
        pass
    skip = {".git", "__pycache__", "node_modules", ".venv", "venv", ".idea", ".vscode"}
    files = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in skip]
        for f in filenames:
            if f.endswith((".pyc", ".log")):
                continue
            files.append(os.path.relpath(os.path.join(dirpath, f), root).replace(os.sep, "/"))
    return sorted(files)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--repo", required=True, help="owner/name")
    ap.add_argument("--root", default=os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    ap.add_argument("--branch", default="main")
    ap.add_argument("--message", default="chore: sync via API")
    ap.add_argument("--dry", action="store_true")
    a = ap.parse_args()

    token = os.environ.get("GH_TOKEN") or os.environ.get("GITHUB_TOKEN")
    if not token:
        print("缺少 GH_TOKEN（需要 repo 权限）")
        return 2

    files = tracked_files(a.root)
    print(f"{a.repo}  ←  {len(files)} 个文件  (branch={a.branch})")
    if a.dry:
        for f in files:
            print("  ", f)
        return 0

    code, j = call(token, "GET", f"/repos/{a.repo}/git/ref/heads/{a.branch}")
    parent = j.get("object", {}).get("sha") if code == 200 else None
    if not parent:
        # 空仓库：先放一个文件，blobs API 才能用
        call(token, "PUT", f"/repos/{a.repo}/contents/README.md", {
            "message": "chore: init repository",
            "content": base64.b64encode(b"# init\n").decode(),
            "branch": a.branch,
        })
        code, j = call(token, "GET", f"/repos/{a.repo}/git/ref/heads/{a.branch}")
        parent = j.get("object", {}).get("sha")
        print("初始提交:", parent)

    tree = []
    for rel in files:
        p = os.path.join(a.root, rel.replace("/", os.sep))
        b64 = base64.b64encode(open(p, "rb").read()).decode()
        code, j = call(token, "POST", f"/repos/{a.repo}/git/blobs",
                       {"content": b64, "encoding": "base64"})
        if code not in (200, 201):
            print(f"ERR blob {rel}: {code} {j}")
            return 1
        tree.append({"path": rel, "mode": "100644", "type": "blob", "sha": j["sha"]})
        print("  ok", rel)

    code, j = call(token, "POST", f"/repos/{a.repo}/git/trees", {"tree": tree})
    if code not in (200, 201):
        print("ERR tree:", code, j)
        return 1

    code, j = call(token, "POST", f"/repos/{a.repo}/git/commits",
                   {"message": a.message, "tree": j["sha"],
                    "parents": [parent] if parent else []})
    if code not in (200, 201):
        print("ERR commit:", code, j)
        return 1
    commit = j["sha"]

    code, j = call(token, "PATCH", f"/repos/{a.repo}/git/refs/heads/{a.branch}",
                   {"sha": commit, "force": True})
    if code not in (200, 201):
        print("ERR ref:", code, j)
        return 1

    print(f"已推送 {commit[:10]} → {a.repo}#{a.branch}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
