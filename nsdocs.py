#!/usr/bin/env python3
"""nsdocs - a lightweight, zero-overhead static wiki generator.

Reads Markdown from ``docs/wiki-src/`` and writes plain HTML pages to
``docs/wiki/``. Configuration lives in ``nsdocs.yml`` in the current
working directory (or passed via ``--config``).
"""

from __future__ import annotations

import argparse
import difflib
import html
import re
import shutil
import sys
import unicodedata
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional, Tuple

try:
    import yaml
except ImportError:  # pragma: no cover
    sys.stderr.write("nsdocs: missing dependency 'pyyaml' (pip install pyyaml)\n")
    sys.exit(2)

__version__ = "0.1.0"

SRC_DIR = Path("docs/wiki-src")
OUT_DIR = Path("docs/wiki")

DEFAULTS: Dict[str, Any] = {
    "site_name": "nsdocs",
    "brand_name": None,  # falls back to site_name
    "site_url": "",
    "accent_color": "#238FC9",
    "license_text": "",
    "repo": {"url": "", "branch": "main"},
    "assets": {"css": None, "favicon_dark": None, "favicon_light": None},
    "features": {
        "theme_toggle": True,
        "clean_stale_files": True,
        "edit_button": True,
        "copy_button": True,
    },
    "nav": {},
}


# --------------------------------------------------------------------------
# Configuration
# --------------------------------------------------------------------------

