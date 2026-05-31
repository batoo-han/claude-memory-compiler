#!/usr/bin/env python3
"""
Obsidian Vault Index Generator

Scans all markdown files and generates:
  - _indexes/master-index.md (alphabetical list of all articles)
  - _indexes/tag-index.md (articles grouped by tag)
  - _indexes/topic-indexes/ (per-directory index files)

Usage:
  generate_indexes.py [--vault-root PATH]
"""

import os
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

import click
import frontmatter
from rich.console import Console

VAULT_ROOT = Path("/mnt/h/claude-memory-compiler")
SKIP_DIRS = {".obsidian", ".git", ".trash", "_meta", "_indexes", "_outputs", "node_modules", "templates", ".venv", "__pycache__"}
INDEXES_DIR = VAULT_ROOT / "_indexes"

console = Console()


def collect_articles(vault_root: Path):
    """Collect all markdown files with their frontmatter."""
    articles = []
    for md_file in vault_root.rglob("*.md"):
        rel = md_file.relative_to(vault_root)

        if any(p in SKIP_DIRS for p in rel.parts):
            continue

        if rel.name in ("dashboard.md", "CLAUDE.md"):
            continue

        try:
            post = frontmatter.load(str(md_file))
            title = post.get("title", md_file.stem) or md_file.stem
            tags = post.get("tags", [])
            if isinstance(tags, str):
                tags = [t.strip() for t in tags.split(",") if t.strip()]
            elif tags is None:
                tags = []
            date = str(post.get("date", ""))
            summary = post.get("summary", "")

            if not summary:
                for line in post.content.split("\n"):
                    line = line.strip()
                    if line and not line.startswith("#") and not line.startswith("---"):
                        summary = line[:120]
                        break
        except Exception:
            title = md_file.stem
            tags = []
            date = ""
            summary = ""

        articles.append({
            "path": str(rel),
            "stem": md_file.stem,
            "title": title,
            "tags": tags,
            "date": date,
            "summary": summary,
            "top_dir": rel.parts[0] if len(rel.parts) > 1 else "_root",
        })

    return articles


def write_master_index(articles: list):
    """Write alphabetical master index."""
    by_letter = defaultdict(list)
    for a in sorted(articles, key=lambda x: x["title"].lower()):
        letter = a["title"][0].upper()
        if not letter.isalpha():
            letter = "#"
        by_letter[letter].append(a)

    lines = [
        "---",
        'title: "Master Index"',
        f'date: "{datetime.now(timezone.utc).strftime("%Y-%m-%d")}"',
        "auto_generated: true",
        "---",
        "",
        "# Master Index",
        "",
        f"*{len(articles)} articles indexed*",
        "",
    ]

    for letter in sorted(by_letter.keys()):
        lines.append(f"## {letter}")
        for a in by_letter[letter]:
            desc = f" — {a['summary']}" if a["summary"] else ""
            lines.append(f"- [[{a['stem']}]]{desc}")
        lines.append("")

    out = INDEXES_DIR / "master-index.md"
    out.write_text("\n".join(lines), encoding="utf-8")
    return len(articles)


def write_tag_index(articles: list):
    """Write tag-based index."""
    by_tag = defaultdict(list)
    for a in articles:
        for tag in a["tags"]:
            by_tag[str(tag).lower()].append(a)

    lines = [
        "---",
        'title: "Tag Index"',
        f'date: "{datetime.now(timezone.utc).strftime("%Y-%m-%d")}"',
        "auto_generated: true",
        "---",
        "",
        "# Tag Index",
        "",
        f"*{len(by_tag)} tags across {len(articles)} articles*",
        "",
    ]

    for tag in sorted(by_tag.keys()):
        items = by_tag[tag]
        lines.append(f"## {tag} ({len(items)})")
        for a in sorted(items, key=lambda x: x["title"].lower()):
            lines.append(f"- [[{a['stem']}|{a['title']}]]")
        lines.append("")

    out = INDEXES_DIR / "tag-index.md"
    out.write_text("\n".join(lines), encoding="utf-8")
    return len(by_tag)


def write_topic_indexes(articles: list):
    """Write per-directory topic indexes."""
    by_dir = defaultdict(list)
    for a in articles:
        by_dir[a["top_dir"]].append(a)

    topic_dir = INDEXES_DIR / "topic-indexes"
    topic_dir.mkdir(parents=True, exist_ok=True)
    count = 0

    for dirname, items in sorted(by_dir.items()):
        if dirname == "_root":
            continue

        display_name = dirname.replace("-", " ").replace("_", " ").title()

        lines = [
            "---",
            f'title: "Topic Index: {display_name}"',
            f'date: "{datetime.now(timezone.utc).strftime("%Y-%m-%d")}"',
            "auto_generated: true",
            "---",
            "",
            f"# {display_name}",
            "",
            f"*{len(items)} articles*",
            "",
        ]

        by_subdir = defaultdict(list)
        for a in items:
            parts = Path(a["path"]).parts
            subdir = parts[1] if len(parts) > 2 else "_top"
            by_subdir[subdir].append(a)

        for subdir in sorted(by_subdir.keys()):
            sub_items = by_subdir[subdir]
            if subdir != "_top":
                sub_display = subdir.replace("-", " ").replace("_", " ").title()
                lines.append(f"### {sub_display}")
            for a in sorted(sub_items, key=lambda x: x["date"] or "", reverse=True):
                date_str = f" ({a['date']})" if a["date"] else ""
                lines.append(f"- [[{a['stem']}|{a['title']}]]{date_str}")
            lines.append("")

        # Related topics
        all_tags = set()
        for a in items:
            all_tags.update(a["tags"])
        related_dirs = set()
        for a in articles:
            if a["top_dir"] != dirname and any(t in all_tags for t in a["tags"]):
                related_dirs.add(a["top_dir"])

        if related_dirs:
            lines.append("### Related Topics")
            for rd in sorted(related_dirs):
                rd_display = rd.replace("-", " ").replace("_", " ").title()
                lines.append(f"- [[{rd}|{rd_display}]]")
            lines.append("")

        out = topic_dir / f"{dirname}.md"
        out.write_text("\n".join(lines), encoding="utf-8")
        count += 1

    return count


@click.command()
@click.option("--vault-root", default=str(VAULT_ROOT), help="Vault root path")
def main(vault_root):
    """Generate index files for the Obsidian vault."""
    root = Path(vault_root)
    INDEXES_DIR.mkdir(parents=True, exist_ok=True)

    console.print("[bold]Scanning vault...[/bold]")
    articles = collect_articles(root)

    n_articles = write_master_index(articles)
    n_tags = write_tag_index(articles)
    n_topics = write_topic_indexes(articles)

    console.print(f"[green]Done![/green]")
    console.print(f"  Articles: {n_articles}")
    console.print(f"  Tags: {n_tags}")
    console.print(f"  Topic indexes: {n_topics}")
    console.print(f"  Output: {INDEXES_DIR}/")


if __name__ == "__main__":
    main()
