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


# ----- Renderer: GitHub API + cooldown -----

GITHUB_API_URL = "https://api.github.com/markdown"
COOLDOWN_RATE_LIMIT_S = 600.0   # 10 min for 403/429
COOLDOWN_TRANSIENT_S = 30.0     # 30s for network/5xx


@dataclass
class RenderState:
    """Process-wide renderer state (single instance, mutated under the asyncio loop)."""
    cooldown_until: float = 0.0
    client: Optional[httpx.AsyncClient] = None  # set in lifespan startup
    cache: dict[tuple[Path, float], tuple[str, str]] = field(default_factory=dict)  # (abs_path, mtime) -> (html, mode)


STATE = RenderState()


async def _render_via_api(text: str) -> str:
    assert STATE.client is not None, "httpx client not initialized"
    headers: dict[str, str] = {}
    token = os.environ.get("GITHUB_TOKEN")
    if token:
        headers["Authorization"] = f"Bearer {token}"
    r = await STATE.client.post(GITHUB_API_URL, json={"text": text, "mode": "gfm"}, headers=headers)
    if r.status_code in (403, 429):
        STATE.cooldown_until = time.monotonic() + COOLDOWN_RATE_LIMIT_S
        raise httpx.HTTPStatusError("rate limited", request=r.request, response=r)
    if r.status_code >= 500:
        STATE.cooldown_until = time.monotonic() + COOLDOWN_TRANSIENT_S
        raise httpx.HTTPStatusError("server error", request=r.request, response=r)
    r.raise_for_status()
    return r.text


async def render(text: str) -> tuple[str, str]:
    """Render markdown. Returns (html, mode) where mode is "api" or "local"."""
    now = time.monotonic()
    if now >= STATE.cooldown_until:
        try:
            return await _render_via_api(text), "api"
        except (httpx.HTTPError, httpx.TimeoutException):
            # Network / timeout / 5xx — short cooldown if not already rate-limited
            if STATE.cooldown_until <= now:
                STATE.cooldown_until = now + COOLDOWN_TRANSIENT_S
    # Cooldown active or API just failed → local
    return render_local(text), "local"


async def render_path(abs_path: Path) -> tuple[str, str]:
    """Render the markdown file at abs_path. Returns (html, mode).

    Uses the (abs_path, mtime) cache. The FileWatcher invalidates by path
    explicitly because some filesystems / atomic-write strategies leave mtime
    unchanged across content changes — DO NOT remove that invalidation.
    """
    mtime = abs_path.stat().st_mtime
    key = (abs_path, mtime)
    if key in STATE.cache:
        html, mode = STATE.cache[key]
        return html, mode
    text = abs_path.read_text(encoding="utf-8")
    html, mode = await render(text)
    STATE.cache[key] = (html, mode)
    return html, mode


def invalidate_cache_for(abs_path: Path) -> None:
    """Remove ALL cache entries for a path, regardless of mtime.

    Removing only the (path, current_mtime) entry is insufficient: if a
    file was reverted to an older mtime that's still cached (e.g. via
    `git checkout`), the stale entry would survive.
    """
    to_drop = [k for k in STATE.cache if k[0] == abs_path]
    for k in to_drop:
        STATE.cache.pop(k, None)


# ----- FastAPI app -----

@asynccontextmanager
async def lifespan(app: FastAPI):
    # Startup: client first (used by render()); observer comes in Task 11
    STATE.client = httpx.AsyncClient(timeout=httpx.Timeout(10.0, connect=5.0))
    try:
        yield
    finally:
        # Shutdown
        if STATE.client is not None:
            await STATE.client.aclose()
            STATE.client = None


app = FastAPI(lifespan=lifespan)


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
