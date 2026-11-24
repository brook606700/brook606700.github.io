#!/usr/bin/env python3
"""Read-only SEO sanity checker for a static HTML site (Python 3.10+, stdlib only).

Run from the repository root:
    python scripts/seo_check.py
    python scripts/seo_check.py --strict

This script reports potential issues; it does not modify files and does not replace
Google Search Console, a real browser test, or a full crawler.
"""
from __future__ import annotations

import argparse
import re
import sys
import xml.etree.ElementTree as ET
from collections import Counter
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import unquote, urlsplit

SITE_HOST = "brook606700.github.io"
SKIP_DIRS = {".git", ".github", "node_modules", ".venv", "venv"}


class PageParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.title_parts: list[str] = []
        self.in_title = False
        self.meta_description: str | None = None
        self.canonicals: list[str] = []
        self.h1_texts: list[str] = []
        self.heading_levels: list[int] = []
        self.missing_alt: list[str] = []
        self.local_refs: list[tuple[str, str]] = []
        self._heading_level: int | None = None
        self._heading_text: list[str] = []
        self._img_index = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attr = {k.lower(): v for k, v in attrs}
        tag = tag.lower()
        if tag == "title":
            self.in_title = True
        elif tag == "meta":
            name = (attr.get("name") or "").lower()
            if name == "description":
                self.meta_description = attr.get("content") or ""
        elif tag == "link" and "canonical" in (attr.get("rel") or "").lower().split():
            self.canonicals.append(attr.get("href") or "")
        elif re.fullmatch(r"h[1-6]", tag):
            self._heading_level = int(tag[1])
            self._heading_text = []
            self.heading_levels.append(self._heading_level)
        elif tag == "img":
            self._img_index += 1
            if "alt" not in attr:
                self.missing_alt.append(f"img #{self._img_index}: {attr.get('src') or '(no src)'}")
        for key in ("href", "src"):
            value = attr.get(key)
            if value:
                self.local_refs.append((key, value))

    def handle_endtag(self, tag: str) -> None:
        tag = tag.lower()
        if tag == "title":
            self.in_title = False
        if re.fullmatch(r"h[1-6]", tag) and self._heading_level == int(tag[1]):
            value = " ".join("".join(self._heading_text).split())
            if self._heading_level == 1:
                self.h1_texts.append(value)
            self._heading_level = None
            self._heading_text = []

    def handle_data(self, data: str) -> None:
        if self.in_title:
            self.title_parts.append(data)
        if self._heading_level is not None:
            self._heading_text.append(data)

    @property
    def title(self) -> str:
        return " ".join("".join(self.title_parts).split())


def iter_html_files(root: Path):
    for path in root.rglob("*.html"):
        if any(part in SKIP_DIRS for part in path.parts):
            continue
        yield path


def resolve_local_target(root: Path, source_file: Path, raw_url: str) -> Path | None:
    raw_url = raw_url.strip()
    if not raw_url or raw_url.startswith(("#", "mailto:", "tel:", "javascript:", "data:")):
        return None
    parsed = urlsplit(raw_url)
    if parsed.scheme and parsed.scheme not in {"http", "https"}:
        return None
    if parsed.netloc and parsed.netloc.lower() != SITE_HOST:
        return None
    url_path = unquote(parsed.path)
    if not url_path:
        return None
    if url_path.startswith("/"):
        candidate = root / url_path.lstrip("/")
    else:
        candidate = source_file.parent / url_path
    candidate = candidate.resolve()
    if candidate.is_dir():
        candidate = candidate / "index.html"
    elif not candidate.suffix and (candidate / "index.html").is_file():
        candidate = candidate / "index.html"
    return candidate


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--root", default=".", help="repository root (default: current directory)")
    ap.add_argument("--strict", action="store_true", help="return exit code 1 for reported errors")
    args = ap.parse_args()
    root = Path(args.root).resolve()
    html_files = sorted(iter_html_files(root))
    if not html_files:
        print(f"ERROR: no HTML files found under {root}")
        return 2

    errors: list[str] = []
    warnings: list[str] = []
    totals = Counter()
    for file in html_files:
        rel = file.relative_to(root).as_posix()
        content = file.read_text(encoding="utf-8", errors="replace")
        parser = PageParser()
        try:
            parser.feed(content)
        except Exception as exc:  # keep scanning other pages
            warnings.append(f"{rel}: HTML parse warning: {exc}")

        doctype_count = len(re.findall(r"<!doctype\s+html\s*>", content, flags=re.I))
        if doctype_count == 0:
            warnings.append(f"{rel}: no HTML5 doctype found")
        elif doctype_count > 1:
            errors.append(f"{rel}: duplicate DOCTYPE ({doctype_count})")

        if not parser.title:
            warnings.append(f"{rel}: missing <title>")
        elif len(parser.title) > 60:
            warnings.append(f"{rel}: title is {len(parser.title)} chars (review >60)")

        if parser.meta_description is None:
            warnings.append(f"{rel}: missing meta description")
        else:
            desc_len = len(parser.meta_description.strip())
            if desc_len < 70 or desc_len > 160:
                warnings.append(f"{rel}: meta description is {desc_len} chars (review 70–160)")

        if len(parser.h1_texts) != 1:
            warnings.append(f"{rel}: expected 1 H1, found {len(parser.h1_texts)}")
        if len(parser.canonicals) != 1:
            warnings.append(f"{rel}: expected 1 canonical, found {len(parser.canonicals)}")
        if parser.missing_alt:
            warnings.append(f"{rel}: images without an alt attribute: {len(parser.missing_alt)}")
            for item in parser.missing_alt[:3]:
                warnings.append(f"  {item}")

        prev = 0
        for level in parser.heading_levels:
            if prev and level > prev + 1:
                warnings.append(f"{rel}: heading order skips H{prev} → H{level}")
                break
            prev = level

        for key, value in parser.local_refs:
            target = resolve_local_target(root, file, value)
            if target is None:
                continue
            try:
                target.relative_to(root)
            except ValueError:
                warnings.append(f"{rel}: local reference escapes repo root: {value}")
                continue
            if not target.exists():
                errors.append(f"{rel}: broken local {key}: {value}")

    print(f"HTML files scanned: {len(html_files)}")
    print(f"Errors: {len(errors)} | Warnings: {len(warnings)}")
    for line in errors:
        print(f"ERROR: {line}")
    for line in warnings:
        print(f"WARN:  {line}")

    sitemap = root / "sitemap.xml"
    if sitemap.exists():
        try:
            tree = ET.parse(sitemap)
            ns = {"sm": "http://www.sitemaps.org/schemas/sitemap/0.9"}
            urls = [el.text or "" for el in tree.findall(".//sm:loc", ns)]
            lastmods = [el.text or "" for el in tree.findall(".//sm:lastmod", ns)]
            print(f"Sitemap URLs: {len(urls)} | Unique URLs: {len(set(urls))} | lastmod values: {sorted(set(lastmods))}")
            if len(urls) != len(set(urls)):
                errors.append("sitemap.xml contains duplicate <loc> URLs")
        except ET.ParseError as exc:
            errors.append(f"sitemap.xml is not valid XML: {exc}")
    else:
        warnings.append("sitemap.xml not found at repository root")

    print("Note: this is a local static check, not a Google index/rank test or full browser audit.")
    return 1 if args.strict and errors else 0


if __name__ == "__main__":
    sys.exit(main())
