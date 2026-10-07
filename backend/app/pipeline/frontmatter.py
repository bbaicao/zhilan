"""Markdown frontmatter（任务清单 12）：解析文件头 ``---`` 包裹的 YAML，元数据自动入库。

只登记三个键：``category`` / ``tags`` / ``created_at``（任务清单条目 12 的「先不做」明确
不做自定义 schema、不做任意 YAML 键全量落库——未知键静默忽略，不算失败）。

- YAML 块本身不算正文：剥离后再清洗分块，否则检索命中的片段满是元数据行；
- 解析失败不静默：YAML 非法 / 值类型不对时生成**带行号**的可读警告，正文照常入库，
  警告随 ``clean_report["frontmatter_warnings"]`` 落库并出现在上传响应的 message 里；
- 首行 ``---`` 找不到闭合时**不当 frontmatter**——那通常只是水平分割线，剥了就丢正文。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date, datetime

import yaml

# frontmatter 键：与 documents 表的既有字段一一对应，不新增概念
CATEGORY_KEY = "category"
TAGS_KEY = "tags"
CREATED_KEY = "created_at"

_OPEN_RE = re.compile(r"^---\s*$")
_CLOSE_RE = re.compile(r"^(?:---|\.\.\.)\s*$")
# 往后找多少行闭合边界：frontmatter 是文件头，几十行封顶足够，也避免把全文扫一遍
_MAX_SCAN_LINES = 100
_CATEGORY_MAX_LEN = 255  # documents.category 列宽
_TAG_SPLIT_RE = re.compile(r"[,，、]")
_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


@dataclass
class Frontmatter:
    """一次解析的产物：结构化元数据 + 剥离后正文 + 可读警告。"""

    present: bool = False  # 文件是否真的带 frontmatter 块（哪怕块内解析失败）
    body: str = ""  # 剥离 frontmatter 后的正文；无 frontmatter 时原样返回
    category: str = ""
    tags: list[str] = field(default_factory=list)
    created_at: datetime | None = None
    warnings: list[str] = field(default_factory=list)


def parse_frontmatter(text: str) -> Frontmatter:
    """解析 markdown 文本头部的 frontmatter；返回剥离后的正文与元数据。

    任何失败都只产生警告，不抛异常——调用方（入库链路）拿着正文继续走。
    """
    lines = text.split("\n")
    if not lines or not _OPEN_RE.match(lines[0].rstrip("\r")):
        return Frontmatter(body=text)

    close_at = -1
    for index in range(1, min(len(lines), _MAX_SCAN_LINES + 1)):
        if _CLOSE_RE.match(lines[index].rstrip("\r")):
            close_at = index
            break
    if close_at < 0:
        # 没有闭合 ---：按水平分割线处理，整个文件就是正文
        return Frontmatter(body=text)

    result = Frontmatter(present=True)
    yaml_text = "\n".join(lines[1:close_at])
    body_lines = lines[close_at + 1 :]
    if body_lines and body_lines[-1] == "":
        body_lines = body_lines[:-1]  # 闭合 --- 后的换行不算正文的开头空行
    result.body = "\n".join(body_lines)
    _load_fields(yaml_text, result)
    return result


def _load_fields(yaml_text: str, result: Frontmatter) -> None:
    """把 YAML 头解析进 result；失败只写警告。"""
    if not yaml_text.strip():
        return  # 空 frontmatter 合法（Obsidian 允许），不算失败
    try:
        data = yaml.safe_load(yaml_text)
    except (yaml.YAMLError, ValueError) as exc:
        # PyYAML 的裸日期构造器会对非法日历值抛 ValueError，沿用 YAML 失败警告。
        result.warnings.append(_yaml_warning(exc))
        return
    if data is None:
        return
    if not isinstance(data, dict):
        result.warnings.append(
            f"frontmatter 应是「键: 值」形式的键值对，实际解析出 {type(data).__name__}，元数据未入库"
        )
        return

    category = _parse_category(data)
    if isinstance(category, str):
        result.category = category
    else:
        result.warnings.append(
            f"frontmatter 的 {CATEGORY_KEY} 应是文本，实际是 {type(category).__name__}，已忽略"
        )

    tags, tag_warning = _parse_tags(data)
    if tag_warning:
        result.warnings.append(tag_warning)
    result.tags = tags

    created, created_warning = _parse_created(data)
    if created_warning:
        result.warnings.append(created_warning)
    result.created_at = created


def _yaml_warning(exc: yaml.YAMLError | ValueError) -> str:
    """非法 YAML 的可读提示；行号换算成**整个文件**的行号（YAML 头前面还有一个 ``---``）。"""
    detail = str(exc).splitlines()[0] if str(exc) else "未知错误"
    mark = getattr(exc, "problem_mark", None)
    line = getattr(mark, "line", None) if mark is not None else None
    if line is not None:
        return (
            f"frontmatter 第 {int(line) + 2} 行不是合法的 YAML（{detail}），"
            "本文件元数据未入库，正文不受影响"
        )
    return f"frontmatter 不是合法的 YAML（{detail}），本文件元数据未入库，正文不受影响"


def _parse_category(data: dict) -> str | object:
    """category 只收文本；空串按「没写」处理，交给回退规则。"""
    value = data.get(CATEGORY_KEY)
    if value is None:
        return ""
    if isinstance(value, str):
        text = value.strip()
        return text[:_CATEGORY_MAX_LEN]  # 超列宽截断，截断本身不报错
    return value  # 非字符串：返回原值让调用方生成警告


def _parse_tags(data: dict) -> tuple[list[str], str | None]:
    """tags 收 YAML 列表或逗号串；去空去重保序，条目一律转成文本。"""
    value = data.get(TAGS_KEY)
    if value is None:
        return [], None
    items: list
    if isinstance(value, (list, tuple)):
        items = list(value)
    elif isinstance(value, str):
        items = _TAG_SPLIT_RE.split(value)
    else:
        return [], f"frontmatter 的 {TAGS_KEY} 应是列表或逗号分隔的文本，实际是 {type(value).__name__}，已忽略"

    tags: list[str] = []
    for item in items:
        text = str(item).strip() if item is not None else ""
        if text and text not in tags:
            tags.append(text)
    return tags, None


def _parse_created(data: dict) -> tuple[datetime | None, str | None]:
    """created_at 收 YAML 日期（safe_load 会把 ``2026-07-01`` 自动解析成 date）或文本。"""
    value = data.get(CREATED_KEY)
    if value is None:
        return None, None
    if isinstance(value, datetime):
        return _as_naive(value), None
    if isinstance(value, date):
        return datetime(value.year, value.month, value.day), None
    if isinstance(value, str) and value.strip():
        text = value.strip()
        try:
            if _DATE_RE.match(text):
                return datetime.strptime(text, "%Y-%m-%d"), None
            return _as_naive(datetime.fromisoformat(text.replace("Z", "+00:00"))), None
        except ValueError:
            return None, f"frontmatter 的 {CREATED_KEY} 不是可识别的日期（{text}），已忽略"
    return None, f"frontmatter 的 {CREATED_KEY} 应是日期或文本，实际是 {type(value).__name__}，已忽略"


def _as_naive(moment: datetime) -> datetime:
    """入库列是 DATETIME（无时区）：带时区的时间转本机墙钟再剥掉 tzinfo。

    YAML 的裸日期解析出来本来就是 naive；带时区的写法（如 ``+08:00``）转本机时间，
    保存的仍是用户看到的那个时刻，而不是被挪到 UTC。
    """
    if moment.tzinfo is None:
        return moment
    return moment.astimezone().replace(tzinfo=None)


def date_from_name(filename: str) -> datetime | None:
    """文件名回退日期：``2026-07-01-xxx.md`` / ``2026-07-01 xxx.md`` 取前缀当 created_at。"""
    stem = filename.rsplit(".", 1)[0] if "." in filename else filename
    # 回归修复：`str.strip()` 不吃 U+FEFF（它不是空白字符）⇒ 带 BOM 的文件名取不到日期。
    # 清洗集加上前导 `\ufeff`；既有 `strip()` 保留 ⇒ 空格 / 制表符语义不被收窄。
    cleaned = stem.strip().lstrip("\ufeff").strip()
    match = re.match(r"^(\d{4}-\d{2}-\d{2})", cleaned)
    if not match:
        return None
    try:
        return datetime.strptime(match.group(1), "%Y-%m-%d")
    except ValueError:  # 形如 2026-13-45 的假日期不算数
        return None
