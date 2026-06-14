"""
Backend operations documentation service.

Loads markdown files from docs/minecraft/backend and renders safe HTML.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import html
from pathlib import Path
import re
from typing import Any, List, Optional, Tuple

import yaml

from app.core.config import ROOT_DIR

DOCS_DIR = ROOT_DIR / "docs" / "minecraft" / "backend"
_SLUG_PATTERN = re.compile(r"^[a-z0-9][a-z0-9-]{0,80}$")
_TITLE_PATTERN = re.compile(r"^\s*#\s+(.+?)\s*$", re.MULTILINE)
_VALID_AUDIENCES = frozenset({"admin_only", "privileged_staff"})
_DEFAULT_AUDIENCE = "admin_only"
_DEFAULT_OWNER = "operations"


@dataclass(frozen=True)
class BackendDocSummary:
    slug: str
    title: str
    audience: str
    owner: str
    last_reviewed_at: str
    tags: Tuple[str, ...]
    source_path: str
    updated_at: str


@dataclass(frozen=True)
class BackendDoc(BackendDocSummary):
    raw: str
    html: str


def _doc_sort_key(path: Path) -> tuple:
    stem = path.stem
    if stem == "index":
        return (0, 0, stem)
    match = re.match(r"^(\d+)-", stem)
    if match:
        return (1, int(match.group(1)), stem)
    return (2, 9999, stem)


def _is_valid_slug(slug: str) -> bool:
    return bool(_SLUG_PATTERN.match(slug))


def _extract_title(markdown_text: str, slug: str) -> str:
    match = _TITLE_PATTERN.search(markdown_text)
    if match:
        return match.group(1).strip()
    return slug.replace("-", " ").strip().title()


def _extract_title_with_meta(markdown_text: str, slug: str, metadata: dict[str, Any]) -> str:
    title = metadata.get("title")
    if isinstance(title, str) and title.strip():
        return title.strip()
    return _extract_title(markdown_text, slug=slug)


def _normalize_audience(value: Any) -> str:
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized in _VALID_AUDIENCES:
            return normalized
    return _DEFAULT_AUDIENCE


def _normalize_owner(value: Any) -> str:
    if isinstance(value, str) and value.strip():
        return value.strip()
    return _DEFAULT_OWNER


def _normalize_last_reviewed(value: Any) -> str:
    if isinstance(value, str):
        return value.strip()
    return ""


def _normalize_tags(value: Any) -> Tuple[str, ...]:
    if isinstance(value, str):
        cleaned = value.strip()
        return (cleaned,) if cleaned else tuple()
    if isinstance(value, list):
        tags = []
        for item in value:
            if isinstance(item, str) and item.strip():
                tags.append(item.strip())
        return tuple(tags)
    return tuple()


def _updated_at_iso(path: Path) -> str:
    ts = path.stat().st_mtime
    return datetime.fromtimestamp(ts, tz=timezone.utc).isoformat()


def _relative_source_path(path: Path) -> str:
    try:
        return path.relative_to(ROOT_DIR).as_posix()
    except ValueError:
        return path.as_posix()


def _split_front_matter(markdown_text: str) -> Tuple[dict[str, Any], str]:
    """
    Split optional YAML front matter from markdown body.

    If parsing fails, returns empty metadata and the original markdown.
    """
    if not markdown_text.startswith("---"):
        return {}, markdown_text

    lines = markdown_text.splitlines()
    if not lines or lines[0].strip() != "---":
        return {}, markdown_text

    closing_index = None
    for i in range(1, len(lines)):
        if lines[i].strip() == "---":
            closing_index = i
            break

    if closing_index is None:
        return {}, markdown_text

    raw_meta = "\n".join(lines[1:closing_index])
    body = "\n".join(lines[closing_index + 1:]).lstrip("\n")

    try:
        parsed = yaml.safe_load(raw_meta) or {}
    except Exception:
        return {}, markdown_text

    if not isinstance(parsed, dict):
        return {}, markdown_text

    return parsed, body


def _can_access_doc(*, audience: str, is_admin_user: bool) -> bool:
    if is_admin_user:
        return True
    return audience == "privileged_staff"


def _render_inline(text: str) -> str:
    escaped = html.escape(text, quote=False)
    escaped = re.sub(
        r"`([^`]+)`",
        r"<code class=\"px-1 py-0.5 rounded bg-slate-900 text-cyan-300 text-[0.95em]\">\1</code>",
        escaped,
    )
    escaped = re.sub(r"\*\*([^*]+)\*\*", r"<strong class=\"font-semibold text-white\">\1</strong>", escaped)
    escaped = re.sub(r"\*([^*]+)\*", r"<em>\1</em>", escaped)
    return escaped


def render_markdown(markdown_text: str) -> str:
    """
    Render a constrained markdown subset to safe HTML.
    Supported blocks: headings, paragraphs, bullet lists, fenced code, and hr.
    """
    lines = markdown_text.replace("\r\n", "\n").split("\n")

    parts: List[str] = []
    paragraph: List[str] = []
    list_open = False
    in_code = False
    code_lines: List[str] = []

    def close_paragraph() -> None:
        if not paragraph:
            return
        text = " ".join(p.strip() for p in paragraph).strip()
        paragraph.clear()
        if text:
            parts.append(f"<p class=\"mb-4 leading-7 text-slate-200\">{_render_inline(text)}</p>")

    def close_list() -> None:
        nonlocal list_open
        if list_open:
            parts.append("</ul>")
            list_open = False

    for raw_line in lines:
        line = raw_line.rstrip("\n")
        stripped = line.strip()

        if in_code:
            if stripped.startswith("```"):
                code = html.escape("\n".join(code_lines), quote=False)
                parts.append(
                    "<pre class=\"mb-5 overflow-x-auto rounded-lg border border-slate-700/70 "
                    "bg-[#05070d] p-4 text-sm text-slate-200\"><code>"
                    f"{code}</code></pre>"
                )
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
            parts.append("<hr class=\"my-6 border-slate-700/60\">")
            continue

        heading_match = re.match(r"^(#{1,3})\s+(.+)$", stripped)
        if heading_match:
            close_paragraph()
            close_list()
            level = len(heading_match.group(1))
            heading = _render_inline(heading_match.group(2).strip())
            if level == 1:
                parts.append(f"<h1 class=\"mb-4 text-3xl font-bold text-white\">{heading}</h1>")
            elif level == 2:
                parts.append(f"<h2 class=\"mb-3 mt-8 text-2xl font-semibold text-white\">{heading}</h2>")
            else:
                parts.append(f"<h3 class=\"mb-2 mt-6 text-xl font-semibold text-slate-100\">{heading}</h3>")
            continue

        if stripped.startswith("- "):
            close_paragraph()
            if not list_open:
                parts.append("<ul class=\"mb-5 list-disc space-y-1 pl-6 text-slate-200\">")
                list_open = True
            parts.append(f"<li>{_render_inline(stripped[2:].strip())}</li>")
            continue

        close_list()
        paragraph.append(line)

    if in_code:
        code = html.escape("\n".join(code_lines), quote=False)
        parts.append(
            "<pre class=\"mb-5 overflow-x-auto rounded-lg border border-slate-700/70 "
            "bg-[#05070d] p-4 text-sm text-slate-200\"><code>"
            f"{code}</code></pre>"
        )

    close_paragraph()
    close_list()
    return "\n".join(parts)


def _load_doc(slug: str, path: Path) -> Optional[BackendDoc]:
    try:
        raw_text = path.read_text(encoding="utf-8")
    except OSError:
        return None

    metadata, markdown_body = _split_front_matter(raw_text)
    title = _extract_title_with_meta(markdown_body, slug=slug, metadata=metadata)
    audience = _normalize_audience(metadata.get("audience"))
    owner = _normalize_owner(metadata.get("owner"))
    last_reviewed_at = _normalize_last_reviewed(metadata.get("last_reviewed_at"))
    tags = _normalize_tags(metadata.get("tags"))

    return BackendDoc(
        slug=slug,
        title=title,
        audience=audience,
        owner=owner,
        last_reviewed_at=last_reviewed_at,
        tags=tags,
        source_path=_relative_source_path(path),
        updated_at=_updated_at_iso(path),
        raw=markdown_body,
        html=render_markdown(markdown_body),
    )


def list_docs(*, is_admin_user: bool = True) -> List[BackendDocSummary]:
    if not DOCS_DIR.exists():
        return []

    docs: List[BackendDocSummary] = []
    for path in sorted(DOCS_DIR.glob("*.md"), key=_doc_sort_key):
        slug = path.stem
        if not _is_valid_slug(slug):
            continue
        doc = _load_doc(slug=slug, path=path)
        if not doc:
            continue
        if not _can_access_doc(audience=doc.audience, is_admin_user=is_admin_user):
            continue
        docs.append(
            BackendDocSummary(
                slug=doc.slug,
                title=doc.title,
                audience=doc.audience,
                owner=doc.owner,
                last_reviewed_at=doc.last_reviewed_at,
                tags=doc.tags,
                source_path=doc.source_path,
                updated_at=doc.updated_at,
            )
        )
    return docs


def get_doc(slug: str, *, is_admin_user: bool = True) -> Optional[BackendDoc]:
    if not _is_valid_slug(slug):
        return None

    path = (DOCS_DIR / f"{slug}.md").resolve()
    docs_root = DOCS_DIR.resolve()
    if docs_root not in path.parents:
        return None
    if not path.exists() or path.suffix != ".md":
        return None

    doc = _load_doc(slug=slug, path=path)
    if not doc:
        return None

    if not _can_access_doc(audience=doc.audience, is_admin_user=is_admin_user):
        return None

    return doc
