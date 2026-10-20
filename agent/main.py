"""命令行入口。

用法（在仓库根目录）：
    python -m agent.main "打开设置"
    python -m agent.main "打开微信，给文件传输助手发一条你好"
"""
from __future__ import annotations

import argparse
import sys

from .loop import run_task


def main() -> None:
    ap = argparse.ArgumentParser(description="端云协同 GUI Agent")
    ap.add_argument("task", nargs="+", help="要执行的自然语言任务")
    args = ap.parse_args()

    task = " ".join(args.task)
    result = run_task(task, verbose=True)

    print(f"\n结果：{'✅ 成功' if result['success'] else '❌ 失败'}"
          f" | 步数={result['steps']} | {result['reason']}")
    sys.exit(0 if result["success"] else 1)


if __name__ == "__main__":
    main()
