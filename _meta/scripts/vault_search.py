#!/usr/bin/env python3
"""
Obsidian Vault FTS5 Search Engine

Usage:
  vault_search.py index [--incremental]
  vault_search.py search <query> [--tag TAG] [--dir DIR] [--limit N] [--json]
  vault_search.py serve [--port PORT]
"""

import json
import os
import re
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

import click
import frontmatter
from rich.console import Console
from rich.table import Table

VAULT_ROOT = Path("/mnt/h/claude-memory-compiler")
DB_PATH = VAULT_ROOT / "_meta" / "vault-search.db"
SKIP_DIRS = {".obsidian", ".git", ".trash", "_meta", "node_modules", ".venv", "__pycache__"}

console = Console()


def get_connection():
    db = sqlite3.connect(str(DB_PATH))
    db.execute("PRAGMA journal_mode=WAL")
    return db


def init_db(db: sqlite3.Connection):
    db.executescript("""
    CREATE TABLE IF NOT EXISTS articles (
      id INTEGER PRIMARY KEY,
      path TEXT UNIQUE NOT NULL,
      title TEXT,
      tags TEXT,
      date TEXT,
      notion_id TEXT,
      content TEXT,
      mtime REAL,
      last_indexed TEXT
    );

    CREATE VIRTUAL TABLE IF NOT EXISTS articles_fts USING fts5(
      title,
      tags,
      content,
      content=articles,
      content_rowid=id,
      tokenize='porter unicode61'
    );

    CREATE TRIGGER IF NOT EXISTS articles_ai AFTER INSERT ON articles BEGIN
      INSERT INTO articles_fts(rowid, title, tags, content)
      VALUES (new.id, new.title, new.tags, new.content);
    END;

    CREATE TRIGGER IF NOT EXISTS articles_ad AFTER DELETE ON articles BEGIN
      INSERT INTO articles_fts(articles_fts, rowid, title, tags, content)
      VALUES ('delete', old.id, old.title, old.tags, old.content);
    END;

    CREATE TRIGGER IF NOT EXISTS articles_au AFTER UPDATE ON articles BEGIN
      INSERT INTO articles_fts(articles_fts, rowid, title, tags, content)
      VALUES ('delete', old.id, old.title, old.tags, old.content);
      INSERT INTO articles_fts(rowid, title, tags, content)
      VALUES (new.id, new.title, new.tags, new.content);
    END;
    """)


def should_skip(path: Path) -> bool:
    parts = path.relative_to(VAULT_ROOT).parts
    return any(p in SKIP_DIRS for p in parts)


def collect_md_files():
    for md_file in VAULT_ROOT.rglob("*.md"):
        if not should_skip(md_file):
            yield md_file


def parse_md(filepath: Path):
    try:
        post = frontmatter.load(str(filepath))
    except Exception:
        text = filepath.read_text(encoding="utf-8", errors="replace")
        return {"title": filepath.stem, "tags": "", "date": "", "notion_id": "", "content": text}

    tags = post.get("tags", [])
    if isinstance(tags, list):
        tags = ", ".join(str(t) for t in tags)
    elif tags is None:
        tags = ""

    return {
        "title": post.get("title", filepath.stem) or filepath.stem,
        "tags": str(tags),
        "date": str(post.get("date", "")),
        "notion_id": str(post.get("notion_id", "")),
        "content": post.content,
    }


@click.group()
def cli():
    """Obsidian Vault FTS5 Search Engine"""


@cli.command()
@click.option("--incremental", is_flag=True, help="Only index changed files")
def index(incremental):
    """Index all markdown files in the vault."""
    db = get_connection()
    init_db(db)

    now = datetime.now(timezone.utc).isoformat()
    indexed = 0
    skipped = 0
    deleted = 0

    current_files = set()

    for md_file in collect_md_files():
        rel_path = str(md_file.relative_to(VAULT_ROOT))
        current_files.add(rel_path)
        mtime = md_file.stat().st_mtime

        if incremental:
            row = db.execute(
                "SELECT mtime FROM articles WHERE path = ?", (rel_path,)
            ).fetchone()
            if row and row[0] >= mtime:
                skipped += 1
                continue

        parsed = parse_md(md_file)
        db.execute(
            """INSERT INTO articles (path, title, tags, date, notion_id, content, mtime, last_indexed)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(path) DO UPDATE SET
              title=excluded.title, tags=excluded.tags, date=excluded.date,
              notion_id=excluded.notion_id, content=excluded.content,
              mtime=excluded.mtime, last_indexed=excluded.last_indexed""",
            (rel_path, parsed["title"], parsed["tags"], parsed["date"],
             parsed["notion_id"], parsed["content"], mtime, now),
        )
        indexed += 1

    all_paths = {r[0] for r in db.execute("SELECT path FROM articles").fetchall()}
    stale = all_paths - current_files
    for path in stale:
        db.execute("DELETE FROM articles WHERE path = ?", (path,))
        deleted += 1

    db.commit()
    db.close()

    console.print(f"[green]Indexed:[/green] {indexed}  [dim]Skipped:[/dim] {skipped}  [red]Removed:[/red] {deleted}")
    console.print(f"[dim]Total articles: {len(current_files)}[/dim]")


