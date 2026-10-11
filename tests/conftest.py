"""全局测试夹具：import 路径引导 + 运行期产物隔离。

`src` 引导从 M0 起就在这里（测试不依赖环境 PYTHONPATH）；本次把它写成幂等，
并新增一个夹具把日志目录重定向到临时目录——否则测试记录会混进仓库
`logs/usage.jsonl`，而 `/metrics` 的今日成本正是从该文件回填的。
"""

import sys
from pathlib import Path

import pytest

SRC = Path(__file__).resolve().parents[1] / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from analytics_copilot import config  # noqa: E402


@pytest.fixture(autouse=True)
def _isolate_runtime_logs(tmp_path_factory, monkeypatch):
    """测试期间日志一律落到临时目录。

    之前测试直接写仓库的 `logs/usage.jsonl`，混进正常记录里；
    而 `/metrics` 的成本口径又是从该文件回填的，会把这些噪声算成真实花费。
    """
    log_dir = tmp_path_factory.mktemp("ac-logs")
    monkeypatch.setattr(config, "LOG_DIR", log_dir)
    monkeypatch.setattr(config, "USAGE_LOG", log_dir / "usage.jsonl")
