#!/usr/bin/env python3
"""
Obsidian Vault Linter / Health Check

Checks:
  1. Broken wikilinks
  2. Frontmatter validation
  3. Orphan detection (no incoming links)
  4. Tag consistency (typos, near-duplicates)
  5. Stale raw sources
  6. Naming convention violations
  7. Empty files
  8. Missing cross-references suggestions

Usage:
  vault_lint.py [--check CHECK] [--fix] [--json]
"""

import json
import os
import re
from collections import Counter, defaultdict
from datetime import datetime, timezone, timedelta
from pathlib import Path

import click
import frontmatter
from rich.console import Console
from rich.table import Table

VAULT_ROOT = Path("/mnt/h/claude-memory-compiler")
SKIP_DIRS = {".obsidian", ".git", ".trash", "_meta", "node_modules", ".venv", "__pycache__"}

console = Console()


def collect_files(vault_root: Path):
    """Collect all markdown files with metadata."""
    files = {}
    for md_file in vault_root.rglob("*.md"):
        rel = md_file.relative_to(vault_root)
        if any(p in SKIP_DIRS for p in rel.parts):
            continue

        try:
            post = frontmatter.load(str(md_file))
            meta = dict(post.metadata)
            content = post.content
        except Exception:
            try:
                content = md_file.read_text(encoding="utf-8", errors="replace")
            except Exception:
                content = ""
            meta = {}

        files[str(rel)] = {
            "path": str(rel),
            "abs_path": str(md_file),
            "stem": md_file.stem,
            "meta": meta,
            "content": content,
            "size": md_file.stat().st_size,
            "mtime": md_file.stat().st_mtime,
        }

    return files


def extract_wikilinks(content: str):
    """Extract [[wikilink]] targets from content."""
    return re.findall(r'\[\[([^\]|]+)(?:\|[^\]]+)?\]\]', content)


def build_stem_map(files: dict):
    """Map lowercase stems to file paths for wikilink resolution."""
    stem_map = defaultdict(list)
    for path, info in files.items():
        stem_map[info["stem"].lower()].append(path)
    return stem_map


# ─── Check Functions ───────────────────────────────────────────────

def check_broken_links(files: dict):
    """Find wikilinks that don't resolve to any file."""
    stem_map = build_stem_map(files)
    issues = []

    for path, info in files.items():
        links = extract_wikilinks(info["content"])
        for link in links:
            target = link.strip()
            target_lower = target.lower()

            if target_lower in stem_map:
                continue

            target_path = target.replace(".md", "")
            if any(target_path.lower() in p.lower() for p in files):
                continue

            issues.append({
                "type": "error",
                "check": "broken_link",
                "file": path,
                "message": f"Broken wikilink [[{target}]] — no matching file found",
            })

    return issues


def check_frontmatter(files: dict):
    """Validate frontmatter fields."""
    issues = []
    required_base = {"title", "date", "tags"}

    for path, info in files.items():
        if "templates/" in path or info["meta"].get("auto_generated"):
            continue

        meta = info["meta"]
        if not meta:
            issues.append({
                "type": "error",
                "check": "frontmatter",
                "file": path,
                "message": "No YAML frontmatter found",
            })
            continue

        for field in required_base:
            if field not in meta:
                issues.append({
                    "type": "warning",
                    "check": "frontmatter",
                    "file": path,
                    "message": f"Missing required field: {field}",
                })

        tags = meta.get("tags")
        if tags is not None and not isinstance(tags, list):
            issues.append({
                "type": "warning",
                "check": "frontmatter",
                "file": path,
                "message": f"'tags' should be a list, got {type(tags).__name__}",
            })

    return issues