@cli.command()
@click.argument("query")
@click.option("--tag", default=None, help="Filter by tag")
@click.option("--dir", "directory", default=None, help="Filter by directory prefix")
@click.option("--limit", default=20, help="Max results")
@click.option("--json-output", "--json", "json_out", is_flag=True, help="JSON output for LLM")
def search(query, tag, directory, limit, json_out):
    """Search the vault using FTS5."""
    if not DB_PATH.exists():
        console.print("[red]No index found. Run 'vault_search.py index' first.[/red]")
        sys.exit(1)

    db = get_connection()

    safe_query = re.sub(r'[^\w\s]', ' ', query).strip()
    if not safe_query:
        console.print("[red]Empty query[/red]")
        sys.exit(1)

    sql = """
    SELECT
      a.path,
      a.title,
      a.tags,
      a.date,
      snippet(articles_fts, 2, '<b>', '</b>', '...', 32) as snippet,
      bm25(articles_fts, 5.0, 2.0, 1.0) as rank
    FROM articles_fts
    JOIN articles a ON a.id = articles_fts.rowid
    WHERE articles_fts MATCH ?
    """
    params = [safe_query]

    if tag:
        sql += " AND a.tags LIKE ?"
        params.append(f"%{tag}%")
    if directory:
        sql += " AND a.path LIKE ?"
        params.append(f"{directory}%")

    sql += " ORDER BY rank LIMIT ?"
    params.append(limit)

    rows = db.execute(sql, params).fetchall()
    db.close()

    if json_out:
        results = [
            {
                "rank": round(row[5], 3),
                "path": row[0],
                "title": row[1],
                "tags": row[2],
                "date": row[3],
                "snippet": row[4],
            }
            for row in rows
        ]
        print(json.dumps(results, indent=2, ensure_ascii=False))
        return

    if not rows:
        console.print(f"[dim]No results for '{query}'[/dim]")
        return

    table = Table(title=f"Search: {query}", show_lines=True)
    table.add_column("#", style="dim", width=3)
    table.add_column("Title", style="bold")
    table.add_column("Path", style="cyan")
    table.add_column("Tags", style="green")
    table.add_column("Snippet", max_width=60)

    for i, row in enumerate(rows, 1):
        snippet = re.sub(r'</?b>', '', row[4] or "")
        table.add_row(str(i), row[1] or "", row[0], row[2] or "", snippet)

    console.print(table)
    console.print(f"[dim]{len(rows)} results[/dim]")


@cli.command()
@click.option("--port", default=8787, help="Port for web UI")
def serve(port):
    """Start a minimal web UI for vault search."""
    try:
        import uvicorn
        from fastapi import FastAPI
        from fastapi.responses import HTMLResponse
    except ImportError:
        console.print("[red]Install fastapi and uvicorn: pip install fastapi uvicorn[/red]")
        sys.exit(1)

    app = FastAPI(title="Vault Search")

    @app.get("/", response_class=HTMLResponse)
    async def home():
        return """
        <!DOCTYPE html>
        <html>
        <head><title>Vault Search</title><meta charset="utf-8">
        <style>
          * { box-sizing: border-box; margin: 0; padding: 0; }
          body { font-family: system-ui, sans-serif; max-width: 800px; margin: 2rem auto; padding: 0 1rem; background: #1a1a2e; color: #eee; }
          h1 { margin-bottom: 1rem; color: #e94560; }
          input { width: 100%; padding: 0.75rem; font-size: 1.1rem; border: 1px solid #333; border-radius: 6px; background: #16213e; color: #eee; }
          input:focus { outline: none; border-color: #e94560; }
          #results { margin-top: 1.5rem; }
          .result { background: #16213e; padding: 1rem; margin-bottom: 0.75rem; border-radius: 6px; }
          .result h3 { color: #e94560; margin-bottom: 0.25rem; }
          .result .path { color: #0f3460; font-size: 0.85rem; margin-bottom: 0.5rem; }
          .result .snippet { font-size: 0.9rem; line-height: 1.5; }
          .result .tags { color: #53a8b6; font-size: 0.8rem; margin-top: 0.25rem; }
        </style></head>
        <body>
          <h1>Vault Search</h1>
          <input type="text" id="q" placeholder="Search the vault..." autofocus onkeyup="if(event.key==='Enter')search()">
          <div id="results"></div>
          <script>
          async function search() {
            const q = document.getElementById('q').value;
            if (!q) return;
            const res = await fetch('/api/search?q=' + encodeURIComponent(q) + '&limit=20');
            const data = await res.json();
            const out = document.getElementById('results');
            if (!data.length) { out.innerHTML = '<p style="color:#666">No results</p>'; return; }
            out.innerHTML = data.map(r =>
              '<div class="result"><h3>' + r.title + '</h3><div class="path">' + r.path + '</div><div class="snippet">' + (r.snippet||'') + '</div><div class="tags">' + (r.tags||'') + '</div></div>'
            ).join('');
          }
          </script>
        </body></html>"""

    @app.get("/api/search")
    async def api_search(q: str = "", limit: int = 20):
        if not DB_PATH.exists():
            return []
        db = get_connection()
        safe_query = re.sub(r'[^\w\s]', ' ', q).strip()
        if not safe_query:
            return []
        sql = """SELECT a.path, a.title, a.tags, a.date,
                 snippet(articles_fts, 2, '<b>', '</b>', '...', 32) as snippet
                 FROM articles_fts JOIN articles a ON a.id = articles_fts.rowid
                 WHERE articles_fts MATCH ? ORDER BY rank LIMIT ?"""
        rows = db.execute(sql, (safe_query, limit)).fetchall()
        db.close()
        return [{"path": r[0], "title": r[1], "tags": r[2], "date": r[3], "snippet": r[4]} for r in rows]

    console.print(f"[green]Starting vault search at http://localhost:{port}[/green]")
    uvicorn.run(app, host="0.0.0.0", port=port, log_level="warning")


if __name__ == "__main__":
    cli()
