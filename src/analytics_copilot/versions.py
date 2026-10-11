"""版本标识：一处定义，接口与评测产物都引用它。

为什么需要：M6 要汇总 P0 时间窗、P1 口径、P1-2 能力卡、v1.2 验证层四批改动，
如果产物里没有版本标识，过几天就说不清"这个数字是哪一版跑出来的"。
"""

APP_VERSION = "0.3.0"
# 验证层：v1 → v1.1（解释强度）→ v1.2（实体编码 / 单位换算）
VALIDATOR_VERSION = "v1.2"
# 解释强度模型
EXPLAIN_VERSION = "v1.1"
# 时间窗解析层（P0-1 引入）
WINDOW_VERSION = "v1.0"
# 数据能力卡（P1-2 引入）
CAPABILITY_VERSION = "v1.0"


def stamp():
    """给接口与评测产物用的版本块。"""
    return {
        "validator_version": VALIDATOR_VERSION,
        "explain_version": EXPLAIN_VERSION,
        "window_version": WINDOW_VERSION,
        "capability_version": CAPABILITY_VERSION,
        "app_version": APP_VERSION,
    }
