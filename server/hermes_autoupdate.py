#!/usr/bin/env python3
"""sjc-edge 每日自动升级 Hermes WebUI（cb/vX.Y.Z 补丁分支）与 Hermes Agent 镜像。

以 hermes 用户的 systemd --user timer 运行（每天 04:00）。流程与运维说明见本分支 README.md。
只用标准库；宿主 python3 执行，容器数据目录里的文件经 `podman unshare` 读写。
"""
from __future__ import annotations

import argparse
import datetime as dt
import fcntl
import json
import os
import re
import subprocess
import sys
import tempfile
import time
import traceback
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path

PLAIN_TAG = re.compile(r"^v(\d+)\.(\d+)\.(\d+)$")


def tag_key(tag):
    match = PLAIN_TAG.match(tag or "")
    return tuple(int(x) for x in match.groups()) if match else None


def newest_tag(tags):
    plain = [t for t in tags if tag_key(t)]
    return max(plain, key=tag_key) if plain else None


def log(message):
    print(message, flush=True)


def parse_time(value):
    """GitHub/gateway 的 ISO 时间 → epoch 秒；解析失败返回 None。"""
    if not value:
        return None
    try:
        return dt.datetime.fromisoformat(str(value).replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


@dataclass
class Config:
    home: Path = field(default_factory=Path.home)
    image_repo: str = "docker.io/nousresearch/hermes-agent"
    hub_tags_url: str = (
        "https://hub.docker.com/v2/repositories/nousresearch/hermes-agent/tags"
        "?page_size=100&ordering=last_updated"
    )
    github_repo: str = "cagedbird043/hermes-webui"
    workflow: str = "cb-follow.yml"
    webui_url: str = "http://127.0.0.1:8787"
    webui_unit: str = "hermes-webui"
    gateway_unit: str = "hermes-gateway"
    idle_wait: float = 2 * 3600
    idle_poll: float = 60
    health_timeout: float = 240
    health_poll: float = 10
    ci_stale_hours: float = 36

    @property
    def src(self):
        return self.home / "hermes-webui-src"

    @property
    def data(self):
        return self.home / ".hermes"

    @property
    def quadlets(self):
        return self.home / ".config/containers/systemd"

    @property
    def state_dir(self):
        return self.home / ".local/state/hermes-autoupdate"

    @property
    def snapshot_dir(self):
        return self.state_dir / "agent-snapshot"


@dataclass
class State:
    webui_branch: str
    webui_sha: str
    agent_tag: str
    dirty: bool = False


# ---------------------------------------------------------------- 判定（纯函数）


def plan_changes(cur, latest_webui, latest_agent):
    """返回 (目标 State, webui 是否变化, agent 是否变化)。

    WebUI：远端最新 cb/vX.Y.Z 不旧于当前分支且提交不同（含同 tag 上的补丁修正）。
    Agent：Docker Hub 最新 vX.Y.Z 严格新于当前标签；当前不是版本号（如 stable）时视为需要升级。
    """
    webui_branch, webui_sha = cur.webui_branch, cur.webui_sha
    if latest_webui:
        branch, sha = latest_webui
        cur_key = tag_key(cur.webui_branch[len("cb/"):]) or ()
        if tag_key(branch[len("cb/"):]) >= cur_key and sha != cur.webui_sha:
            webui_branch, webui_sha = branch, sha
    agent_tag = cur.agent_tag
    if latest_agent and (tag_key(cur.agent_tag) is None or tag_key(latest_agent) > tag_key(cur.agent_tag)):
        agent_tag = latest_agent
    target = State(webui_branch, webui_sha, agent_tag)
    return target, webui_sha != cur.webui_sha, agent_tag != cur.agent_tag


def describe(cur, tgt, webui_change, agent_change):
    webui = f"WebUI：{cur.webui_branch} ({cur.webui_sha[:9]})"
    webui += f" → {tgt.webui_branch} ({tgt.webui_sha[:9]})" if webui_change else "（未变）"
    agent = f"Agent：{cur.agent_tag}" + (f" → {tgt.agent_tag}" if agent_change else "（未变）")
    return f"{webui}\n{agent}"


def evaluate_health(facts, *, since, baseline, agent_tag, check_gateway):
    """facts 来自 Ops.probe；全部通过返回 None，否则返回第一条失败原因。"""
    for unit, state in facts.get("units", {}).items():
        if state != "active":
            return f"{unit} 状态为 {state}"
    health = facts.get("webui_health")
    if not health:
        return "WebUI /health 无响应"
    if health.get("status") != "ok":
        return f"WebUI /health 状态为 {health.get('status')}"
    if (health.get("server_started_at") or 0) < since - 1:
        return "WebUI 尚未完成重启"
    models = facts.get("models")
    if models is None:
        return "WebUI /api/models 无响应"
    expected = sorted(p for p, n in baseline.items() if n > 0)
    if expected:
        empty = [p for p in expected if models.get(p, 0) == 0]
        if empty:
            return "模型列表为空：" + ", ".join(empty)
    elif not any(models.values()):
        return "模型列表全部为空"
    if check_gateway:
        gateway = facts.get("gateway")
        if not gateway:
            return "读不到 gateway_state.json"
        if gateway.get("gateway_state") != "running":
            return f"gateway 状态为 {gateway.get('gateway_state')}"
        if (parse_time(gateway.get("updated_at")) or 0) < since - 1:
            return "gateway 尚未刷新状态"
        telegram = (gateway.get("platforms") or {}).get("telegram") or {}
        if telegram.get("state") != "connected":
            return f"Telegram 状态为 {telegram.get('state')}"
        if agent_tag and gateway.get("code_version") != agent_tag.lstrip("v"):
            return f"gateway 版本为 {gateway.get('code_version')}，期望 {agent_tag}"
    return None


def ci_warnings(info, *, now, repo, workflow, stale_hours):
    """info: {"workflow": ..., "run": 最近一次定时运行或 None, "failure": [原因...]}。"""
    wf = info.get("workflow") or {}
    run = info.get("run")
    out = []
    actions = f"https://github.com/{repo}/actions/workflows/{workflow}"
    if run and run.get("status") == "completed" and run.get("conclusion") not in ("success", "skipped"):
        detail = "\n".join(info.get("failure") or []) or f"结论：{run.get('conclusion')}"
        out.append(f"❌ WebUI 自动变基失败\n{detail}\n{run.get('html_url', actions)}")
    if wf.get("state") and wf.get("state") != "active":
        out.append(f"⏸️ cb-follow 已停用（{wf.get('state')}），处理后在 Actions 页面重新启用：\n{actions}")
    elif wf:
        last = parse_time((run or {}).get("created_at")) or parse_time(wf.get("created_at"))
        if last and now - last > stale_hours * 3600:
            out.append(f"⚠️ cb-follow 已超过 {stale_hours:g} 小时没有定时运行：\n{actions}")
    return out


# ---------------------------------------------------------------- 编排


def wait_idle(ops, cfg):
    deadline = ops.now() + cfg.idle_wait
    while True:
        reason = ops.busy()
        if not reason or ops.now() >= deadline:
            return reason
        log(f"等待空闲：{reason}")
        ops.sleep(cfg.idle_poll)


def check_health(ops, cfg, *, since, baseline, agent_tag, units):
    deadline = ops.now() + cfg.health_timeout
    while True:
        facts = ops.probe(units)
        reason = evaluate_health(
            facts, since=since, baseline=baseline, agent_tag=agent_tag,
            check_gateway=cfg.gateway_unit in units,
        )
        if not reason or ops.now() >= deadline:
            return reason
        ops.sleep(cfg.health_poll)


def rollback(ops, cfg, cur, *, webui_change, agent_change, units, baseline):
    since = ops.now()
    try:
        if webui_change:
            ops.checkout(cur.webui_branch, cur.webui_sha)
        if agent_change:
            ops.set_image(cur.agent_tag)
            ops.stop(units)
            ops.restore_snapshot()
            ops.start(units)
        else:
            ops.restart(units)
    except Exception as exc:  # noqa: BLE001 - 回滚失败必须如实上报
        return f"回滚出错，需要人工处理：{exc}"
    # 旧标签可能不是版本号（如 stable），回滚只检查服务健康，不校验 gateway 版本。
    reason = check_health(ops, cfg, since=since, baseline=baseline, agent_tag=None, units=units)
    return "检查正常" if not reason else f"仍异常，需要人工处理：{reason}"


def run_once(ops, cfg, *, dry_run=False, drill_rollback=False):
    try:
        notes = ci_warnings(
            ops.ci_info(), now=ops.now(), repo=cfg.github_repo,
            workflow=cfg.workflow, stale_hours=cfg.ci_stale_hours,
        )
    except Exception as exc:  # noqa: BLE001 - CI 查询失败不阻塞升级
        notes = [f"⚠️ 查询 cb-follow 状态失败：{type(exc).__name__}: {exc}"]

    def finish(message):
        text = "\n\n".join([message, *notes]) if message else "\n\n".join(notes)
        if not text:
            return
        if dry_run:
            log("（演练，不发送）\n" + text)
        else:
            ops.notify(text)

    cur = ops.current()
    tgt, webui_change, agent_change = plan_changes(cur, ops.latest_webui(), ops.latest_agent())
    if not (webui_change or agent_change or drill_rollback):
        log("没有新版本")
        finish("")
        return "noop"
    versions = describe(cur, tgt, webui_change, agent_change)
    log(versions)
    if cur.dirty:
        finish(f"⏸️ 跳过自动升级：服务器上的 WebUI 源码有未提交改动\n{versions}")
        return "skipped"

    if webui_change:
        ops.fetch_webui(tgt.webui_branch, tgt.webui_sha)
    if agent_change:
        ops.pull_image(tgt.agent_tag)
    problem = ops.preflight(tgt.agent_tag, tgt.webui_sha)
    if problem:
        finish(f"⏸️ 跳过自动升级：预检失败\n{versions}\n{problem}")
        return "skipped"
    if dry_run:
        finish(f"将升级：\n{versions}")
        return "dry-run"

    busy = wait_idle(ops, cfg)
    if busy:
        finish(f"⏸️ 跳过自动升级：{busy}，明天再试\n{versions}")
        return "skipped"

    try:
        baseline = ops.models()
    except Exception:  # noqa: BLE001 - 当前版本已坏时，只要求升级后至少有一组模型
        baseline = {}
    units = [cfg.webui_unit] + ([cfg.gateway_unit] if agent_change else [])
    if agent_change:
        ops.snapshot(cur)

    since = ops.now()
    try:
        if webui_change:
            ops.checkout(tgt.webui_branch, tgt.webui_sha)
        if agent_change:
            ops.set_image(tgt.agent_tag)
        ops.restart(units)
        reason = check_health(
            ops, cfg, since=since, baseline=baseline,
            agent_tag=tgt.agent_tag if agent_change else None, units=units,
        )
    except Exception as exc:  # noqa: BLE001 - 任何异常都走回滚
        reason = f"升级过程出错：{exc}"
    if drill_rollback and not reason:
        reason = "演练：模拟健康检查失败"

    if not reason:
        if agent_change:
            try:
                ops.prune_images({tgt.agent_tag, cur.agent_tag})
            except Exception as exc:  # noqa: BLE001 - 清理失败不影响结果
                log(f"清理旧镜像失败：{exc}")
        finish(f"✅ Hermes 自动升级完成\n{versions}")
        return "upgraded"

    result = rollback(
        ops, cfg, cur, webui_change=webui_change, agent_change=agent_change,
        units=units, baseline=baseline,
    )
    restored = "（已恢复升级前的 state.db 与 config.yaml）" if agent_change else ""
    finish(f"⚠️ Hermes 自动升级已回滚{restored}\n{versions}\n原因：{reason}\n回滚后：{result}")
    return "rolled-back"


# ---------------------------------------------------------------- 真实操作


class CommandError(RuntimeError):
    pass


PREFLIGHT_PY = "import sys; sys.path.insert(0, '/webui'); import api.config, api.streaming, api.routes"

SNAPSHOT_PY = r"""
import os, shutil, sqlite3, sys
data, out = sys.argv[1:3]
src = sqlite3.connect(f"file:{data}/state.db?mode=ro", uri=True)
dst = sqlite3.connect(f"{out}/state.db")
src.backup(dst)
dst.close()
src.close()
check = sqlite3.connect(f"{out}/state.db").execute("pragma integrity_check").fetchone()[0]
if check != "ok":
    sys.exit("state.db 快照完整性检查失败：" + check)
shutil.copy2(f"{data}/config.yaml", f"{out}/config.yaml")
for name in ("state.db", "config.yaml"):
    os.chmod(f"{out}/{name}", 0o600)
"""

# 服务已停止时执行；旧库必须连同删除新版本留下的 -wal/-shm，否则会把新 WAL 套到旧库上。
RESTORE_PY = r"""
import os, shutil, sys
data, snap = sys.argv[1:3]
for name in ("state.db", "config.yaml"):
    tmp = f"{data}/{name}.autoupdate-restore"
    shutil.copyfile(f"{snap}/{name}", tmp)
    os.chown(tmp, 10000, 10000)
    os.chmod(tmp, 0o600)
    os.replace(tmp, f"{data}/{name}")
for suffix in ("-wal", "-shm"):
    try:
        os.remove(f"{data}/state.db{suffix}")
    except FileNotFoundError:
        pass
"""


class RealOps:
    def __init__(self, cfg):
        self.cfg = cfg

    # 基础
    def now(self):
        return time.time()

    def sleep(self, seconds):
        time.sleep(seconds)

    def _run(self, *args, check=True, timeout=600, input=None):
        proc = subprocess.run(
            [str(a) for a in args], input=input, text=True,
            capture_output=True, timeout=timeout, check=False,
        )
        if check and proc.returncode != 0:
            tail = (proc.stderr or proc.stdout).strip()[-400:]
            raise CommandError(f"{args[0]} {args[1] if len(args) > 1 else ''} 退出码 {proc.returncode}：{tail}")
        return proc

    def _git(self, *args, **kw):
        return self._run("git", "-C", self.cfg.src, *args, **kw)

    def _systemctl(self, *args, **kw):
        return self._run("systemctl", "--user", *args, **kw)

    def _read_data(self, name):
        """读容器 uid 所有的数据文件（宿主 hermes 用户无直接读权限）。"""
        return self._run("podman", "unshare", "cat", self.cfg.data / name, timeout=30).stdout

    def _http_json(self, url, *, data=None, timeout=30):
        headers = {"User-Agent": "hermes-autoupdate", "Accept": "application/json"}
        body = None
        if data is not None:
            body = json.dumps(data).encode()
            headers["Content-Type"] = "application/json"
        request = urllib.request.Request(url, data=body, headers=headers)
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return json.load(response)

    # 版本
    def current(self):
        branch = self._git("rev-parse", "--abbrev-ref", "HEAD").stdout.strip()
        sha = self._git("rev-parse", "HEAD").stdout.strip()
        dirty = bool(self._git("status", "--porcelain", "--untracked-files=no").stdout.strip())
        tags = set()
        for unit in (self.cfg.webui_unit, self.cfg.gateway_unit):
            text = (self.cfg.quadlets / f"{unit}.container").read_text()
            match = re.search(r"(?m)^Image=" + re.escape(self.cfg.image_repo) + r":(\S+)$", text)
            if not match:
                raise RuntimeError(f"{unit}.container 的 Image= 不是 {self.cfg.image_repo}:<tag>")
            tags.add(match.group(1))
        if len(tags) != 1:
            raise RuntimeError("两个 quadlet 的镜像标签不一致：" + ", ".join(sorted(tags)))
        return State(branch, sha, tags.pop(), dirty)

    def latest_webui(self):
        out = self._git("ls-remote", "--heads", "origin", "refs/heads/cb/v*", timeout=60).stdout
        refs = {}
        for line in out.splitlines():
            sha, ref = line.split("\t", 1)
            refs[ref[len("refs/heads/"):]] = sha
        best = newest_tag(b[len("cb/"):] for b in refs)
        return (f"cb/{best}", refs[f"cb/{best}"]) if best else None

    def latest_agent(self):
        data = self._http_json(self.cfg.hub_tags_url)
        return newest_tag(r.get("name", "") for r in data.get("results", []))

    def ci_info(self):
        base = f"https://api.github.com/repos/{self.cfg.github_repo}/actions/workflows/{self.cfg.workflow}"
        workflow = self._http_json(base)
        runs = self._http_json(base + "/runs?event=schedule&per_page=1").get("workflow_runs") or []
        run = runs[0] if runs else None
        failure = []
        if run and run.get("status") == "completed" and run.get("conclusion") not in ("success", "skipped"):
            for job in self._http_json(run["jobs_url"]).get("jobs", []):
                steps = [s["name"] for s in job.get("steps", []) if s.get("conclusion") == "failure"]
                if steps:
                    failure.append("失败步骤：" + "、".join(steps))
                notes = self._http_json(
                    f"https://api.github.com/repos/{self.cfg.github_repo}/check-runs/{job['id']}/annotations"
                )
                failure += [
                    a["message"] for a in notes
                    if a.get("annotation_level") == "failure"
                    and not a.get("message", "").startswith("Process completed with exit code")
                ]
        return {"workflow": workflow, "run": run, "failure": failure}

    # 准备
    def fetch_webui(self, branch, sha):
        self._git("fetch", "-q", "--depth=50", "origin", f"+refs/heads/{branch}:refs/remotes/origin/{branch}", timeout=300)
        fetched = self._git("rev-parse", f"refs/remotes/origin/{branch}").stdout.strip()
        if fetched != sha:
            raise RuntimeError(f"{branch} 在拉取期间变化：{fetched[:9]} ≠ {sha[:9]}")

    def pull_image(self, tag):
        self._run("podman", "pull", "-q", f"{self.cfg.image_repo}:{tag}", timeout=1800)

    def preflight(self, tag, sha):
        with tempfile.TemporaryDirectory(prefix="hermes-autoupdate-") as tree:
            os.chmod(tree, 0o755)
            archive = subprocess.run(
                ["git", "-C", str(self.cfg.src), "archive", sha], capture_output=True, check=True,
            ).stdout
            subprocess.run(["tar", "-x", "-C", tree], input=archive, capture_output=True, check=True)
            proc = self._run(
                "podman", "run", "--rm", "--user", "10000:10000", "--network", "none",
                "-e", "PYTHONPATH=/webui-pydeps", "-e", "HOME=/tmp", "-e", "HERMES_HOME=/tmp/hermes-home",
                "-v", f"{self.cfg.home}/webui-pydeps:/webui-pydeps:ro", "-v", f"{tree}:/webui:ro",
                "--entrypoint", "/opt/hermes/.venv/bin/python", f"{self.cfg.image_repo}:{tag}",
                "-c", PREFLIGHT_PY, check=False, timeout=300,
            )
        if proc.returncode != 0:
            lines = (proc.stderr or proc.stdout).strip().splitlines()
            return "新 WebUI 在新镜像中导入失败：" + (lines[-1] if lines else f"退出码 {proc.returncode}")
        return None

    # 运行状态
    def busy(self):
        try:
            health = self._http_json(self.cfg.webui_url + "/health", timeout=10)
            running = (health.get("active_streams") or 0) + (health.get("active_runs") or 0)
            if running:
                return f"WebUI 有 {running} 个对话在运行"
        except Exception:  # noqa: BLE001, S110 - WebUI 已坏不算忙
            pass
        try:
            gateway = json.loads(self._read_data("gateway_state.json"))
            api_runs = (((gateway.get("platforms") or {}).get("api_server") or {}).get("metrics") or {}).get("active_runs") or 0
            running = (gateway.get("active_agents") or 0) + api_runs
            if running:
                return f"gateway 有 {running} 个任务在运行"
        except Exception:  # noqa: BLE001, S110 - gateway 已坏不算忙
            pass
        return None

    def models(self):
        data = self._http_json(self.cfg.webui_url + "/api/models", timeout=90)
        return {g.get("provider_id") or "?": len(g.get("models") or []) for g in data.get("groups", [])}

    def probe(self, units):
        facts = {"units": {u: self._systemctl("is-active", u, check=False).stdout.strip() for u in units}}
        try:
            facts["webui_health"] = self._http_json(self.cfg.webui_url + "/health", timeout=10)
        except Exception:  # noqa: BLE001
            facts["webui_health"] = None
        facts["models"] = None
        if (facts["webui_health"] or {}).get("status") == "ok":
            try:
                facts["models"] = self.models()
            except Exception:  # noqa: BLE001, S110 - 读不到即视为未通过
                pass
        if self.cfg.gateway_unit in units:
            try:
                facts["gateway"] = json.loads(self._read_data("gateway_state.json"))
            except Exception:  # noqa: BLE001
                facts["gateway"] = None
        return facts

    # 变更
    def snapshot(self, cur):
        snap = self.cfg.snapshot_dir
        tmp = snap.with_name(snap.name + ".new")
        self._run("rm", "-rf", tmp)
        tmp.mkdir(mode=0o700, parents=True)
        self._run("podman", "unshare", "python3", "-c", SNAPSHOT_PY, self.cfg.data, tmp, timeout=600)
        meta = {
            "image": f"{self.cfg.image_repo}:{cur.agent_tag}",
            "webui_branch": cur.webui_branch,
            "webui_sha": cur.webui_sha,
            "taken_at": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
        }
        (tmp / "SOURCE.json").write_text(json.dumps(meta, ensure_ascii=False, indent=1) + "\n")
        os.chmod(tmp / "SOURCE.json", 0o600)
        self._run("rm", "-rf", snap)
        tmp.rename(snap)

    def restore_snapshot(self):
        snap = self.cfg.snapshot_dir
        if not (snap / "SOURCE.json").exists():
            raise RuntimeError(f"没有可用快照：{snap}")
        self._run("podman", "unshare", "python3", "-c", RESTORE_PY, self.cfg.data, snap, timeout=600)

    def checkout(self, branch, sha):
        self._git("checkout", "-q", "-B", branch, sha)
        self._git("branch", "-q", f"--set-upstream-to=origin/{branch}", check=False)

    def set_image(self, tag):
        for unit in (self.cfg.webui_unit, self.cfg.gateway_unit):
            path = self.cfg.quadlets / f"{unit}.container"
            text = path.read_text()
            new = re.sub(r"(?m)^Image=.*$", f"Image={self.cfg.image_repo}:{tag}", text, count=1)
            tmp = path.with_name(path.name + ".autoupdate")
            tmp.write_text(new)
            os.chmod(tmp, path.stat().st_mode & 0o777)
            os.replace(tmp, path)
        self._systemctl("daemon-reload")

    def restart(self, units):
        self._systemctl("restart", *units)

    def stop(self, units):
        self._systemctl("stop", *units)

    def start(self, units):
        self._systemctl("start", *units)

    def prune_images(self, keep_tags):
        keep = {f"{self.cfg.image_repo}:{t}" for t in keep_tags}
        refs = self._run(
            "podman", "images", "--format", "{{.Repository}}:{{.Tag}}", "--filter", f"reference={self.cfg.image_repo}",
        ).stdout.split()
        for ref in refs:
            if ref not in keep and not ref.endswith(":<none>"):
                self._run("podman", "rmi", ref, check=False)
        self._run("podman", "image", "prune", "-f", check=False)

    # 通知：直接调 Bot API，不经过 gateway，新版本 gateway 崩了也能送达。
    def notify(self, text):
        try:
            env = parse_env(self._read_data(".env"))
            token = env.get("TELEGRAM_BOT_TOKEN")
            chat = (env.get("TELEGRAM_ALLOWED_USERS") or "").split(",")[0].strip()
            if not token or not chat:
                log("通知未发送：.env 缺少 TELEGRAM_BOT_TOKEN 或 TELEGRAM_ALLOWED_USERS")
                return
            self._http_json(
                f"https://api.telegram.org/bot{token}/sendMessage",
                data={"chat_id": chat, "text": text, "disable_web_page_preview": True},
            )
            log("已发送 Telegram 通知")
        except Exception as exc:  # noqa: BLE001 - 只记类型，异常文本可能带 URL（含 token）
            log(f"通知发送失败：{type(exc).__name__} {getattr(exc, 'code', '')}")
        log(text)


def parse_env(text):
    env = {}
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.removeprefix("export ").split("=", 1)
        env[key.strip()] = value.strip().strip("'\"")
    return env


def main(argv=None):
    parser = argparse.ArgumentParser(description="Hermes WebUI / Agent 每日自动升级")
    parser.add_argument("--dry-run", action="store_true", help="只检查和预检，不改动、不发通知")
    parser.add_argument("--drill-rollback", action="store_true", help="演练：升级后按健康检查失败处理并回滚")
    args = parser.parse_args(argv)
    cfg = Config()
    ops = RealOps(cfg)
    cfg.state_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    with open(cfg.state_dir / "lock", "w") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            log("已有一次升级在运行")
            return 1
        try:
            log("结果：" + run_once(ops, cfg, dry_run=args.dry_run, drill_rollback=args.drill_rollback))
            return 0
        except Exception as exc:  # noqa: BLE001
            traceback.print_exc()
            if not args.dry_run:
                ops.notify(f"❌ Hermes 自动升级脚本出错：{type(exc).__name__}: {exc}")
            return 1


if __name__ == "__main__":
    sys.exit(main())
