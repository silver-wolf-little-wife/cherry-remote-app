# -*- coding: utf-8 -*-
"""手动重建 exe 索引（无需重启常驻服务即可刷新 exe_index.json）。

索引格式 v2：
- 文件名(小写) -> 完整路径（原有）
- 产品名/文件说明(小写) -> [{file, path}]（显示名，解决文件名与应用名不一致）

用法：
    python rebuild_exe_index.py                    # 写入当前目录 exe_index.json
    python rebuild_exe_index.py -o "D:\\Program Files\\fairy\\exe_index.json"
"""
import argparse
import asyncio
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "src"))

from cherry_remote_app.executor import Executor


async def main() -> None:
    parser = argparse.ArgumentParser(description="重建 exe 索引（文件名 + 产品名/文件说明显示名）")
    parser.add_argument(
        "-o", "--output", default=os.path.abspath("exe_index.json"),
        help="输出 JSON 路径（默认当前目录 exe_index.json）",
    )
    args = parser.parse_args()

    ex = Executor({"build_exe_index": True, "exe_index_file": args.output})
    await ex._build_exe_index()
    print(f"完成：{len(ex.exe_index)} 条文件名 + {len(ex.exe_product_index)} 条显示名 -> {args.output}")


if __name__ == "__main__":
    asyncio.run(main())
