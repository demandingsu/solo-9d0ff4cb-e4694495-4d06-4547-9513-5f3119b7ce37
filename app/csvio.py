"""图版号 + 语种 CSV 解析。

支持两种文件：

* 无表头：每行 ``图版号,语种``（两个字段）；
* 带表头：识别含 ``plate`` / ``图版`` 的列为图版号，
  含 ``lang`` / ``语种`` / ``语言`` 的列为语种列。

编码按 UTF-8（含 BOM）解析，再回退 GB18030（馆内 Excel 另存常见编码）。
"""

from __future__ import annotations

import csv
import io
from dataclasses import dataclass

PLATE_HINTS = ("plate", "图版", "片号", "版号")
LANG_HINTS = ("lang", "语种", "语言")


class CsvFormatError(ValueError):
    pass


@dataclass(frozen=True)
class ParsedCsv:
    pairs: list[tuple[str, str]]          # 文件内去重后的搭配，保持出现顺序
    raw_data_rows: int                    # 文件中的原始数据行数（不含空行/表头）


def _decode(raw: bytes) -> str:
    for enc in ("utf-8-sig", "gb18030"):
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            continue
    raise CsvFormatError("文件编码无法识别，请使用 UTF-8 或 GB18030 编码的 CSV")


def _is_header(fields: list[str]) -> bool:
    lowered = [f.strip().lower() for f in fields]
    return any(any(h in f for h in PLATE_HINTS) for f in lowered) and any(
        any(h in f for h in LANG_HINTS) for f in lowered
    )


def _pick_columns(header: list[str]) -> tuple[int, int]:
    plate_idx = lang_idx = -1
    for i, field_name in enumerate(header):
        name = field_name.strip().lower()
        if plate_idx < 0 and any(h in name for h in PLATE_HINTS):
            plate_idx = i
        if lang_idx < 0 and any(h in name for h in LANG_HINTS):
            lang_idx = i
    if plate_idx < 0 or lang_idx < 0:
        raise CsvFormatError(
            "表头需同时包含图版号列（plate/图版）和语种列（language/语种）"
        )
    return plate_idx, lang_idx


def parse_pairs(raw: bytes) -> ParsedCsv:
    """解析 CSV 字节流，返回文件内去重后的 (图版号, 语种) 列表与原始行数。

    任何数据行缺列或字段为空都整单拒收（抛 CsvFormatError），
    由 Web 层返回错误且不写入数据库。
    """
    text = _decode(raw)
    reader = csv.reader(io.StringIO(text))
    rows = [row for row in reader]
    if not rows:
        raise CsvFormatError("CSV 文件为空")

    first = rows[0]
    has_header = bool(first) and _is_header(first)
    if has_header:
        plate_idx, lang_idx = _pick_columns(first)
        data_rows = rows[1:]
    else:
        if len(first) != 2:
            raise CsvFormatError("无表头 CSV 每行需且仅需两列：图版号,语种")
        plate_idx, lang_idx = 0, 1
        data_rows = rows

    pairs: list[tuple[str, str]] = []
    seen: set[tuple[str, str]] = set()
    raw_data_rows = 0
    for line_no, row in enumerate(data_rows, start=2 if has_header else 1):
        if not row or all(not cell.strip() for cell in row):
            continue  # 跳过空行
        raw_data_rows += 1
        if not has_header and len(row) != 2:
            raise CsvFormatError(f"第 {line_no} 行列数不是两列，应为：图版号,语种")
        if max(plate_idx, lang_idx) >= len(row):
            raise CsvFormatError(f"第 {line_no} 行列数不足，应为：图版号,语种")
        plate = row[plate_idx].strip()
        language = row[lang_idx].strip()
        if not plate or not language:
            raise CsvFormatError(f"第 {line_no} 行存在空的图版号或语种")
        pair = (plate, language)
        if pair not in seen:
            seen.add(pair)
            pairs.append(pair)

    if not pairs:
        raise CsvFormatError("CSV 中没有可登记的数据行")
    return ParsedCsv(pairs=pairs, raw_data_rows=raw_data_rows)
