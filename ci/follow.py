#!/usr/bin/env python3
"""选择 cb-follow 本次要跟进的上游稳定 tag。

    python3 ci/follow.py select

基线 = fork 上最新的 `cb/vX.Y.Z` 远端分支；目标 = 上游最新的 `vX.Y.Z`（只认纯版本号 tag，
不含 `exp-`），比基线新时输出 `next=`，中间版本直接跳过。输出可直接追加到 $GITHUB_OUTPUT。
"""
from __future__ import annotations

import re
import subprocess
import sys

PLAIN_TAG = re.compile(r"^v(\d+)\.(\d+)\.(\d+)$")


def tag_key(tag):
    match = PLAIN_TAG.match(tag)
    return tuple(int(x) for x in match.groups()) if match else None


def newest(tags):
    plain = [t for t in tags if tag_key(t)]
    return max(plain, key=tag_key) if plain else None


def select(branches, upstream_tags):
    """branches: 远端分支短名（如 `cb/v0.52.544`）；返回 (base, next 或 None)。"""
    base = newest(b[len("cb/"):] for b in branches if b.startswith("cb/"))
    if not base:
        raise SystemExit("fork 上没有 cb/vX.Y.Z 分支，无法确定基线")
    latest = newest(upstream_tags)
    return base, latest if latest and tag_key(latest) > tag_key(base) else None


def git_lines(*args):
    out = subprocess.run(["git", *args], check=True, capture_output=True, text=True).stdout
    return [line.strip() for line in out.splitlines() if line.strip()]


def main(argv):
    if argv != ["select"]:
        raise SystemExit("usage: follow.py select")
    branches = [
        ref[len("refs/remotes/origin/"):]
        for ref in git_lines("for-each-ref", "--format=%(refname)", "refs/remotes/origin/cb/")
    ]
    tags = [
        line.split("\t", 1)[1][len("refs/tags/"):]
        for line in git_lines("ls-remote", "--tags", "--refs", "upstream", "refs/tags/v*")
    ]
    base, nxt = select(branches, tags)
    print(f"base={base}")
    if nxt:
        print(f"next={nxt}")


if __name__ == "__main__":
    main(sys.argv[1:])
