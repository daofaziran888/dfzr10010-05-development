"""容量换算：表容量、文件行组上限与优化预算统一按字节计算。

页面上的容量是「1.2 TB」「800 GB」这种带单位的字符串，编排执行计划时必须换成
字节才能比较预算、计算收益，最后再换回可读文本展示。
"""
from __future__ import annotations

import math
import re
from typing import Any

_UNITS = {
    "B": 1,
    "BYTE": 1,
    "BYTES": 1,
    "K": 1024,
    "KB": 1024,
    "KIB": 1024,
    "M": 1024 ** 2,
    "MB": 1024 ** 2,
    "MIB": 1024 ** 2,
    "G": 1024 ** 3,
    "GB": 1024 ** 3,
    "GIB": 1024 ** 3,
    "T": 1024 ** 4,
    "TB": 1024 ** 4,
    "TIB": 1024 ** 4,
    "P": 1024 ** 5,
    "PB": 1024 ** 5,
    "PIB": 1024 ** 5,
}

_PATTERN = re.compile(r"^\s*(-?\d+(?:\.\d+)?)\s*([A-Za-z]*)\s*$")


def parse_size(value: Any, *, numeric_unit: int = 1, default: int | None = None) -> int | None:
    """把容量文本换成字节数。

    数字按 ``numeric_unit`` 解释（预算接口传 1024**3，也就是数字代表 GB）；
    文本支持 B/KB/MB/GB/TB/PB 以及对应的二进制写法。无法解析时返回 ``default``。
    """
    if value is None or value == "":
        return default
    if isinstance(value, bool):
        return default
    if isinstance(value, (int, float)):
        return int(float(value) * numeric_unit)
    text = str(value).strip().replace(",", "")
    if not text:
        return default
    match = _PATTERN.match(text)
    if not match:
        return default
    number = float(match.group(1))
    unit = match.group(2).upper() or ("B" if numeric_unit == 1 else "")
    if not unit:
        multiplier = numeric_unit
    else:
        multiplier = _UNITS.get(unit)
        if multiplier is None:
            return default
    return int(number * multiplier)


def format_size(value: Any, *, precision: int = 1) -> str:
    """把字节数换成页面上的容量文本，超过 1024 才进位。"""
    try:
        number = float(value)
    except (TypeError, ValueError):
        return "—"
    if number <= 0:
        return "0 B"
    units = [(1024 ** 5, "PB"), (1024 ** 4, "TB"), (1024 ** 3, "GB"), (1024 ** 2, "MB"), (1024, "KB")]
    for base, label in units:
        if number >= base:
            text = f"{number / base:.{precision}f}".rstrip("0").rstrip(".")
            return f"{text} {label}"
    return f"{int(number)} B"


def format_percent(value: Any, *, precision: int = 2) -> str:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return "—"
    text = f"{number * 100:.{precision}f}".rstrip("0").rstrip(".")
    return f"{text}%"


def ceil_div(value: int, divisor: int) -> int:
    if divisor <= 0:
        return 0
    return int(math.ceil(value / divisor))
