"""测试夹具：把 src 加进路径，保持与其他项目一致的做法。"""

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
