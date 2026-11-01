"""ci/follow.py 选 tag 逻辑测试。"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "ci"))

import follow


class SelectTests(unittest.TestCase):
    def test_跳到最新稳定版并忽略实验版(self):
        branches = ["cb/exp-v0.52.543", "cb/v0.52.544", "cb/automation"]
        tags = ["v0.52.544", "v0.52.600", "v0.52.1000", "exp-v0.52.2000", "v0.53.0-rc1"]
        self.assertEqual(follow.select(branches, tags), ("v0.52.544", "v0.52.1000"))

    def test_没有更新时不输出next(self):
        self.assertEqual(follow.select(["cb/v0.52.544"], ["v0.52.492", "v0.52.544"]), ("v0.52.544", None))

    def test_没有稳定基线时报错(self):
        with self.assertRaises(SystemExit):
            follow.select(["cb/exp-v0.52.543"], ["v0.52.544"])


if __name__ == "__main__":
    unittest.main()
