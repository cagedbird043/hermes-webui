"""hermes_autoupdate 的判定与编排测试（不触碰真实服务）。"""
from __future__ import annotations

import datetime as dt
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "server"))

import hermes_autoupdate as au

NOW = 1_800_000_000.0
BASELINE = {"kiro": 20, "cpa": 4}


def healthy(since, agent_tag="v0.22.5"):
    return {
        "units": {"hermes-webui": "active", "hermes-gateway": "active"},
        "webui_health": {"status": "ok", "server_started_at": since + 5},
        "models": {"kiro": 20, "cpa": 4},
        "gateway": {
            "gateway_state": "running",
            "updated_at": dt.datetime.fromtimestamp(since + 10, dt.timezone.utc).isoformat(),
            "code_version": agent_tag.lstrip("v"),
            "platforms": {"telegram": {"state": "connected"}},
        },
    }


class FakeOps:
    def __init__(self, cur, webui=None, agent=None, *, busy=(), probes=None, preflight=None, ci=None):
        self.cur = cur
        self.webui = webui
        self.agent = agent
        self.busy_seq = list(busy)
        self.probes = probes
        self.preflight_result = preflight
        self.ci = ci or {"workflow": {"state": "active", "created_at": None}, "run": None}
        self.t = NOW
        self.calls = []
        self.messages = []

    def now(self):
        return self.t

    def sleep(self, seconds):
        self.t += seconds

    def ci_info(self):
        return self.ci

    def current(self):
        return self.cur

    def latest_webui(self):
        return self.webui

    def latest_agent(self):
        return self.agent

    def fetch_webui(self, branch, sha):
        self.calls.append(("fetch", branch, sha))

    def pull_image(self, tag):
        self.calls.append(("pull", tag))

    def preflight(self, tag, sha):
        self.calls.append(("preflight", tag, sha))
        return self.preflight_result

    def busy(self):
        return self.busy_seq.pop(0) if self.busy_seq else None

    def models(self):
        return dict(BASELINE)

    def probe(self, units):
        self.calls.append(("probe", tuple(units)))
        return self.probes(self) if self.probes else healthy(self.t)

    def snapshot(self, cur):
        self.calls.append(("snapshot", cur.agent_tag))

    def restore_snapshot(self):
        self.calls.append(("restore",))

    def checkout(self, branch, sha):
        self.calls.append(("checkout", branch, sha))

    def set_image(self, tag):
        self.calls.append(("image", tag))

    def restart(self, units):
        self.calls.append(("restart", tuple(units)))

    def stop(self, units):
        self.calls.append(("stop", tuple(units)))

    def start(self, units):
        self.calls.append(("start", tuple(units)))

    def prune_images(self, keep):
        self.calls.append(("prune", tuple(sorted(keep))))

    def notify(self, text):
        self.messages.append(text)

    def mutations(self):
        names = {"snapshot", "restore", "checkout", "image", "restart", "stop", "start"}
        return [c for c in self.calls if c[0] in names]


CUR = au.State("cb/v0.52.544", "a" * 40, "v0.22.5")


def cfg():
    c = au.Config(home=Path("/nonexistent"))
    c.idle_wait = 600
    c.idle_poll = 60
    c.health_timeout = 120
    c.health_poll = 10
    return c


class PlanTests(unittest.TestCase):
    def test_新稳定分支与新镜像都升级(self):
        tgt, w, a = au.plan_changes(CUR, ("cb/v0.52.600", "b" * 40), "v0.23.0")
        self.assertEqual((tgt.webui_branch, tgt.agent_tag, w, a), ("cb/v0.52.600", "v0.23.0", True, True))

    def test_同一tag补丁修正也升级(self):
        tgt, w, a = au.plan_changes(CUR, ("cb/v0.52.544", "c" * 40), "v0.22.5")
        self.assertEqual((tgt.webui_sha, w, a), ("c" * 40, True, False))

    def test_远端分支更旧或镜像更旧时不动(self):
        _, w, a = au.plan_changes(CUR, ("cb/v0.52.492", "d" * 40), "v0.22.4")
        self.assertEqual((w, a), (False, False))

    def test_当前为浮动标签时切到具体版本(self):
        cur = au.State("cb/exp-v0.52.543", "a" * 40, "stable")
        tgt, w, a = au.plan_changes(cur, ("cb/v0.52.544", "b" * 40), "v0.22.5")
        self.assertEqual((tgt.agent_tag, w, a), ("v0.22.5", True, True))

    def test_只认纯版本号(self):
        self.assertEqual(au.newest_tag(["v0.22.5-desktop", "main", "v0.22.10", "v0.22.9", "latest"]), "v0.22.10")


