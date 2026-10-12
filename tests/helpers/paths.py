"""仓库内绝对路径。

测试文件下沉到子目录后 `Path(__file__).parent.parent` 不再指向仓库根，
统一从这里取，避免每处各自推算层级。
"""

from __future__ import annotations

from pathlib import Path

# tests/helpers/paths.py -> parents[0]=helpers, [1]=tests, [2]=仓库根
ROOT = Path(__file__).resolve().parents[2]
SRC = ROOT / "src"
SCRIPTS = ROOT / "scripts"
# 真实上游样本（SSE / JSON），契约测试用
PROVIDER_FIXTURES = SRC / "provider" / "fixtures"

__all__ = ["PROVIDER_FIXTURES", "ROOT", "SCRIPTS", "SRC"]


def script_path(name: str) -> Path:
    """`scripts/` 下脚本的绝对路径，`name` 带或不带 `.py` 均可。"""
    filename = name if name.endswith(".py") else f"{name}.py"
    return SCRIPTS / filename


def provider_fixture(provider: str, name: str) -> str:
    """读取某渠道的真实样本文件内容。"""
    return (PROVIDER_FIXTURES / provider / name).read_text(encoding="utf-8")
