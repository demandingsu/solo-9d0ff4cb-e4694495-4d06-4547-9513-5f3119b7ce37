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
ACTION_HINTS = ("action", "operation", "操作", "动作")

# 分站差异 CSV 的操作词归一化：增补 / 排除。
ACTION_ALIASES = {
    "add": "add",
    "include": "add",
    "plus": "add",
    "增补": "add",
    "增加": "add",
    "加印": "add",
    "新增": "add",
    "补充": "add",
    "添加": "add",
    "exclude": "exclude",
    "remove": "exclude",
    "minus": "exclude",
    "排除": "exclude",
    "取消": "exclude",
    "删除": "exclude",
    "剔除": "exclude",
    "停用": "exclude",
}


class CsvFormatError(ValueError):
    pass


@dataclass(frozen=True)
class ParsedCsv:
    pairs: list[tuple[str, str]]          # 文件内去重后的搭配，保持出现顺序
    raw_data_rows: int                    # 文件中的原始数据行数（不含空行/表头）


@dataclass(frozen=True)
class ParsedDiffCsv:
    diffs: list[tuple[str, str, str]]     # (图版号, 语种, 操作)，文件内去重后保持出现顺序
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


def _is_diff_header(fields: list[str]) -> bool:
    lowered = [f.strip().lower() for f in fields]
    return (
        any(any(h in f for h in PLATE_HINTS) for f in lowered)
        and any(any(h in f for h in LANG_HINTS) for f in lowered)
        and any(any(h in f for h in ACTION_HINTS) for f in lowered)
    )


def _pick_diff_columns(header: list[str]) -> tuple[int, int, int]:
    plate_idx = lang_idx = action_idx = -1
    for i, field_name in enumerate(header):
        name = field_name.strip().lower()
        if plate_idx < 0 and any(h in name for h in PLATE_HINTS):
            plate_idx = i
        if lang_idx < 0 and any(h in name for h in LANG_HINTS):
            lang_idx = i
        if action_idx < 0 and any(h in name for h in ACTION_HINTS):
            action_idx = i
    if plate_idx < 0 or lang_idx < 0 or action_idx < 0:
        raise CsvFormatError(
            "差异表头需同时包含图版号列（plate/图版）、语种列（language/语种）"
            "和操作列（action/操作，取值：add 增补 / exclude 排除）"
        )
    return plate_idx, lang_idx, action_idx


def parse_diffs(raw: bytes) -> ParsedDiffCsv:
    """解析分站差异 CSV：每行 ``图版号,语种,操作``。

    * 操作支持 ``add/增补/加印/新增…`` 与 ``exclude/排除/取消/删除…``；
    * 无表头时每行需且仅需三列，带表头时按列名识别（允许额外列）；
    * **空文件或只有表头（空清单）合法**：整份替换即清空全部差异，
      分站恢复直接沿用主展；
    * 同一 ``(图版号, 语种)`` 在文件内出现相反操作（既增补又排除）时
      整单拒收（抛 CsvFormatError），由 Web 层保留原差异继续生效；
    * 同一搭配重复相同操作按一行计。
    """
    text = _decode(raw)
    rows = [row for row in csv.reader(io.StringIO(text))]
    if not rows or all(not row or all(not cell.strip() for cell in row) for row in rows):
        # 空清单：清空差异、恢复直接沿用主展。
        return ParsedDiffCsv(diffs=[], raw_data_rows=0)

    first = rows[0]
    has_header = bool(first) and _is_diff_header(first)
    if has_header:
        plate_idx, lang_idx, action_idx = _pick_diff_columns(first)
        data_rows = rows[1:]
    else:
        if len(first) != 3:
            raise CsvFormatError("无表头差异 CSV 每行需且仅需三列：图版号,语种,操作")
        plate_idx, lang_idx, action_idx = 0, 1, 2
        data_rows = rows

    diffs: list[tuple[str, str, str]] = []
    seen: dict[tuple[str, str], str] = {}
    raw_data_rows = 0
    for line_no, row in enumerate(data_rows, start=2 if has_header else 1):
        if not row or all(not cell.strip() for cell in row):
            continue  # 跳过空行
        raw_data_rows += 1
        if not has_header and len(row) != 3:
            raise CsvFormatError(f"第 {line_no} 行列数不是三列，应为：图版号,语种,操作")
        if max(plate_idx, lang_idx, action_idx) >= len(row):
            raise CsvFormatError(f"第 {line_no} 行列数不足，应为：图版号,语种,操作")
        plate = row[plate_idx].strip()
        language = row[lang_idx].strip()
        action_text = row[action_idx].strip().lower()
        if not plate or not language or not action_text:
            raise CsvFormatError(f"第 {line_no} 行存在空的图版号、语种或操作")
        action = ACTION_ALIASES.get(action_text)
        if action is None:
            raise CsvFormatError(
                f"第 {line_no} 行操作 {row[action_idx].strip()!r} 无法识别，"
                "请使用 add（增补）或 exclude（排除）"
            )
        pair = (plate, language)
        previous = seen.get(pair)
        if previous is not None and previous != action:
            raise CsvFormatError(
                f"同一搭配（{plate}, {language}）在文件内同时写了增补与排除，"
                "整单拒收，原差异继续生效"
            )
        if previous is None:
            seen[pair] = action
            diffs.append((plate, language, action))

    return ParsedDiffCsv(diffs=diffs, raw_data_rows=raw_data_rows)
