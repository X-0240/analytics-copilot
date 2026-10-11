"""只读数据访问层：白名单 + 单语句 + 行数上限，三条一起才有意义。"""

import json
import re
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal

import duckdb

from analytics_copilot import config

# 禁止出现在 SQL 里的关键字（只读连接已经挡住写入，这里再挡一层）
FORBIDDEN = re.compile(
    r"\b(insert|update|delete|create|drop|alter|attach|detach|copy|install|load"
    r"|pragma|export|import|truncate|grant|revoke)\b",
    re.IGNORECASE,
)
OBJECT_REF = re.compile(
    r"\b(?:from|join)\s+([a-zA-Z_][\w]*(?:\.[a-zA-Z_][\w]*)?)", re.IGNORECASE
)


@dataclass
class QueryResult:
    """一次查询的结果：列名、行、以及是否触顶（用于提示结论不完整）。"""

    columns: list[str]
    rows: list[list]
    truncated: bool
    sql: str


class SqlRejected(Exception):
    """SQL 被护栏拒绝；code 用于对外返回结构化错误。"""

    def __init__(self, code, message):
        super().__init__(message)
        self.code = code


def load_metadata():
    """读取建库时导出的元数据（表结构、口径、白名单）。"""
    return json.loads(config.METADATA_PATH.read_text(encoding="utf-8"))


def allowed_views():
    """应用层允许访问的对象白名单。"""
    return tuple(load_metadata()["allowed_views"])


def connect():
    """只读连接：物理上也写不进去（不只是靠 SQL 检查）。"""
    return duckdb.connect(str(config.DB_PATH), read_only=True)


def check_sql(sql, allowed=None):
    """静态护栏：单语句、无写关键字、只引用白名单对象。返回规范化后的 SQL。"""
    allowed = tuple(allowed or allowed_views())
    text = sql.strip().rstrip(";")
    if not text:
        raise SqlRejected("EMPTY_SQL", "SQL 为空")
    if ";" in text:
        raise SqlRejected("MULTI_STATEMENT", "只允许单条语句")
    if FORBIDDEN.search(text):
        raise SqlRejected("FORBIDDEN_KEYWORD", "SQL 含只读场景不允许的关键字")
    if not re.match(r"^(select|with)\b", text, re.IGNORECASE):
        raise SqlRejected("NOT_SELECT", "只允许 SELECT / WITH 查询")
    for name in OBJECT_REF.findall(text):
        base = name.split(".")[-1]
        if base.lower() not in {view.lower() for view in allowed}:
            raise SqlRejected(
                "NOT_WHITELISTED", "对象不在白名单内：%s（可用：%s）"
                % (base, ", ".join(allowed))
            )
    return text


def run_sql(sql, max_rows=None):
    """执行只读查询；结果超上限时截断并标记 truncated=True。"""
    max_rows = max_rows or config.MAX_ROWS
    text = check_sql(sql)
    con = connect()
    try:
        # 外层再套 LIMIT，保证无论内层怎么写都不会把整表拉回来（多取 1 行判截断）
        cursor = con.execute(
            "SELECT * FROM (%s) AS q LIMIT %d" % (text, max_rows + 1)
        )
        columns = [item[0] for item in cursor.description]
        rows = [
            [_jsonable(value) for value in row] for row in cursor.fetchall()
        ]
    except duckdb.Error as exc:
        raise SqlRejected("BAD_SQL", "SQL 执行失败：%s" % exc) from exc
    finally:
        con.close()
    truncated = len(rows) > max_rows
    return QueryResult(
        columns=columns,
        rows=rows[:max_rows],
        truncated=truncated,
        sql=text,
    )


def _jsonable(value):
    """把 DuckDB 返回的特殊类型转成 JSON 可序列化的值。

    DuckDB 的 ROUND() 返回 Decimal、时间列返回 datetime；这些直接进
    `json.dumps` 会抛 TypeError（M1 实测踩到），因此在数据层统一归一化。
    """
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, (datetime, date)):
        return value.isoformat(sep=" ") if isinstance(value, datetime) else str(value)
    return value