def check_orphans(files: dict):
    """Find files with no incoming wikilinks."""
    stem_map = build_stem_map(files)
    incoming = defaultdict(int)

    for path, info in files.items():
        links = extract_wikilinks(info["content"])
        for link in links:
            target_lower = link.strip().lower()
            if target_lower in stem_map:
                for target_path in stem_map[target_lower]:
                    incoming[target_path] += 1

    issues = []
    for path, info in files.items():
        if "_indexes/" in path or "_index.md" in path or "templates/" in path:
            continue
        if info["meta"].get("auto_generated"):
            continue
        if "dashboard.md" in path or "CLAUDE.md" in path or "AGENTS.md" in path:
            continue
        if incoming[path] == 0:
            issues.append({
                "type": "info",
                "check": "orphan",
                "file": path,
                "message": "No incoming wikilinks from any other file",
            })

    return issues


def check_tag_consistency(files: dict):
    """Find tags used only once or near-duplicate tags."""
    tag_counts = Counter()

    for info in files.values():
        tags = info["meta"].get("tags", [])
        if isinstance(tags, list):
            for t in tags:
                tag_counts[str(t).lower()] += 1

    issues = []
    for tag, count in tag_counts.items():
        if count == 1:
            issues.append({
                "type": "info",
                "check": "tag_consistency",
                "file": "",
                "message": f"Tag '{tag}' used only once — possible typo?",
            })

    tags_list = sorted(tag_counts.keys())
    seen = {}
    for tag in tags_list:
        normalized = tag.replace("-", "").replace("_", "").replace(" ", "")
        if normalized in seen and seen[normalized] != tag:
            issues.append({
                "type": "info",
                "check": "tag_consistency",
                "file": "",
                "message": f"Similar tags: '{seen[normalized]}' and '{tag}' — consider merging",
            })
        seen[normalized] = tag

    return issues


def check_stale_raw(files: dict):
    """Find raw sources not yet compiled."""
    cutoff = datetime.now(timezone.utc).timestamp() - 7 * 86400
    issues = []

    for path, info in files.items():
        if not path.startswith("raw/"):
            continue
        if path == "raw/_inbox.md":
            continue

        status = info["meta"].get("status", "")
        if status == "raw" and info["mtime"] < cutoff:
            issues.append({
                "type": "info",
                "check": "stale_raw",
                "file": path,
                "message": "Raw source older than 7 days, not yet compiled",
            })

    return issues


def check_naming(files: dict):
    """Check kebab-case naming convention."""
    issues = []
    kebab_re = re.compile(r'^[a-z0-9]+(-[a-z0-9]+)*$')

    for path, info in files.items():
        stem = info["stem"]
        if stem.startswith("_"):
            continue
        if "templates/" in path:
            continue
        if not kebab_re.match(stem):
            issues.append({
                "type": "warning",
                "check": "naming",
                "file": path,
                "message": f"Filename '{stem}' violates kebab-case convention",
            })

    return issues


def check_empty(files: dict):
    """Find empty or nearly empty files."""
    issues = []
    for path, info in files.items():
        if "templates/" in path:
            continue
        if info["size"] == 0:
            issues.append({
                "type": "warning",
                "check": "empty",
                "file": path,
                "message": "File is empty (0 bytes)",
            })
        elif not info["content"].strip() and info["meta"]:
            issues.append({
                "type": "info",
                "check": "empty",
                "file": path,
                "message": "File has frontmatter but no body content",
            })

    return issues


def check_cross_refs(files: dict):
    """Suggest missing cross-references based on shared tags."""
    issues = []

    tag_files = defaultdict(set)
    for path, info in files.items():
        tags = info["meta"].get("tags", [])
        if isinstance(tags, list):
            for t in tags:
                tag_files[str(t).lower()].add(path)

    stem_map = build_stem_map(files)

    for path, info in files.items():
        links = set()
        for link in extract_wikilinks(info["content"]):
            target_lower = link.strip().lower()
            if target_lower in stem_map:
                links.update(stem_map[target_lower])

        tags = info["meta"].get("tags", [])
        if not isinstance(tags, list):
            continue

        related = set()
        for t in tags:
            related.update(tag_files.get(str(t).lower(), set()))
        related.discard(path)
        unlinked = related - links

        if 0 < len(unlinked) <= 5:
            for target in sorted(unlinked):
                target_stem = files[target]["stem"]
                issues.append({
                    "type": "info",
                    "check": "cross_ref",
                    "file": path,
                    "message": f"Consider linking to [[{target_stem}]] (shared tags)",
                })

    return issues


