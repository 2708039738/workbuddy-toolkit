#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""跨账号记忆汇总（共享记忆库）测试。"""

import json
import os
import shutil
import sys
import tempfile
import unittest
from unittest.mock import patch

REPO_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SIDECAR_DIR = os.path.join(REPO_DIR, "sidecar")
if SIDECAR_DIR not in sys.path:
    sys.path.insert(0, SIDECAR_DIR)

import importlib.machinery
import importlib.util

import memory_share as ms

loader = importlib.machinery.SourceFileLoader("workbuddy", os.path.join(REPO_DIR, "bin", "workbuddy"))
spec = importlib.util.spec_from_loader("workbuddy", loader)
workbuddy = importlib.util.module_from_spec(spec)
loader.exec_module(workbuddy)


class TestMemoryHub(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp()
        self.acc_a = {"alias": "alpha", "uid": "uid-a", "enterprise_id": None}
        self.acc_b = {"alias": "beta", "uid": "uid-b", "enterprise_id": None}
        self.path_a = self._write(self.acc_a, "# 记忆\n- 默认用中文回复\n- 称呼 ser\n")
        self.path_b = self._write(self.acc_b, "# 记忆\n- 改完代码先跑测试\n")

    def tearDown(self):
        shutil.rmtree(self.root, ignore_errors=True)

    def _write(self, acc, text):
        d = ms.memory_dir(self.root, acc["uid"], acc["enterprise_id"])
        os.makedirs(d, exist_ok=True)
        path = os.path.join(d, ms.MEMORY_FILE)
        with open(path, "w", encoding="utf-8") as f:
            f.write(text)
        return path

    def _read(self, path):
        with open(path, "r", encoding="utf-8") as f:
            return f.read()

    # ------------------------------------------------------------------
    def test_01_quality_prefers_specific_over_vague(self):
        """T01: 具体、可执行的条目得分高于模糊措辞"""
        specific, _ = ms.quality("- 提交前必须跑 python -m unittest discover -s tests")
        vague, _ = ms.quality("- 测试可能要多跑几次大概就稳了")
        self.assertGreater(specific, vague)
        self.assertGreater(specific, 5.0)
        self.assertLess(vague, 5.0)

    def test_02_similarity_groups_near_duplicates(self):
        """T02: 换标点/语序微调算相似，不同主题不算"""
        a = ms.normalize("默认用中文回复")
        b = ms.normalize("默认用中文回复。")
        c = ms.normalize("提交前先跑全量测试")
        self.assertGreaterEqual(ms.similarity(a, b), ms.SIMILARITY_THRESHOLD)
        self.assertLess(ms.similarity(a, c), ms.SIMILARITY_THRESHOLD)

    def test_03_collect_dedupes_and_keeps_better_version(self):
        """T03: 近重复条目只留质量更高的一条，落选版本仍留档"""
        self._write(self.acc_a, "# 记忆\n- 提交前必须跑全量测试，大概\n")
        self._write(self.acc_b, "# 记忆\n- 提交前必须跑全量测试\n")
        stats = ms.collect([self.acc_a, self.acc_b], root=self.root)
        self.assertEqual(stats["entries"], 1, "近重复条目应合并为一条")
        self.assertEqual(stats["merged"], 1)

        lib = ms.load_library(self.root)
        entry = lib["entries"][0]
        self.assertEqual(entry["text"], "- 提交前必须跑全量测试", "应保留无模糊措辞的版本")
        self.assertEqual(set(entry["sources"]), {"uid-a", "uid-b"})
        self.assertTrue(entry["superseded"], "落选版本必须留档，不能丢")
        self.assertIn("大概", entry["superseded"][0]["text"])

    def test_04_apply_appends_shared_and_keeps_origin_verbatim(self):
        """T04: 渲染后账号原文逐字保留，共享段落追加在后面"""
        origin = self._read(self.path_a)
        ms.collect([self.acc_a, self.acc_b], root=self.root)
        res = ms.apply_account(self.acc_a, root=self.root)
        self.assertTrue(res["ok"])
        self.assertEqual(res["action"], "created")
        self.assertGreaterEqual(res["shared"], 1)

        text = self._read(self.path_a)
        self.assertTrue(text.startswith(origin.rstrip()), "原文必须原样留在文件开头")
        self.assertIn(ms.BEGIN_MARK, text)
        self.assertIn("改完代码先跑测试", text)
        self.assertNotIn("改完代码先跑测试", origin)

    def test_05_apply_is_idempotent(self):
        """T05: 重复渲染不产生改动，也不重复追加"""
        ms.collect([self.acc_a, self.acc_b], root=self.root)
        ms.apply_account(self.acc_a, root=self.root)
        first = self._read(self.path_a)
        res = ms.apply_account(self.acc_a, root=self.root)
        self.assertEqual(res["action"], "unchanged")
        self.assertEqual(self._read(self.path_a), first)
        self.assertEqual(first.count(ms.BEGIN_MARK), 1)

    def test_06_limit_keeps_origin_intact(self):
        """T06: 原文接近 4000 字时，共享段落让位，原文一个字都不截"""
        origin = "# 记忆\n" + "\n".join("- 自有条目 {:04d} {}".format(i, "内" * 60) for i in range(50))
        self._write(self.acc_a, origin)
        self._write(self.acc_b, "# 记忆\n" + "\n".join("- 他人条目 {} {}".format(i, "外" * 60) for i in range(10)))
        ms.collect([self.acc_a, self.acc_b], root=self.root)
        res = ms.apply_account(self.acc_a, root=self.root)

        text = self._read(self.path_a)
        self.assertTrue(text.startswith(origin.rstrip()), "原文不能被共享内容挤掉")
        self.assertLessEqual(len(text.strip()), ms.INJECT_LIMIT + 200, "超限时应只做有限展开")
        self.assertGreaterEqual(res["truncated"], 1, "放不下的共享条目应被计入截断")

    def test_07_restore_returns_exact_original(self):
        """T07: restore 用快照还原，内容与接入手写版本逐字一致"""
        origin = self._read(self.path_a)
        ms.collect([self.acc_a, self.acc_b], root=self.root)
        ms.apply_account(self.acc_a, root=self.root)
        self.assertNotEqual(self._read(self.path_a), origin)

        res = ms.restore_account(self.acc_a, root=self.root)
        self.assertTrue(res["ok"])
        self.assertEqual(self._read(self.path_a), origin)
        self.assertTrue(res["backup"] and os.path.isfile(res["backup"]))

    def test_08_absorb_keeps_old_and_adds_new(self):
        """T08: 账号自己在文件里新增条目，下次渲染既不丢旧条目也能吸收新条目"""
        ms.collect([self.acc_a, self.acc_b], root=self.root)
        ms.apply_account(self.acc_a, root=self.root)
        with open(self.path_a, "a", encoding="utf-8") as f:
            f.write("- 新规矩：提交信息用中文\n")

        res = ms.apply_account(self.acc_a, root=self.root)
        self.assertTrue(res["ok"])
        text = self._read(self.path_a)
        self.assertIn("默认用中文回复", text, "旧条目不能因为新增而丢失")
        self.assertIn("称呼 ser", text)
        self.assertIn("新规矩：提交信息用中文", text)
        self.assertEqual(res["added"], 1)

        snap = self._read(ms.snapshot_path(self.root, "uid-a"))
        self.assertIn("新规矩：提交信息用中文", snap)

    def test_09_snapshot_never_overwritten_on_recollect(self):
        """T09: 反复 collect 不覆盖基线快照"""
        ms.collect([self.acc_a, self.acc_b], root=self.root)
        ms.apply_account(self.acc_a, root=self.root)
        snap_before = self._read(ms.snapshot_path(self.root, "uid-a"))
        ms.collect([self.acc_a, self.acc_b], root=self.root)
        ms.apply_account(self.acc_a, root=self.root)
        self.assertEqual(self._read(ms.snapshot_path(self.root, "uid-a")), snap_before)

    def test_10_status_reports_own_and_shared(self):
        """T10: status 分别报告自有字数与共享条数"""
        ms.collect([self.acc_a, self.acc_b], root=self.root)
        ms.apply_account(self.acc_a, root=self.root)
        data = ms.status([self.acc_a, self.acc_b], root=self.root)
        row = [r for r in data["accounts"] if r["alias"] == "alpha"][0]
        self.assertTrue(row["exists"])
        self.assertTrue(row["snapshot"])
        self.assertGreater(row["own_chars"], 0)
        self.assertGreaterEqual(row["shared_entries"], 1)


class TestMemoryHubCli(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp()
        self.orig_root = workbuddy.get_memory_root
        self.orig_profiles = workbuddy.PROFILES_DIR
        self.orig_hub = workbuddy.MEMORY_HUB_FILE
        workbuddy.PROFILES_DIR = os.path.join(self.root, "auth_profiles")
        workbuddy.MEMORY_HUB_FILE = os.path.join(self.root, "cache", "memory_hub.json")
        workbuddy.get_memory_root = lambda: self.root
        os.makedirs(workbuddy.PROFILES_DIR, exist_ok=True)

    def tearDown(self):
        workbuddy.get_memory_root = self.orig_root
        workbuddy.PROFILES_DIR = self.orig_profiles
        workbuddy.MEMORY_HUB_FILE = self.orig_hub
        shutil.rmtree(self.root, ignore_errors=True)

    def _profile(self, alias, uid, nick="Nick"):
        path = os.path.join(workbuddy.PROFILES_DIR, "{}.info".format(alias))
        with open(path, "w", encoding="utf-8") as f:
            json.dump({"account": {"uid": uid, "nickname": nick},
                       "auth": {"accessToken": "t", "expiresAt": 1800000000000}}, f)
        return path

    def test_11_cli_collect_apply_status_restore(self):
        """T11: collect → apply → status → restore 全链路可用"""
        import io

        self._profile("alpha", "uid-a")
        self._profile("beta", "uid-b")
        a_dir = ms.memory_dir(self.root, "uid-a")
        b_dir = ms.memory_dir(self.root, "uid-b")
        os.makedirs(a_dir)
        os.makedirs(b_dir)
        with open(os.path.join(a_dir, "MEMORY.md"), "w", encoding="utf-8") as f:
            f.write("# 记忆\n- 称呼 ser\n")
        with open(os.path.join(b_dir, "MEMORY.md"), "w", encoding="utf-8") as f:
            f.write("# 记忆\n- 提交前跑全量测试\n")

        with patch("sys.stdout", io.StringIO()):
            self.assertEqual(workbuddy.run_memory_cmd(["collect"]), 0)
            self.assertEqual(workbuddy.run_memory_cmd(["apply", "alpha"]), 0)
            self.assertEqual(workbuddy.run_memory_cmd(["status"]), 0)
            self.assertEqual(workbuddy.run_memory_cmd(["restore", "alpha"]), 0)

        with open(os.path.join(a_dir, "MEMORY.md"), encoding="utf-8") as f:
            self.assertEqual(f.read(), "# 记忆\n- 称呼 ser\n")

    def test_12_disabled_switch_does_not_touch_memory(self):
        """T12: 未开启时切号不写任何记忆文件"""
        self._profile("alpha", "uid-a")
        self._profile("beta", "uid-b")
        a_dir = ms.memory_dir(self.root, "uid-a")
        os.makedirs(a_dir)
        path = os.path.join(a_dir, "MEMORY.md")
        with open(path, "w", encoding="utf-8") as f:
            f.write("# 记忆\n")
        before = os.path.getmtime(path)

        workbuddy._sync_memory_on_switch({"uid": "uid-b", "nickname": "Beta"})
        self.assertEqual(before, os.path.getmtime(path))
        self.assertFalse(os.path.exists(workbuddy.MEMORY_HUB_FILE))

    def test_13_enabled_switch_applies_before_restart(self):
        """T13: 切号时先渲染共享记忆，再重启客户端；顺序不能反"""
        import io

        self._profile("alpha", "uid-a")
        self._profile("beta", "uid-b")
        for uid, text in (("uid-a", "# 记忆\n- 称呼 ser\n"), ("uid-b", "# 记忆\n- 提交前跑测试\n")):
            d = ms.memory_dir(self.root, uid)
            os.makedirs(d, exist_ok=True)
            with open(os.path.join(d, "MEMORY.md"), "w", encoding="utf-8") as f:
                f.write(text)
        workbuddy.save_memory_hub_config({"enabled": True})

        order = []
        original_sync = workbuddy._sync_memory_on_switch

        def spy_sync(target):
            order.append("sync")
            return original_sync(target)

        auth_file = os.path.join(self.root, "auth", "workbuddy-desktop.info")
        os.makedirs(os.path.dirname(auth_file), exist_ok=True)
        with open(auth_file, "w", encoding="utf-8") as f:
            json.dump({"account": {"uid": "uid-a", "nickname": "Alpha"},
                       "auth": {"accessToken": "t", "expiresAt": 1800000000000}}, f)

        with patch("sys.stdout", io.StringIO()), \
                patch.object(workbuddy, "AUTH_FILE", auth_file), \
                patch.object(workbuddy, "DB_FILE", os.path.join(self.root, "wb.db")), \
                patch.object(workbuddy, "check_profile_health", lambda *a, **k: {"status": "HEALTHY"}), \
                patch.object(workbuddy, "_sync_memory_on_switch", spy_sync), \
                patch.object(workbuddy, "restart_workbuddy", lambda: order.append("restart")):
            self.assertTrue(workbuddy.switch_to_profile("beta", auto_restart=True))

        self.assertEqual(order, ["sync", "restart"], "渲染必须在重启之前完成")
        with open(os.path.join(ms.memory_dir(self.root, "uid-b"), "MEMORY.md"), encoding="utf-8") as f:
            text = f.read()
        self.assertIn("称呼 ser", text)
        self.assertIn(ms.BEGIN_MARK, text)


if __name__ == "__main__":
    unittest.main()