class HealthTests(unittest.TestCase):
    def check(self, facts, **kw):
        args = {"since": NOW, "baseline": BASELINE, "agent_tag": "v0.22.5", "check_gateway": True}
        args.update(kw)
        return au.evaluate_health(facts, **args)

    def test_全部正常(self):
        self.assertIsNone(self.check(healthy(NOW)))

    def test_WebUI未重启视为未通过(self):
        facts = healthy(NOW)
        facts["webui_health"]["server_started_at"] = NOW - 3600
        self.assertIn("尚未完成重启", self.check(facts))

    def test_原本有模型的分组变空(self):
        facts = healthy(NOW)
        facts["models"] = {"kiro": 20, "cpa": 0}
        self.assertIn("cpa", self.check(facts))

    def test_原本就空的分组不要求(self):
        self.assertIsNone(self.check(healthy(NOW), baseline={"kiro": 20, "cpa": 4, "pindo": 0}))

    def test_Telegram未连接(self):
        facts = healthy(NOW)
        facts["gateway"]["platforms"]["telegram"]["state"] = "retrying"
        self.assertIn("Telegram", self.check(facts))

    def test_gateway版本不符(self):
        self.assertIn("期望 v0.23.0", self.check(healthy(NOW), agent_tag="v0.23.0"))

    def test_gateway状态未刷新(self):
        facts = healthy(NOW)
        facts["gateway"]["updated_at"] = "2020-01-01T00:00:00+00:00"
        self.assertIn("尚未刷新", self.check(facts))

    def test_只升级WebUI时不查gateway(self):
        facts = healthy(NOW)
        facts["gateway"] = None
        del facts["units"]["hermes-gateway"]
        self.assertIsNone(self.check(facts, check_gateway=False, agent_tag=None))


class CiTests(unittest.TestCase):
    def warn(self, info):
        return au.ci_warnings(info, now=NOW, repo="o/r", workflow="cb-follow.yml", stale_hours=36)

    def test_最近一次成功且新鲜时不提醒(self):
        info = {"workflow": {"state": "active"}, "run": {"status": "completed", "conclusion": "success",
                                                        "created_at": "2027-01-15T07:00:00Z"}}
        self.assertEqual(self.warn(info), [])

    def test_失败与停用都提醒并带原因(self):
        info = {
            "workflow": {"state": "disabled_manually"},
            "run": {"status": "completed", "conclusion": "failure", "created_at": "2027-01-15T07:00:00Z",
                    "html_url": "https://x/run/1"},
            "failure": ["变基冲突：api/config.py"],
        }
        out = self.warn(info)
        self.assertEqual(len(out), 2)
        self.assertIn("api/config.py", out[0])
        self.assertIn("已停用", out[1])

    def test_长时间没有定时运行(self):
        info = {"workflow": {"state": "active"}, "run": {"status": "completed", "conclusion": "success",
                                                        "created_at": "2027-01-10T00:00:00Z"}}
        self.assertIn("没有定时运行", self.warn(info)[0])