ALL_CHECKS = {
    "links": check_broken_links,
    "frontmatter": check_frontmatter,
    "orphans": check_orphans,
    "tags": check_tag_consistency,
    "stale_raw": check_stale_raw,
    "naming": check_naming,
    "empty": check_empty,
    "cross_refs": check_cross_refs,
}


@click.command()
@click.option("--check", "check_name", default=None, help="Run only this check")
@click.option("--fix", is_flag=True, help="Auto-fix safe issues (naming)")
@click.option("--json-output", "--json", "json_out", is_flag=True, help="JSON output")
def main(check_name, fix, json_out):
    """Run health checks on the Obsidian vault."""
    console.print("[bold]Scanning vault...[/bold]")
    files = collect_files(VAULT_ROOT)
    console.print(f"[dim]{len(files)} files found[/dim]")

    all_issues = []
    checks_to_run = {check_name: ALL_CHECKS[check_name]} if check_name else ALL_CHECKS

    for name, check_fn in checks_to_run.items():
        issues = check_fn(files)
        all_issues.extend(issues)

    if fix:
        for issue in all_issues:
            if issue["check"] == "naming" and issue["file"]:
                old_path = VAULT_ROOT / issue["file"]
                new_stem = re.sub(r'[^a-z0-9]+', '-', old_path.stem.lower()).strip('-')
                new_path = old_path.parent / f"{new_stem}.md"
                if not new_path.exists() and old_path.exists():
                    old_path.rename(new_path)
                    issue["message"] += f" → renamed to {new_stem}.md"

    if json_out:
        print(json.dumps(all_issues, indent=2, ensure_ascii=False))
        return

    errors = [i for i in all_issues if i["type"] == "error"]
    warnings = [i for i in all_issues if i["type"] == "warning"]
    infos = [i for i in all_issues if i["type"] == "info"]

    console.print()
    console.print(f"[bold]Vault Health Report — {datetime.now().strftime('%Y-%m-%d')}[/bold]")
    console.print(f"  Files scanned: {len(files)}")
    console.print(f"  [red]Errors: {len(errors)}[/red]")
    console.print(f"  [yellow]Warnings: {len(warnings)}[/yellow]")
    console.print(f"  [blue]Suggestions: {len(infos)}[/blue]")
    console.print()

    if errors:
        console.print("[bold red]── Errors ──[/bold red]")
        for i in errors:
            console.print(f"  [red]✗[/red] [{i['check']}] {i['file']}: {i['message']}")
        console.print()

    if warnings:
        console.print("[bold yellow]── Warnings ──[/bold yellow]")
        for i in warnings:
            console.print(f"  [yellow]![/yellow] [{i['check']}] {i['file']}: {i['message']}")
        console.print()

    if infos:
        console.print("[bold blue]── Suggestions ──[/bold blue]")
        for i in infos[:30]:
            file_str = f"{i['file']}: " if i['file'] else ""
            console.print(f"  [blue]·[/blue] [{i['check']}] {file_str}{i['message']}")
        if len(infos) > 30:
            console.print(f"  [dim]... and {len(infos) - 30} more suggestions[/dim]")
        console.print()

    if not errors and len(warnings) <= 5:
        console.print("[bold green]Overall health: GOOD[/bold green]")
    elif not errors:
        console.print("[bold yellow]Overall health: FAIR — some warnings to address[/bold yellow]")
    else:
        console.print("[bold red]Overall health: NEEDS ATTENTION — fix errors first[/bold red]")


if __name__ == "__main__":
    main()
