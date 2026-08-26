# -*- coding: utf-8 -*-
"""exe 索引（文件名索引 + 产品名/文件说明显示名索引）单元测试。

覆盖：
- 显示名回退链：ProductName(产品名称) -> FileDescription(文件说明) -> 无（仅文件名兜底）
- 通用产品名黑名单、与文件名重复的别名去重、单别名路径数上限
- _build_exe_index 端到端：内存索引 / JSON 输出（_product/_meta）/ 显示名解析 / app search
- 旧版扁平索引合并（保留仍有效的历史条目）
- 真实 exe 版本资源读取（Windows only）
"""
import asyncio
import json
import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "src"))

import cherry_remote_app.executor as executor_mod  # noqa: E402
from cherry_remote_app.executor import (  # noqa: E402
    Executor,
    _PRODUCT_PATHS_LIMIT,
    _normalize_alias,
    _read_exe_version_info,
)

TMP = Path(tempfile.mkdtemp(prefix="cherry_index_"))


def _make_executor(index_file: Path) -> Executor:
    return Executor(
        {
            "allowed_actions": ["app", "system"],
            "build_exe_index": True,
            "exe_index_file": str(index_file),
            "default_timeout": 30,
        }
    )


class AliasNormalizeTest(unittest.TestCase):
    def test_normalize_alias(self):
        self.assertEqual(_normalize_alias("  Google   Chrome "), "google chrome")
        self.assertEqual(_normalize_alias("  米哈游  启动器  "), "米哈游 启动器")
        self.assertEqual(_normalize_alias(""), "")


class VersionInfoReadTest(unittest.TestCase):
    def test_read_real_exe(self):
        """Windows 下读取真实 exe 的版本资源；非 Windows 跳过。"""
        target = r"C:\Windows\System32\notepad.exe"
        if os.name != "nt" or not os.path.isfile(target):
            self.skipTest("非 Windows 或不存在 notepad.exe")
        info = _read_exe_version_info(target)
        self.assertIsNotNone(info)
        self.assertTrue(info.get("ProductName") or info.get("FileDescription"))
        # 无版本资源的文件（如某些裸打包 exe）应返回 None 而非抛异常
        nores = TMP / "nores.exe"
        nores.write_bytes(b"MZ")
        self.assertIsNone(_read_exe_version_info(str(nores)))


class IndexAliasesTest(unittest.TestCase):
    def setUp(self):
        self.ex = _make_executor(TMP / "exe_index.json")
        self._orig_reader = executor_mod._read_exe_version_info

    def tearDown(self):
        executor_mod._read_exe_version_info = self._orig_reader

    def _alias(self, name: str, info) -> dict:
        exe = TMP / name
        exe.write_bytes(b"MZ")
        product_index: dict[str, list[str]] = {}
        executor_mod._read_exe_version_info = lambda p: info
        self.ex._index_product_aliases(str(exe), product_index)
        return product_index

    def test_product_name_used(self):
        pi = self._alias("hyp.exe", {"ProductName": "米哈游启动器", "FileDescription": "米哈游启动器"})
        self.assertEqual(pi, {"米哈游启动器": [str(TMP / "hyp.exe")]})

    def test_empty_product_falls_back_to_description(self):
        """产品名缺失/为空 -> 回退到文件说明。"""
        pi = self._alias("taskmgr.exe", {"ProductName": "", "FileDescription": "任务管理器"})
        self.assertEqual(pi, {"任务管理器": [str(TMP / "taskmgr.exe")]})
        pi = self._alias("x.exe", {"ProductName": "   ", "FileDescription": "某工具"})
        self.assertEqual(pi, {"某工具": [str(TMP / "x.exe")]})

    def test_both_empty_no_alias(self):
        """产品名与文件说明都为空 -> 无别名（文件名索引兜底）。"""
        pi = self._alias("plain.exe", {"ProductName": "  ", "FileDescription": ""})
        self.assertEqual(pi, {})
        pi = self._alias("noinfo.exe", None)  # 无版本资源
        self.assertEqual(pi, {})

    def test_alias_same_as_filename_skipped(self):
        pi = self._alias("tool.exe", {"ProductName": "tool", "FileDescription": ""})
        self.assertEqual(pi, {})

    def test_generic_product_name_blocked(self):
        pi = self._alias("taskmgr.exe", {"ProductName": "Microsoft® Windows® Operating System"})
        self.assertEqual(pi, {})

    def test_paths_cap_per_alias(self):
        product_index: dict[str, list[str]] = {}
        for i in range(_PRODUCT_PATHS_LIMIT + 3):
            exe = TMP / f"dup{i}.exe"
            exe.write_bytes(b"MZ")
            executor_mod._read_exe_version_info = lambda p: {"ProductName": "Dupe App"}
            self.ex._index_product_aliases(str(exe), product_index)
        self.assertEqual(len(product_index["dupe app"]), _PRODUCT_PATHS_LIMIT)


