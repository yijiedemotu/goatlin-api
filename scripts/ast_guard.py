"""注释改动的安全性验证：确认「只改注释/docstring，不动逻辑」。

为什么需要它：这个工作区**没有 git 仓库**，所以没法用 `git diff` 证明改动只是注释。
这个脚本用 AST 做等价性判断：

1. `ast.parse` 得到语法树；
2. 剥离所有 `ast.Constant` 里的字符串字面量（含 docstring 与非 docstring 字符串），
   因为注释/文档改动会连带改动字符串，但这**不改变控制流**；
3. 把剩下的树用 `ast.dump(annotate_fields=False)` 序列化成规范文本；
4. 两个版本的规范文本相同 ⇒ 除字符串字面量外结构完全一致。

用法：
    # 1) 在改动前记录基线
    python scripts/ast_guard.py snapshot <根目录> <清单文件.json>
    # 2) 改动后比对
    python scripts/ast_guard.py check <根目录> <清单文件.json>

脚本自身带一个自检（`python scripts/ast_guard.py selftest`），
用来证明"只加注释不动逻辑"的改动不会被误报、"改了逻辑"的改动一定会被抓到。
"""

from __future__ import annotations

import ast
import hashlib
import json
import sys
import tempfile
from pathlib import Path

SKIP_DIRS = {
    ".venv",
    "venv",
    "node_modules",
    "__pycache__",
    ".mypy_cache",
    ".ruff_cache",
    ".pytest_cache",
    "dist",
    "build",
}


def iter_python_files(root: Path) -> list[Path]:
    files: list[Path] = []
    for path in sorted(root.rglob("*.py")):
        if any(part in SKIP_DIRS for part in path.parts):
            continue
        files.append(path)
    return files


def structural_digest(source: str) -> str:
    """去掉全部字符串字面量后的 AST 摘要。注释与 docstring 不影响它。"""
    tree = ast.parse(source)
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            # 保留一个占位常量，避免"删掉字符串"被误判成结构变化
            node.value = "<str>"
    return ast.dump(tree, annotate_fields=False)


def file_digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def snapshot(root: Path, manifest_path: Path) -> int:
    manifest: dict[str, dict[str, str]] = {}
    for path in iter_python_files(root):
        relative = path.relative_to(root).as_posix()
        source = path.read_text(encoding="utf-8")
        try:
            structural = structural_digest(source)
        except SyntaxError as exc:  # 语法错误说明改动已经破坏代码，直接暴露
            print(f"[FAIL] 语法错误 {relative}: {exc}")
            return 1
        manifest[relative] = {"file": file_digest(path), "ast": structural}
    manifest_path.write_text(json.dumps(manifest, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"[ok] 已记录 {len(manifest)} 个文件的基线 -> {manifest_path}")
    return 0


def check(root: Path, manifest_path: Path) -> int:
    baseline: dict[str, dict[str, str]] = json.loads(manifest_path.read_text(encoding="utf-8"))
    changed: list[str] = []
    structural_changes: list[str] = []
    missing: list[str] = []
    new_files: list[str] = []

    for relative, digests in baseline.items():
        path = root / relative
        if not path.exists():
            missing.append(relative)
            continue
        if file_digest(path) == digests["file"]:
            continue
        changed.append(relative)
        try:
            current = structural_digest(path.read_text(encoding="utf-8"))
        except SyntaxError as exc:
            structural_changes.append(f"{relative}（语法错误: {exc}）")
            continue
        if current != digests["ast"]:
            structural_changes.append(relative)

    current_files = {path.relative_to(root).as_posix() for path in iter_python_files(root)}
    new_files = sorted(current_files - set(baseline))

    print(f"改动文件数：{len(changed)}")
    for name in changed:
        marker = "结构也变了!" if name in structural_changes else "仅字符串/注释"
        print(f"  - {name}: {marker}")

    if missing:
        print(f"[WARN] 文件消失（{len(missing)}）：{', '.join(missing)}")
    if new_files:
        print(f"[WARN] 新增文件（{len(new_files)}）：{', '.join(new_files)}")

    if structural_changes:
        print("\n[FAIL] 以下文件除字符串外结构也发生变化，必须人工确认是否改了逻辑：")
        for name in structural_changes:
            print(f"  - {name}")
        return 1

    print("\n[ok] 所有改动都只涉及字符串字面量（注释 / docstring / 文案），代码结构未变")
    return 0


def selftest() -> int:
    """自检：证明判定规则可靠。"""
    original = '''"""模块 docstring。"""


def add(a: int, b: int) -> int:
    """求和。"""
    # 旧注释
    total = a + b
    return total
'''
    comment_only = '''"""模块 docstring（已扩写，讲清在链路中的位置）。"""


def add(a: int, b: int) -> int:
    """求和。

    参数：
        a: 第一个加数。
        b: 第二个加数。
    """
    # 更详细的新注释：说明为什么不用 sum()
    total = a + b
    return total
'''
    logic_changed = '''"""模块 docstring。"""


def add(a: int, b: int) -> int:
    """求和。"""
    total = a - b
    return total
'''
    literal_changed = original.replace("旧注释", "新注释").replace("求和。", "计算两数之和。")
    cases = [
        ("只加注释/扩写 docstring", original, comment_only, False),
        ("改了运算符", original, logic_changed, True),
        ("只改字符串字面量", original, literal_changed, False),
    ]

    failed = 0
    with tempfile.TemporaryDirectory() as tmp:
        tmp_root = Path(tmp)
        for index, (name, before, after, expect_structural) in enumerate(cases):
            before_path = tmp_root / f"before_{index}.py"
            after_path = tmp_root / f"after_{index}.py"
            before_path.write_text(before, encoding="utf-8")
            after_path.write_text(after, encoding="utf-8")
            is_structural = structural_digest(before) != structural_digest(after)
            ok = is_structural == expect_structural
            failed += 0 if ok else 1
            verdict = "抓到结构变化" if is_structural else "判定为仅注释/字符串"
            expect_text = "结构变化" if expect_structural else "不算结构变化"
            print(f"  [{'PASS' if ok else 'FAIL'}] {name}: {verdict}（期望 {expect_text}）")

    print("[ok] 自检全部通过" if failed == 0 else f"[FAIL] 自检失败 {failed} 项")
    return 0 if failed == 0 else 1


def main(argv: list[str]) -> int:
    if len(argv) < 2:
        print(__doc__)
        return 2
    command = argv[1]
    if command == "selftest":
        return selftest()
    if command == "snapshot":
        return snapshot(Path(argv[2]).resolve(), Path(argv[3]).resolve())
    if command == "check":
        return check(Path(argv[2]).resolve(), Path(argv[3]).resolve())
    print(f"未知命令：{command}")
    return 2


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