def deep_merge(base: Dict[str, Any], override: Dict[str, Any]) -> Dict[str, Any]:
    """Recursively merge ``override`` into ``base`` and return a new dict."""
    out = dict(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = deep_merge(out[key], value)
        else:
            out[key] = value
    return out


def load_config(config_path: Path) -> Dict[str, Any]:
    """Load nsdocs.yml from disk and apply defaults."""
    if not config_path.is_file():
        sys.stderr.write(f"nsdocs: config not found: {config_path}\n")
        sys.exit(2)
    try:
        data = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError as exc:
        sys.stderr.write(f"nsdocs: invalid YAML in {config_path}: {exc}\n")
        sys.exit(2)
    if not isinstance(data, dict):
        sys.stderr.write(f"nsdocs: config root must be a mapping: {config_path}\n")
        sys.exit(2)
    return deep_merge(DEFAULTS, data)


# --------------------------------------------------------------------------
# Front matter
# --------------------------------------------------------------------------

FRONT_MATTER_RE = re.compile(r"\A---\s*\n(.*?)\n?---\s*\n?", re.DOTALL)


def parse_front_matter(text: str) -> Tuple[Dict[str, Any], str]:
    """Split leading ``---`` YAML front matter from a Markdown document.

    Returns ``(meta, body)``. When no front matter is present the metadata
    dict is empty and ``body`` is the untouched input.
    """
    match = FRONT_MATTER_RE.match(text)
    if not match:
        return {}, text
    try:
        meta = yaml.safe_load(match.group(1)) or {}
    except yaml.YAMLError:
        meta = {}
    if not isinstance(meta, dict):
        meta = {}
    return meta, text[match.end():]


# --------------------------------------------------------------------------
# Markdown -> HTML
# --------------------------------------------------------------------------

def slugify(text: str) -> str:
    """Lowercased, ASCII-only, hyphen-separated heading anchor."""
    text = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode("ascii")
    text = re.sub(r"[^\w\s-]", "", text, flags=re.UNICODE)
    text = re.sub(r"[\s_]+", "-", text.strip().lower())
    return text.strip("-") or "section"


def rewrite_md_links(href: str) -> str:
    """Rewrite relative ``.md`` hrefs to ``.html``; leave absolute URLs alone."""
    lowered = href.lower()
    if lowered.startswith(("http://", "https://", "mailto:", "#", "data:")):
        return href
    if lowered.endswith(".md"):
        return href[:-3] + ".html"
    return href


def render_inline(text: str) -> str:
    """Inline formatting: code, bold, italics, links, raw .md link rewriting."""
    placeholders: List[str] = []

    def stash(html_fragment: str) -> str:
        placeholders.append(html_fragment)
        return f"\x00NSDOCS{len(placeholders) - 1}\x00"

    # Inline code first so its content is never further formatted.
    text = re.sub(r"`([^`]+)`", lambda m: stash(f"<code>{html.escape(m.group(1))}</code>"), text)
    # Images: ![alt](src)
    text = re.sub(
        r"!\[([^\]]*)\]\(([^)\s]+)\)",
        lambda m: stash(
            f'<img src="{html.escape(rewrite_md_links(m.group(2)), quote=True)}" '
            f'alt="{html.escape(m.group(1), quote=True)}">'
        ),
        text,
    )
    # Links: [label](href)
    text = re.sub(
        r"\[([^\]]+)\]\(([^)\s]+)\)",
        lambda m: stash(
            f'<a href="{html.escape(rewrite_md_links(m.group(2)), quote=True)}">'
            f"{render_inline(m.group(1))}</a>"
        ),
        text,
    )
    # Escape everything that survived, then add tags that are allowed through.
    text = html.escape(text, quote=False)
    text = re.sub(r"\*\*([^*]+)\*\*", r"<strong>\1</strong>", text)
    text = re.sub(r"(?<!\*)\*([^*\s][^*]*)\*(?!\*)", r"<em>\1</em>", text)
    # Restore stashed fragments.
    return re.sub(r"\x00NSDOCS(\d+)\x00", lambda m: placeholders[int(m.group(1))], text)


def render_table(rows: List[str]) -> str:
    """Render a collected GFM table (header, delimiter, body rows)."""
    def cells(line: str) -> List[str]:
        line = line.strip()
        if line.startswith("|"):
            line = line[1:]
        if line.endswith("|"):
            line = line[:-1]
        return [c.strip() for c in line.split("|")]

    header = cells(rows[0])
    body = [cells(r) for r in rows[2:]]
    head = "".join(f"<th>{render_inline(c)}</th>" for c in header)
    body_rows = "".join(
        "<tr>" + "".join(f"<td>{render_inline(c)}</td>" for c in row) + "</tr>" for row in body
    )
    return f"<table><thead><tr>{head}</tr></thead><tbody>{body_rows}</tbody></table>"


ADMONITIONS = {
    "note": "note",
    "warning": "warning",
    "important": "important",
    "tip": "tip",
    "caution": "warning",
}


def md_to_html(markdown: str, copy_button: bool = True) -> Tuple[Dict[str, Any], str]:
    """Convert raw Markdown to ``(front_matter, html_fragment)``."""
    meta, body = parse_front_matter(markdown)
    lines = body.splitlines()
    out: List[str] = []
    i = 0
    n = len(lines)

    def flush_paragraph(buf: List[str]) -> None:
        if buf:
            out.append(f"<p>{render_inline(' '.join(buf))}</p>")
            buf.clear()

    paragraph: List[str] = []
    while i < n:
        line = lines[i]
        stripped = line.strip()

        # Fenced code blocks -------------------------------------------------
        fence = re.match(r"^```\s*([\w+-]*)\s*$", stripped)
        if fence:
            flush_paragraph(paragraph)
            lang = fence.group(1) or ""
            code: List[str] = []
            i += 1
            while i < n and not lines[i].strip().startswith("```"):
                code.append(lines[i])
                i += 1
            i += 1  # closing fence
            escaped = html.escape("\n".join(code))
            lang_attr = f' class="language-{html.escape(lang, quote=True)}"' if lang else ""
            btn = '<button class="copy-btn" type="button">Copy</button>' if copy_button else ""
            out.append(f'<div class="code-block"><pre>{btn}<code{lang_attr}>{escaped}</code></pre></div>')
            continue

        # Admonitions / callouts --------------------------------------------
        if stripped.startswith(">"):
            quote: List[str] = []
            while i < n and lines[i].strip().startswith(">"):
                quote.append(lines[i].strip()[1:].strip())
                i += 1
            if quote:
                m = re.match(r"^\[!(\w+)\]\s*(.*)$", quote[0], re.IGNORECASE)
                kind = ADMONITIONS.get(m.group(1).lower()) if m else None
                if kind:
                    flush_paragraph(paragraph)
                    title = m.group(2).strip() or kind.capitalize()
                    inner = " ".join(quote[1:])
                    body_p = f"<p>{render_inline(inner)}</p>" if inner else ""
                    out.append(
                        f'<div class="admonition admonition-{kind}">'
                        f'<p class="admonition-title">{render_inline(title)}</p>'
                        f"{body_p}</div>"
                    )
                    continue
                # Regular blockquote.
                flush_paragraph(paragraph)
                inner = " ".join(quote)
                out.append(f"<blockquote><p>{render_inline(inner)}</p></blockquote>")
                continue

        # Tables --------------------------------------------------------------
        if stripped.startswith("|") and i + 1 < n and re.match(r"^\s*\|?[\s:|-]+\|?\s*$", lines[i + 1]) and "-" in lines[i + 1]:
            flush_paragraph(paragraph)
            table_rows: List[str] = [lines[i]]
            i += 1
            while i < n and lines[i].strip().startswith("|"):
                table_rows.append(lines[i])
                i += 1
            out.append(render_table(table_rows))
            continue

        # Headings ------------------------------------------------------------
        heading = re.match(r"^(#{1,3})\s+(.*)$", stripped)
        if heading:
            flush_paragraph(paragraph)
            level = len(heading.group(1))
            content = heading.group(2).strip()
            anchor = slugify(content)
            out.append(f'<h{level} id="{anchor}">{render_inline(content)}</h{level}>')
            i += 1
            continue

        # Lists ----------------------------------------------------------------
        ul_match = re.match(r"^[-*+]\s+(.*)$", stripped)
        ol_match = re.match(r"^\d+[.)]\s+(.*)$", stripped)
        if ul_match or ol_match:
            flush_paragraph(paragraph)
            ordered = bool(ol_match)
            tag = "ol" if ordered else "ul"
            items: List[str] = []
            while i < n:
                current = lines[i].strip()
                item = re.match(r"^[-*+]\s+(.*)$", current) if not ordered else re.match(r"^\d+[.)]\s+(.*)$", current)
                if not item:
                    break
                items.append(f"<li>{render_inline(item.group(1))}</li>")
                i += 1
            out.append(f"<{tag}>" + "".join(items) + f"</{tag}>")
            continue

        # Blank lines / paragraphs ---------------------------------------------
        if not stripped:
            flush_paragraph(paragraph)
            i += 1
            continue

        paragraph.append(stripped)
        i += 1

    flush_paragraph(paragraph)
    return meta, "\n".join(out)


# --------------------------------------------------------------------------
# Templates
# --------------------------------------------------------------------------

def nav_pages(config: Dict[str, Any]) -> Iterator[Tuple[str, str]]:
    """Yield ``(title, md_filename)`` for every entry in the nav."""
    for _section, entries in config["nav"].items():
        if isinstance(entries, str):
            entries = [entries]
        for entry in entries or []:
            if isinstance(entry, dict):
                title = entry.get("name") or Path(str(entry.get("path", ""))).stem
                source = str(entry.get("path", ""))
            else:
                title = Path(str(entry)).stem.replace("-", " ").title()
                source = str(entry)
            if source:
                yield title, source


def page_href(md_name: str) -> str:
    """Markdown filename -> output HTML filename."""
    return md_name[:-3] + ".html" if md_name.lower().endswith(".md") else md_name + ".html"


def render_nav(config: Dict[str, Any], active: Optional[str] = None) -> str:
    """Sidebar navigation; ``active`` is the source .md filename to mark."""
    parts: List[str] = ['<nav class="nsdocs-nav">']
    for section, entries in config["nav"].items():
        parts.append(f'<div class="nav-section">{html.escape(str(section))}</div>')
        if isinstance(entries, str):
            entries = [entries]
        for entry in entries or []:
            if isinstance(entry, dict):
                source = str(entry.get("path", ""))
                title = str(entry.get("name") or Path(source).stem)
            else:
                source = str(entry)
                title = Path(source).stem.replace("-", " ").title()
            if not source:
                continue
            href = html.escape(page_href(source), quote=True)
            cls = ' class="active"' if active is not None and Path(source).name == Path(active).name else ""
            parts.append(f'<a href="{href}"{cls}>{html.escape(title)}</a>')
    parts.append("</nav>")
    return "\n".join(parts)


def favicon_links(config: Dict[str, Any]) -> str:
    assets = config["assets"]
    links: List[str] = []
    for key, media in (("favicon_light", "(prefers-color-scheme: light)"), ("favicon_dark", "(prefers-color-scheme: dark)")):
        path = assets.get(key)
        if path:
            links.append(f'<link rel="icon" href="{html.escape(str(path), quote=True)}" media="{media}">')
    return "\n    ".join(links)


def theme_toggle_script() -> str:
    return """(function () {
  var root = document.documentElement;
  var stored = localStorage.getItem('nsdocs-theme');
  var theme = stored || (window.matchMedia('(prefers-color-scheme: dark)').matches ? 'dark' : 'light');
  root.setAttribute('data-theme', theme);
  window.nsdocsToggleTheme = function () {
    var next = root.getAttribute('data-theme') === 'dark' ? 'light' : 'dark';
    root.setAttribute('data-theme', next);
    localStorage.setItem('nsdocs-theme', next);
  };
})();"""


def copy_button_script() -> str:
    return """(function () {
  document.querySelectorAll('.copy-btn').forEach(function (btn) {
    btn.addEventListener('click', function () {
      var code = btn.parentElement.querySelector('code');
      if (!code) return;
      navigator.clipboard.writeText(code.textContent).then(function () {
        var old = btn.textContent;
        btn.textContent = 'Copied!';
        setTimeout(function () { btn.textContent = old; }, 1500);
      });
    });
  });
})();"""


def render_page(
    config: Dict[str, Any],
    title: str,
    description: str,
    body_html: str,
    active_source: str,
    source_path: str,
) -> str:
    """Full HTML page chrome around a rendered Markdown body."""
    features = config["features"]
    repo_url = str(config["repo"].get("url", "")).rstrip("/")
    branch = config["repo"].get("branch", "main")

    edit_link = ""
    if features.get("edit_button") and repo_url and source_path:
        edit_url = f"{repo_url}/edit/{branch}/{source_path.lstrip('/')}"
        edit_link = (
            f'<a class="edit-link" href="{html.escape(edit_url, quote=True)}" '
            f'target="_blank" rel="noopener">Edit on GitHub</a>'
        )

    scripts = [theme_toggle_script()]
    if features.get("copy_button"):
        scripts.append(copy_button_script())
    script_html = "\n  ".join(f"<script>{s}</script>" for s in scripts)

    brand = config.get("brand_name") or config["site_name"]
    css_link = ""
    css_path = config["assets"].get("css")
    if css_path:
        css_link = f'<link rel="stylesheet" href="{html.escape(str(css_path), quote=True)}">'

    description_attr = f'\n  <meta name="description" content="{html.escape(description, quote=True)}">' if description else ""

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">{description_attr}
  <title>{html.escape(title)} - {html.escape(str(config["site_name"]))}</title>
  {favicon_links(config)}
  <style>:root {{ --brand-accent: {config["accent_color"]}; }}</style>
  {css_link}
  <style>
    :root {{ color-scheme: light dark; }}
    * {{ box-sizing: border-box; }}
    body {{ margin: 0; font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Helvetica, Arial, sans-serif; line-height: 1.6; }}
    html[data-theme="dark"] {{ background: #12151a; color: #d6dae0; }}
    html[data-theme="light"] {{ background: #ffffff; color: #1c2127; }}
    html[data-theme="dark"] a {{ color: var(--brand-accent); }}
    html[data-theme="light"] a {{ color: var(--brand-accent); }}
    .topbar {{ display: flex; align-items: center; justify-content: space-between; padding: 0.6rem 1.2rem; border-bottom: 1px solid rgba(128,128,128,.25); position: sticky; top: 0; backdrop-filter: blur(6px); }}
    .brand {{ font-weight: 700; text-decoration: none; color: inherit; }}
    .brand span {{ color: var(--brand-accent); }}
    .theme-btn {{ border: 1px solid rgba(128,128,128,.35); background: transparent; color: inherit; border-radius: 6px; padding: 0.25rem 0.6rem; cursor: pointer; }}
    .layout {{ display: flex; gap: 2rem; max-width: 75rem; margin: 0 auto; padding: 1.5rem 1.2rem; }}
    .nsdocs-nav {{ min-width: 13rem; }}
    .nsdocs-nav .nav-section {{ font-size: .78rem; text-transform: uppercase; letter-spacing: .06em; opacity: .6; margin: 1rem 0 .3rem; }}
    .nsdocs-nav a {{ display: block; padding: .25rem .5rem; border-radius: 6px; text-decoration: none; }}
    .nsdocs-nav a.active {{ background: var(--brand-accent); color: #fff; }}
    html[data-theme="dark"] .nsdocs-nav a {{ color: #d6dae0; }}
    html[data-theme="light"] .nsdocs-nav a {{ color: #1c2127; }}
    html[data-theme="dark"] .nsdocs-nav a.active, html[data-theme="light"] .nsdocs-nav a.active {{ color: #fff; }}
    main {{ flex: 1; min-width: 0; }}
    pre {{ position: relative; background: rgba(128,128,128,.12); border-radius: 8px; padding: 1rem; overflow-x: auto; }}
    code {{ font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace; font-size: .9em; }}
    p code, li code {{ background: rgba(128,128,128,.15); border-radius: 4px; padding: .1rem .3rem; }}
    .copy-btn {{ position: absolute; top: .4rem; right: .4rem; font-size: .72rem; border: 1px solid rgba(128,128,128,.4); background: rgba(128,128,128,.08); color: inherit; border-radius: 5px; padding: .15rem .5rem; cursor: pointer; opacity: .75; }}
    .copy-btn:hover {{ opacity: 1; }}
    .admonition {{ border-left: 4px solid var(--brand-accent); background: rgba(128,128,128,.08); border-radius: 0 8px 8px 0; padding: .6rem 1rem; margin: 1rem 0; }}
    .admonition-warning {{ border-left-color: #d29922; }}
    .admonition-title {{ font-weight: 700; margin: 0 0 .25rem; text-transform: capitalize; }}
    table {{ border-collapse: collapse; width: 100%; margin: 1rem 0; }}
    th, td {{ border: 1px solid rgba(128,128,128,.3); padding: .45rem .7rem; text-align: left; }}
    th {{ background: rgba(128,128,128,.1); }}
    blockquote {{ border-left: 4px solid rgba(128,128,128,.4); margin: 1rem 0; padding: .2rem 1rem; }}
    footer {{ max-width: 75rem; margin: 0 auto; padding: 1rem 1.2rem 2rem; border-top: 1px solid rgba(128,128,128,.25); display: flex; justify-content: space-between; gap: 1rem; font-size: .85rem; opacity: .85; flex-wrap: wrap; }}
    @media (max-width: 48rem) {{ .layout {{ flex-direction: column; }} .nsdocs-nav {{ min-width: 0; }} }}
  </style>
</head>
<body>
  <header class="topbar">
    <a class="brand" href="index.html">{html.escape(str(brand))}<span> docs</span></a>
    {"" if not features.get("theme_toggle") else '<button class="theme-btn" onclick="nsdocsToggleTheme()" title="Toggle theme">&#9789;</button>'}
  </header>
  <div class="layout">
    {render_nav(config, active_source)}
    <main>
{body_html}
    </main>
  </div>
  <footer>
    <span>{html.escape(str(config.get("license_text", "")))}</span>
    {edit_link}
  </footer>
  {script_html}
</body>
</html>
"""


def render_index(config: Dict[str, Any]) -> str:
    """Landing page linking every configured navigation page."""
    items: List[str] = []
    for section, entries in config["nav"].items():
        if isinstance(entries, str):
            entries = [entries]
        links = []
        for entry in entries or []:
            if isinstance(entry, dict):
                source = str(entry.get("path", ""))
                title = str(entry.get("name") or Path(source).stem)
            else:
                source = str(entry)
                title = Path(source).stem.replace("-", " ").title()
            if not source:
                continue
            links.append(f'<li><a href="{html.escape(page_href(source), quote=True)}">{html.escape(title)}</a></li>')
        if links:
            items.append(f'<h2>{html.escape(str(section))}</h2><ul>{"".join(links)}</ul>')
    body = f"<h1>{html.escape(str(config['site_name']))} Wiki</h1>" + "".join(items)
    return render_page(config, config["site_name"], "", body, "", "")


# --------------------------------------------------------------------------
# Build pipeline
# --------------------------------------------------------------------------

def build_pages(config: Dict[str, Any]) -> Dict[str, str]:
    """Render every nav page (plus index) in memory: relative out path -> HTML."""
    pages: Dict[str, str] = {}
    for _title, source in nav_pages(config):
        source_norm = str(source).replace("\\", "/").lstrip("/")
        md_path = SRC_DIR / source_norm
        if not md_path.is_file():
            sys.stderr.write(f"nsdocs: warning: missing source file: {md_path}\n")
            continue
        raw = md_path.read_text(encoding="utf-8")
        meta, body_html = md_to_html(raw, copy_button=bool(config["features"].get("copy_button")))
        title = str(meta.get("name") or Path(source_norm).stem.replace("-", " ").title())
        description = str(meta.get("description") or "")
        href = page_href(source_norm)
        pages[href] = render_page(
            config,
            title,
            description,
            body_html,
            source_norm,
            f"docs/wiki-src/{source_norm}",
        )
    pages["index.html"] = render_index(config)
    return pages


def write_pages(pages: Dict[str, str], config: Dict[str, Any], force_clean: bool = False) -> int:
    """Write rendered pages; drop orphaned .html files when cleanup is enabled."""
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    count = 0
    for rel_path, content in sorted(pages.items()):
        target = OUT_DIR / rel_path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
        count += 1
    if force_clean or config["features"].get("clean_stale_files", True):
        expected = {str(p) for p in pages}
        for existing in sorted(OUT_DIR.rglob("*.html")):
            rel = existing.relative_to(OUT_DIR).as_posix()
            if rel not in expected:
                existing.unlink()
                print(f"nsdocs: removed stale file: {existing}")
    return count


def check_pages(pages: Dict[str, str]) -> int:
    """Compare rendered pages against disk; return number of problems."""
    problems = 0
    for rel_path, content in sorted(pages.items()):
        target = OUT_DIR / rel_path
        if not target.is_file():
            problems += 1
            print(f"nsdocs: MISSING  {target}")
            continue
        if target.read_text(encoding="utf-8") != content:
            problems += 1
            print(f"nsdocs: STALE    {target}")
            disk_lines = target.read_text(encoding="utf-8").splitlines()
            new_lines = content.splitlines()
            diff = difflib.unified_diff(
                disk_lines, new_lines, fromfile=str(target), tofile=f"{target} (expected)", lineterm=""
            )
            sys.stdout.writelines(line + "\n" for line in list(diff)[:40])
    return problems


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------

def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        prog="nsdocs",
        description="Build a lightweight HTML wiki from Markdown.",
    )
    parser.add_argument("--config", default="nsdocs.yml", help="Path to the config file (default: nsdocs.yml)")
    parser.add_argument("--check", action="store_true", help="Verify pages are up to date without writing (CI mode)")
    parser.add_argument("--clean", action="store_true", help="Remove orphaned .html files in docs/wiki/")
    parser.add_argument("--version", action="version", version=f"nsdocs {__version__}")
    args = parser.parse_args(argv)

    config = load_config(Path(args.config))
    pages = build_pages(config)

    if args.check:
        problems = check_pages(pages)
        if problems:
            print(f"nsdocs: {problems} page(s) out of date. Run nsdocs to rebuild.")
            return 1
        print("nsdocs: all pages up to date.")
        return 0

    written = write_pages(pages, config=config, force_clean=args.clean)
    print(f"nsdocs: wrote {written} page(s) to {OUT_DIR}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
