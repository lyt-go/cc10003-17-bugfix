"""测试运行路径保障：让 tests 目录下的用例能直接 import app。"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

BACKEND_ROOT = Path(__file__).resolve().parent.parent
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

from app.store import store  # noqa: E402


@pytest.fixture(autouse=True)
def _clean_store():
    """每个用例都从种子数据重启，避免用例之间互相污染。"""
    store.reset()
    yield
    store.reset()
