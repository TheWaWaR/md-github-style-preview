"""Markdown GitHub-style live preview server."""
from __future__ import annotations

import argparse
import asyncio
import json
import mimetypes
import os
import sys
import threading
import time
import webbrowser
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Optional

import httpx
import markdown as md_lib
import uvicorn
from fastapi import FastAPI, HTTPException, Request, Response
from fastapi.responses import HTMLResponse, JSONResponse, StreamingResponse
from watchdog.events import FileSystemEventHandler
from watchdog.observers import Observer

# ----- CLI -----

def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Markdown GitHub-style live preview")
    p.add_argument("dir", nargs="?", default=".", help="Directory to watch (default: cwd)")
    p.add_argument("--port", type=int, default=8765, help="HTTP port (default: 8765)")
    p.add_argument("--host", default="127.0.0.1", help="Bind address (default: 127.0.0.1)")
    p.add_argument("--no-browser", action="store_true", help="Do not auto-open the browser")
    return p.parse_args()


# ----- Globals (populated in main()) -----

ROOT: Path = Path(".").resolve()


# ----- Path utilities -----
# All paths exposed in URLs/JSON/SSE are relative to ROOT, with POSIX `/` separators,
# regardless of host OS. These helpers normalize at every boundary.

MARKDOWN_SUFFIXES = {".md", ".markdown"}


def is_markdown(path: Path) -> bool:
    return path.suffix.lower() in MARKDOWN_SUFFIXES


def to_rel_posix(abs_path: Path) -> str:
    """Convert an absolute path under ROOT to a POSIX-style relative string."""
    return PurePosixPath(abs_path.resolve().relative_to(ROOT)).as_posix()


def safe_resolve(rel_path: str) -> Path:
    """Resolve a user-supplied relative path under ROOT.

    Raises HTTPException(403) on traversal, HTTPException(404) on non-existence.
    """
    target = (ROOT / rel_path).resolve()
    try:
        target.relative_to(ROOT)
    except ValueError:
        raise HTTPException(status_code=403, detail="path traversal denied")
    if not target.exists():
        raise HTTPException(status_code=404, detail="not found")
    return target


def list_markdown_files() -> list[str]:
    """Return sorted list of POSIX-relative paths to all markdown files under ROOT."""
    out: list[str] = []
    for p in ROOT.rglob("*"):
        if p.is_file() and is_markdown(p):
            out.append(to_rel_posix(p))
    out.sort()
    return out


# ----- Renderer: local fallback -----

LOCAL_MD_EXTENSIONS = [
    "extra",            # also pulls in tables, fenced_code, etc.
    "codehilite",       # pygments-based code block highlighting
    "pymdownx.tilde",   # ~~strikethrough~~
    "pymdownx.tasklist",
]
LOCAL_MD_EXT_CONFIGS = {
    "codehilite": {"guess_lang": False, "css_class": "highlight"},
    "pymdownx.tasklist": {"custom_checkbox": True},
}


def render_local(text: str) -> str:
    """Render markdown to HTML using the local `markdown` library."""
    return md_lib.markdown(
        text,
        extensions=LOCAL_MD_EXTENSIONS,
        extension_configs=LOCAL_MD_EXT_CONFIGS,
        output_format="html5",
    )


# ----- FastAPI app -----

app = FastAPI()


# ----- Routes: index and file enumeration -----

INDEX_TEMPLATE = """<!doctype html>
<html lang="en"><head>
<meta charset="utf-8">
<title>md-preview</title>
<link rel="stylesheet" href="/static/github-markdown.css">
<style>
  body {{ font-family: -apple-system, system-ui, sans-serif; max-width: 720px; margin: 2rem auto; padding: 0 1rem; }}
  h1 {{ font-size: 1.4rem; }}
  ul {{ list-style: none; padding: 0; }}
  li {{ padding: 0.3rem 0; }}
  a {{ text-decoration: none; color: #0366d6; }}
  a:hover {{ text-decoration: underline; }}
</style>
</head><body>
<h1>Markdown files in <code>{root}</code></h1>
<ul id="filelist">
{items}
</ul>
<script>
  const es = new EventSource('/events');
  es.addEventListener('message', (e) => {{
    const evt = JSON.parse(e.data);
    if (evt.event === 'created' || evt.event === 'deleted') {{
      fetch('/files').then(r => r.json()).then(({{files}}) => {{
        const ul = document.getElementById('filelist');
        ul.innerHTML = files.map(f =>
          `<li><a href="/view/${{f}}">${{f}}</a></li>`).join('');
      }});
    }}
  }});
</script>
</body></html>
"""


@app.get("/", response_class=HTMLResponse)
async def index() -> str:
    files = list_markdown_files()
    items = "\n".join(f'<li><a href="/view/{f}">{f}</a></li>' for f in files) or "<li><em>(no markdown files)</em></li>"
    return INDEX_TEMPLATE.format(root=str(ROOT), items=items)


@app.get("/files")
async def files_json() -> JSONResponse:
    return JSONResponse({"files": list_markdown_files()})


# ----- Entrypoint -----

def main() -> None:
    global ROOT
    args = parse_args()
    ROOT = Path(args.dir).resolve()
    if not ROOT.is_dir():
        print(f"error: {ROOT} is not a directory", file=sys.stderr)
        sys.exit(1)
    uvicorn.run(app, host=args.host, port=args.port, log_level="info")


if __name__ == "__main__":
    main()
