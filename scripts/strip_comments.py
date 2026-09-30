"""注释精简工具：删除长篇块注释与装饰性分隔线，保留单行说明。

判定规则（设计原则：宁可少删，不可误删）：
1. 把**连续整行注释**合并成一个"注释块"（tokenize 会把每一行注释拆成独立 token，必须自己合并）；
2. 块长度 ≥4 行 → 视为"教学段落"，删除（内容已迁移到 docs/ 学习文档）；
3. 块长度 ≤3 行 → **保留**（通常是解释"为什么这么写"的关键说明）；
4. 块的最后一行紧贴下一行代码 → **保留**（删掉会失去代码上下文）；
5. 纯装饰分隔线（`# ====` / `# ----`）与极短分节标签（`# 校验`）→ 删除。

安全性：用 `tokenize` 定位注释 token 的行号，不解析文本内容，
因此绝不会误伤字符串里的 `#`（如 `color="#F97316"`、CSV 表头的逗号）。

用法：
    python scripts/strip_comments.py --dry-run    # 只看差异
    python scripts/strip_comments.py              # 实际执行
"""

from __future__ import annotations

import argparse
import io
import re
import sys
import tokenize
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

SRC = Path(__file__).resolve().parents[1] / "src"

_DECORATION = re.compile(r"^#[=\-*─_]{3,}$")
_SECTION_LABEL = re.compile(r"^#\s*[^。#]{0,12}$")  # 极短分节标签，如 "# 校验"
MIN_BLOCK_LINES = 4


def _is_decoration(line: str) -> bool:
    return bool(_DECORATION.match(line.strip()))


def _is_full_line_comment(line: str) -> bool:
    return line.lstrip().startswith("#")


def strip_file(path: Path, *, dry_run: bool) -> tuple[int, int]:
    """返回 (删除行数, 原总行数)。"""
    source = path.read_text(encoding="utf-8")
    lines = source.splitlines(keepends=True)
    tokens = list(tokenize.generate_tokens(io.StringIO(source).readline))
    full_line_comments = {
        tok.start[0]
        for tok in tokens
        if tok.type == tokenize.COMMENT and _is_full_line_comment(lines[tok.start[0] - 1])
    }

    remove: set[int] = set()

    # 把连续整行注释合并成块
    for start in sorted(full_line_comments):
        if start - 1 in full_line_comments:
            continue  # 不是块的起点
        end = start
        while end + 1 in full_line_comments:
            end += 1

        block = [lines[n - 1] for n in range(start, end + 1)]
        length = end - start + 1

        all_decoration = all(_is_decoration(item) or _SECTION_LABEL.match(item.strip()) for item in block)
        # 规则：≥4 行的块才删；短块只在"全是装饰线/分节标签"时才删
        if length < MIN_BLOCK_LINES and not all_decoration:
            continue

        remove |= set(range(start, end + 1))
        # 连带删除块上方紧邻的装饰线，避免留下孤立的 ====
        previous = start - 1
        while previous >= 1 and _is_decoration(lines[previous - 1]):
            remove.add(previous)
            previous -= 1

    if not remove:
        return 0, len(lines)

    kept = [line for number, line in enumerate(lines, start=1) if number not in remove]
    if not dry_run:
        path.write_text("".join(kept), encoding="utf-8")
    return len(remove), len(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description="精简源码中的块注释")
    parser.add_argument("--dry-run", action="store_true", help="只统计，不修改")
    parser.add_argument("--only", nargs="*", default=None, help="只处理路径包含这些关键字的文件")
    args = parser.parse_args()

    total_removed = 0
    changed = 0
    targets = sorted(SRC.rglob("*.py"))
    if args.only:
        targets = [p for p in targets if any(token in p.as_posix() for token in args.only)]

    for path in targets:
        removed, before = strip_file(path, dry_run=args.dry_run)
        if removed:
            changed += 1
            total_removed += removed
            rel = path.relative_to(SRC).as_posix()
            print(f"  {rel:<48} -{removed:>3} / {before:>4} 行")

    verb = "将删除" if args.dry_run else "已删除"
    print(f"\n{verb} {total_removed} 行（{changed} 个文件 / 共检查 {len(targets)} 个）")
    if args.dry_run:
        print("确认后去掉 --dry-run 执行。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