class RunTests(unittest.TestCase):
    def test_无新版本不改动也不通知(self):
        ops = FakeOps(CUR, ("cb/v0.52.544", CUR.webui_sha), "v0.22.5")
        self.assertEqual(au.run_once(ops, cfg()), "noop")
        self.assertEqual((ops.mutations(), ops.messages), ([], []))

    def test_无新版本但CI失败时仍通知(self):
        ci = {"workflow": {"state": "active"}, "run": {"status": "completed", "conclusion": "failure",
                                                      "created_at": "2027-01-15T07:00:00Z"}, "failure": []}
        ops = FakeOps(CUR, None, "v0.22.5", ci=ci)
        au.run_once(ops, cfg())
        self.assertEqual(len(ops.messages), 1)
        self.assertIn("变基失败", ops.messages[0])

    def test_只升级WebUI不快照不重启gateway(self):
        ops = FakeOps(CUR, ("cb/v0.52.600", "b" * 40), "v0.22.5")
        self.assertEqual(au.run_once(ops, cfg()), "upgraded")
        self.assertEqual(ops.mutations(), [("checkout", "cb/v0.52.600", "b" * 40), ("restart", ("hermes-webui",))])
        self.assertIn("✅", ops.messages[0])

    def test_升级Agent先快照后切镜像并重启两者(self):
        ops = FakeOps(CUR, None, "v0.23.0", probes=lambda o: healthy(o.t, "v0.23.0"))
        self.assertEqual(au.run_once(ops, cfg()), "upgraded")
        self.assertEqual(ops.mutations(), [
            ("snapshot", "v0.22.5"), ("image", "v0.23.0"), ("restart", ("hermes-webui", "hermes-gateway")),
        ])
        self.assertIn(("prune", ("v0.22.5", "v0.23.0")), ops.calls)

    def test_健康检查超时后回滚并恢复快照(self):
        def probes(o):
            facts = healthy(o.t, "v0.23.0")
            if ("image", "v0.22.5") not in o.calls:  # 新版本 Telegram 一直连不上
                facts["gateway"]["platforms"]["telegram"]["state"] = "retrying"
            return facts

        ops = FakeOps(CUR, ("cb/v0.52.600", "b" * 40), "v0.23.0", probes=probes)
        self.assertEqual(au.run_once(ops, cfg()), "rolled-back")
        both = ("hermes-webui", "hermes-gateway")
        self.assertEqual(ops.mutations(), [
            ("snapshot", "v0.22.5"),
            ("checkout", "cb/v0.52.600", "b" * 40), ("image", "v0.23.0"), ("restart", both),
            ("checkout", "cb/v0.52.544", CUR.webui_sha), ("image", "v0.22.5"),
            ("stop", both), ("restore",), ("start", both),
        ])
        self.assertNotIn("prune", [c[0] for c in ops.calls])
        self.assertIn("Telegram", ops.messages[0])
        self.assertIn("回滚后：检查正常", ops.messages[0])

    def test_升级动作抛异常也回滚(self):
        class Boom(FakeOps):
            def restart(self, units):
                super().restart(units)
                if ("image", "v0.22.5") not in self.calls and len([c for c in self.calls if c[0] == "restart"]) == 1:
                    raise RuntimeError("systemctl 失败")

        ops = Boom(CUR, None, "v0.23.0")
        self.assertEqual(au.run_once(ops, cfg()), "rolled-back")
        self.assertIn(("restore",), ops.calls)
        self.assertIn("systemctl 失败", ops.messages[0])

    def test_一直忙则跳过不改动(self):
        ops = FakeOps(CUR, ("cb/v0.52.600", "b" * 40), "v0.22.5", busy=["WebUI 有 1 个对话在运行"] * 100)
        self.assertEqual(au.run_once(ops, cfg()), "skipped")
        self.assertEqual(ops.mutations(), [])
        self.assertIn("明天再试", ops.messages[0])

    def test_忙完后再升级(self):
        ops = FakeOps(CUR, ("cb/v0.52.600", "b" * 40), "v0.22.5", busy=["WebUI 有 1 个对话在运行"] * 3)
        self.assertEqual(au.run_once(ops, cfg()), "upgraded")
        self.assertEqual(ops.t, NOW + 180)

    def test_预检失败不改动(self):
        ops = FakeOps(CUR, ("cb/v0.52.600", "b" * 40), "v0.23.0", preflight="新 WebUI 在新镜像中导入失败：No module named 'x'")
        self.assertEqual(au.run_once(ops, cfg()), "skipped")
        self.assertEqual(ops.mutations(), [])
        self.assertIn("No module named", ops.messages[0])

    def test_源码有本地改动时跳过(self):
        cur = au.State(CUR.webui_branch, CUR.webui_sha, CUR.agent_tag, dirty=True)
        ops = FakeOps(cur, ("cb/v0.52.600", "b" * 40), "v0.22.5")
        self.assertEqual(au.run_once(ops, cfg()), "skipped")
        self.assertEqual([c for c in ops.calls if c[0] in ("fetch", "pull")], [])

    def test_演练模式不改动不通知(self):
        ops = FakeOps(CUR, ("cb/v0.52.600", "b" * 40), "v0.23.0")
        self.assertEqual(au.run_once(ops, cfg(), dry_run=True), "dry-run")
        self.assertEqual((ops.mutations(), ops.messages), ([], []))


class EnvTests(unittest.TestCase):
    def test_解析env(self):
        env = au.parse_env("# c\nexport A='1'\nB=\"x=y\"\n\nTELEGRAM_ALLOWED_USERS=1,2\n")
        self.assertEqual(env, {"A": "1", "B": "x=y", "TELEGRAM_ALLOWED_USERS": "1,2"})


if __name__ == "__main__":
    unittest.main()
