"""
Public Near Outpost announcements loaded from a versioned content file.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
import html
from pathlib import Path
import re
from typing import Any, Iterable, List, Optional, Tuple

import yaml

from app.core.config import ROOT_DIR


ANNOUNCEMENTS_FILE = ROOT_DIR / "docs" / "minecraft" / "announcements.yml"
DEFAULT_CATEGORY = "업데이트"
CATEGORY_ORDER = (
    "업데이트",
    "점검/장애",
    "기능/콘텐츠",
    "정책/규정",
    "이벤트",
    "웹/가이드",
    "후원/인프라",
)

_ID_PATTERN = re.compile(r"^[a-z0-9][a-z0-9-]{2,100}$")
_URL_RE = re.compile(r"(?<![\"'=])(https?://[^\s<]+)")
_MARKDOWN_LINK_RE = re.compile(r"\[([^\]]+)\]\((https?://[^)\s]+)\)")
_MARKDOWN_IMAGE_RE = re.compile(r"!\[([^\]]*)\]\((/static/[^)\s]+|https?://[^)\s]+)\)")


@dataclass(frozen=True)
class Announcement:
    id: str
    title: str
    author: str
    date: str
    category: str
    tags: Tuple[str, ...]
    body: str
    html: str
    pinned: bool = False
    source: str = "discord"

    @property
    def published_at(self) -> datetime:
        try:
            return datetime.fromisoformat(self.date)
        except ValueError:
            return datetime.min

    @property
    def display_date(self) -> str:
        dt = self.published_at
        if dt == datetime.min:
            return self.date
        return dt.strftime("%Y.%m.%d %H:%M")


def _normalize_tags(value: Any) -> Tuple[str, ...]:
    if isinstance(value, str):
        cleaned = value.strip()
        return (cleaned,) if cleaned else tuple()
    if isinstance(value, list):
        return tuple(str(item).strip() for item in value if str(item).strip())
    return tuple()


def _normalize_category(value: Any) -> str:
    category = str(value or "").strip()
    return category if category in CATEGORY_ORDER else DEFAULT_CATEGORY


def _safe_href(url: str) -> str:
    cleaned = html.unescape(url).strip()
    if not cleaned.startswith(("https://", "http://", "/static/")):
        return "#"
    return html.escape(cleaned, quote=True)


def _render_inline(text: str) -> str:
    escaped = html.escape(text, quote=False)
    code_fragments: list[str] = []
    image_fragments: list[str] = []
    link_fragments: list[str] = []

    def stash_code(match: re.Match[str]) -> str:
        code_fragments.append(
            '<code class="rounded bg-black/30 px-1.5 py-0.5 text-[0.94em] text-orange-200">'
            f"{match.group(1)}</code>"
        )
        return f"\x00CODE{len(code_fragments) - 1}\x00"

    escaped = re.sub(r"`([^`]+)`", stash_code, escaped)

    def render_markdown_image(match: re.Match[str]) -> str:
        alt = match.group(1)
        src = _safe_href(match.group(2))
        image_fragments.append(
            f'<img src="{src}" alt="{alt}" class="my-4 rounded-lg border border-white/10 max-w-full md:max-w-md mx-auto block" />'
        )
        return f"\x00IMAGE{len(image_fragments) - 1}\x00"

    escaped = _MARKDOWN_IMAGE_RE.sub(render_markdown_image, escaped)

    def render_markdown_link(match: re.Match[str]) -> str:
        label = match.group(1)
        href = _safe_href(match.group(2))
        link_fragments.append(
            f'<a href="{href}" target="_blank" rel="noopener noreferrer" '
            'class="text-cyan-200 underline decoration-cyan-400/40 underline-offset-4 hover:text-cyan-100">'
            f"{label}</a>"
        )
        return f"\x00LINK{len(link_fragments) - 1}\x00"

    escaped = _MARKDOWN_LINK_RE.sub(render_markdown_link, escaped)

    def render_bare_url(match: re.Match[str]) -> str:
        raw_url = match.group(1).rstrip(".,)")
        trailing = match.group(1)[len(raw_url):]
        href = _safe_href(raw_url)
        label = html.escape(html.unescape(raw_url), quote=False)
        return (
            f'<a href="{href}" target="_blank" rel="noopener noreferrer" '
            'class="break-all text-cyan-200 underline decoration-cyan-400/40 underline-offset-4 hover:text-cyan-100">'
            f"{label}</a>{trailing}"
        )

    escaped = _URL_RE.sub(render_bare_url, escaped)
    escaped = re.sub(r"\*\*([^*]+)\*\*", r'<strong class="font-semibold text-white">\1</strong>', escaped)
    escaped = re.sub(r"\*([^*]+)\*", r"<em>\1</em>", escaped)

    for index, fragment in enumerate(code_fragments):
        escaped = escaped.replace(f"\x00CODE{index}\x00", fragment)
    for index, fragment in enumerate(image_fragments):
        escaped = escaped.replace(f"\x00IMAGE{index}\x00", fragment)
    for index, fragment in enumerate(link_fragments):
        escaped = escaped.replace(f"\x00LINK{index}\x00", fragment)
    return escaped


def render_markdown(markdown_text: str | None) -> str:
    """
    Render a constrained, escaped Markdown subset for public announcements.
    """
    if not markdown_text:
        return ""

    lines = markdown_text.replace("\r\n", "\n").split("\n")
    parts: List[str] = []
    paragraph: List[str] = []
    list_open: Optional[str] = None
    in_code = False
    code_lines: List[str] = []

    def close_paragraph() -> None:
        if not paragraph:
            return
        rendered = [_render_inline(line.strip()) for line in paragraph if line.strip()]
        paragraph.clear()
        if rendered:
            parts.append(f'<p class="mb-4 leading-8 text-slate-200">{"<br>".join(rendered)}</p>')

    def close_list() -> None:
        nonlocal list_open
        if list_open:
            parts.append(f"</{list_open}>")
            list_open = None

    def open_list(kind: str) -> None:
        nonlocal list_open
        if list_open == kind:
            return
        close_list()
        class_name = "list-decimal" if kind == "ol" else "list-disc"
        parts.append(f'<{kind} class="mb-5 space-y-1 pl-6 {class_name} text-slate-200">')
        list_open = kind

    def append_code_block() -> None:
        code = html.escape("\n".join(code_lines), quote=False)
        parts.append(
            '<pre class="mb-5 overflow-x-auto rounded-lg border border-white/10 '
            'bg-black/40 p-4 text-sm text-slate-200"><code>'
            f"{code}</code></pre>"
        )

    for raw_line in lines:
        line = raw_line.rstrip("\n")
        stripped = line.strip()

        if in_code:
            if stripped.startswith("```"):
                append_code_block()
                in_code = False
                code_lines = []
            else:
                code_lines.append(line)
            continue

        if stripped.startswith("```"):
            close_paragraph()
            close_list()
            in_code = True
            code_lines = []
            continue

        if not stripped:
            close_paragraph()
            close_list()
            continue

        if stripped == "---":
            close_paragraph()
            close_list()
            parts.append('<hr class="my-6 border-white/10">')
            continue

        heading_match = re.match(r"^(#{1,3})\s+(.+)$", stripped)
        if heading_match:
            close_paragraph()
            close_list()
            level = len(heading_match.group(1))
            heading = _render_inline(heading_match.group(2).strip())
            if level == 1:
                parts.append(f'<h1 class="mb-4 text-3xl font-black text-white">{heading}</h1>')
            elif level == 2:
                parts.append(f'<h2 class="mb-3 mt-7 text-2xl font-bold text-white">{heading}</h2>')
            else:
                parts.append(f'<h3 class="mb-2 mt-5 text-lg font-semibold text-orange-100">{heading}</h3>')
            continue

        unordered_match = re.match(r"^[-*]\s+(.+)$", stripped)
        if unordered_match:
            close_paragraph()
            open_list("ul")
            parts.append(f"<li>{_render_inline(unordered_match.group(1).strip())}</li>")
            continue

        ordered_match = re.match(r"^\d+[.)]\s+(.+)$", stripped)
        if ordered_match:
            close_paragraph()
            open_list("ol")
            parts.append(f"<li>{_render_inline(ordered_match.group(1).strip())}</li>")
            continue

        if stripped.startswith("> "):
            close_paragraph()
            close_list()
            parts.append(
                '<blockquote class="mb-4 rounded-r border-l-2 border-orange-400/60 bg-orange-500/10 px-4 py-3 '
                f'text-slate-200">{_render_inline(stripped[2:].strip())}</blockquote>'
            )
            continue

        close_list()
        paragraph.append(line)

    if in_code:
        append_code_block()

    close_paragraph()
    close_list()
    return "\n".join(parts)


def _announcement_from_dict(item: dict[str, Any]) -> Optional[Announcement]:
    raw_id = str(item.get("id") or "").strip()
    if not _ID_PATTERN.match(raw_id):
        return None

    title = str(item.get("title") or "").strip()
    body = str(item.get("body") or "").strip()
    date = str(item.get("date") or "").strip()
    if not title or not body or not date:
        return None

    return Announcement(
        id=raw_id,
        title=title,
        author=str(item.get("author") or "NEAR OUTPOST").strip(),
        date=date,
        category=_normalize_category(item.get("category")),
        tags=_normalize_tags(item.get("tags")),
        body=body,
        html=render_markdown(body),
        pinned=bool(item.get("pinned")),
        source=str(item.get("source") or "discord").strip(),
    )


def _load_raw_announcements(path: Path = ANNOUNCEMENTS_FILE) -> Iterable[dict[str, Any]]:
    try:
        payload = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except OSError:
        return []
    except yaml.YAMLError:
        return []

    announcements = payload.get("announcements") if isinstance(payload, dict) else None
    if not isinstance(announcements, list):
        return []
    return [item for item in announcements if isinstance(item, dict)]


def list_announcements(
    *,
    category: str | None = None,
    path: Path = ANNOUNCEMENTS_FILE,
) -> List[Announcement]:
    selected_category = str(category or "").strip()
    announcements = [
        announcement
        for item in _load_raw_announcements(path)
        if (announcement := _announcement_from_dict(item)) is not None
    ]
    if selected_category:
        announcements = [item for item in announcements if item.category == selected_category]
    return sorted(announcements, key=lambda item: (item.pinned, item.published_at), reverse=True)


def get_announcement(announcement_id: str, *, path: Path = ANNOUNCEMENTS_FILE) -> Optional[Announcement]:
    target = str(announcement_id or "").strip()
    if not _ID_PATTERN.match(target):
        return None
    for announcement in list_announcements(path=path):
        if announcement.id == target:
            return announcement
    return None


def category_counts(announcements: Iterable[Announcement]) -> list[dict[str, Any]]:
    counts = {category: 0 for category in CATEGORY_ORDER}
    for announcement in announcements:
        counts.setdefault(announcement.category, 0)
        counts[announcement.category] += 1
    return [
        {"name": category, "count": count}
        for category, count in counts.items()
        if count > 0
    ]