class BuildIndexTest(unittest.TestCase):
    def setUp(self):
        self.root = TMP / "apps"
        (self.root / "MiHoYo Launcher" / "1.16.1.364").mkdir(parents=True)
        (self.root / "Game").mkdir(parents=True)
        self.hyp = self.root / "MiHoYo Launcher" / "1.16.1.364" / "HYP.exe"
        self.game = self.root / "Game" / "ZenlessZoneZero.exe"
        self.plain = self.root / "Game" / "plainapp.exe"
        for f in (self.hyp, self.game, self.plain):
            f.write_bytes(b"MZ")
        self.index_file = TMP / "build_index.json"
        self.ex = _make_executor(self.index_file)

    def tearDown(self):
        shutil.rmtree(self.root, ignore_errors=True)

    def _build(self):
        def fake_reader(path: str):
            name = os.path.basename(path).lower()
            if name == "hyp.exe":
                return {"ProductName": "米哈游启动器", "FileDescription": "米哈游启动器"}
            if name == "zenlesszonezero.exe":
                return {"ProductName": "绝区零", "FileDescription": ""}
            if name == "plainapp.exe":
                return {"ProductName": "  ", "FileDescription": ""}  # 字段为空 -> 无别名
            return None

        orig_roots = self.ex._index_roots
        orig_reader = executor_mod._read_exe_version_info
        self.ex._index_roots = lambda: [(str(self.root), 3)]
        executor_mod._read_exe_version_info = fake_reader
        try:
            asyncio.run(self.ex._build_exe_index())
        finally:
            self.ex._index_roots = orig_roots
            executor_mod._read_exe_version_info = orig_reader

    def test_build_index_json_and_resolve(self):
        self._build()

        # 内存索引
        self.assertEqual(self.ex.exe_index["hyp.exe"], str(self.hyp))
        self.assertEqual(self.ex.exe_index["plainapp.exe"], str(self.plain))
        self.assertEqual(self.ex.exe_product_index["米哈游启动器"], [str(self.hyp)])
        self.assertEqual(self.ex.exe_product_index["绝区零"], [str(self.game)])
        self.assertNotIn("plainapp", self.ex.exe_product_index)

        # JSON：文件名扁平映射 + _product + _meta
        payload = json.loads(self.index_file.read_text(encoding="utf-8"))
        self.assertEqual(payload["hyp.exe"], str(self.hyp))
        self.assertEqual(
            payload["_product"]["米哈游启动器"], [{"file": "HYP.exe", "path": str(self.hyp)}]
        )
        self.assertEqual(payload["_meta"]["version"], 2)
        self.assertEqual(payload["_meta"]["exe_count"], 3)
        self.assertEqual(payload["_meta"]["product_count"], 2)

        # 显示名解析（用户认知的应用名 -> 位置+文件名）
        self.assertEqual(self.ex._resolve_exe("米哈游启动器"), str(self.hyp))
        self.assertEqual(self.ex._resolve_exe("绝区零"), str(self.game))
        # 文件名解析（原有行为不破坏）
        self.assertEqual(self.ex._resolve_exe("HYP.exe"), str(self.hyp))
        self.assertEqual(self.ex._resolve_exe("hyp"), str(self.hyp))
        self.assertEqual(self.ex._resolve_exe("plainapp.exe"), str(self.plain))
        self.assertIsNone(self.ex._resolve_exe("不存在的应用"))

    def test_app_search_matched_on(self):
        self._build()
        r = asyncio.run(self.ex._app_search({"query": "米哈游"}))
        self.assertEqual(r["count"], 1)
        self.assertEqual(r["matches"][0]["matched_on"], "product")
        self.assertEqual(r["matches"][0]["path"], str(self.hyp))
        r = asyncio.run(self.ex._app_search({"query": "HYP"}))
        self.assertTrue(any(m["matched_on"] == "filename" for m in r["matches"]))
        # 空查询不崩溃
        r = asyncio.run(self.ex._app_search({"query": ""}))
        self.assertGreaterEqual(r["count"], 3)


class MergeLegacyTest(unittest.TestCase):
    def test_merge_legacy_index(self):
        index_file = TMP / "merge_index.json"
        ex = _make_executor(index_file)
        keep = TMP / "keep.exe"
        keep.write_bytes(b"MZ")
        index_file.write_text(
            json.dumps(
                {
                    "keep.exe": str(keep),
                    "gone.exe": str(TMP / "gone.exe"),  # 文件不存在 -> 丢弃
                    "_product": {"x": []},  # 结构键 -> 跳过
                }
            ),
            encoding="utf-8",
        )
        index: dict[str, str] = {}
        ex._merge_legacy_index(index)
        self.assertEqual(index, {"keep.exe": str(keep)})
        # 已有键不覆盖
        index["keep.exe"] = "overridden"
        ex._merge_legacy_index(index)
        self.assertEqual(index["keep.exe"], "overridden")


if __name__ == "__main__":
    unittest.main(verbosity=2)
