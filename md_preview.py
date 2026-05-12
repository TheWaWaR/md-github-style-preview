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
    r = await STATE.client.post(GITHUB_API_URL, json={"text": text, "mode": "markdown"}, headers=headers)
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
    # Startup, in this order:
    # 1. Capture loop ref (must precede observer.start so events have a loop to dispatch to)
    loop = asyncio.get_running_loop()
    # 2. httpx client
    STATE.client = httpx.AsyncClient(timeout=httpx.Timeout(10.0, connect=5.0))
    # 3. Observer
    handler = MarkdownWatcher(loop)
    observer = Observer()
    observer.schedule(handler, str(ROOT), recursive=True)
    observer.start()

    try:
        yield
    finally:
        # Shutdown
        observer.stop()
        observer.join(timeout=5)
        if STATE.client is not None:
            await STATE.client.aclose()
            STATE.client = None


app = FastAPI(lifespan=lifespan)


# ----- SSE broadcast -----

# Set of asyncio.Queue instances, one per active /events connection.
SSE_CLIENTS: set[asyncio.Queue] = set()


def broadcast(event: dict) -> None:
    """Push an event to every active SSE client. Must be called from the loop thread."""
    dead = []
    for q in list(SSE_CLIENTS):
        try:
            q.put_nowait(event)
        except asyncio.QueueFull:
            dead.append(q)
    for q in dead:
        SSE_CLIENTS.discard(q)


# ----- File watcher -----

DEBOUNCE_S = 1.0


class MarkdownWatcher(FileSystemEventHandler):
    """Watches ROOT recursively. Coalesces events per path with a 1s debounce.

    Runs on watchdog's worker thread; bridges to the asyncio loop via
    loop.call_soon_threadsafe(_dispatch, ...).
    """

    def __init__(self, loop: asyncio.AbstractEventLoop):
        self._loop = loop
        self._timers: dict[str, threading.Timer] = {}  # rel_posix -> Timer
        self._latest: dict[str, str] = {}              # rel_posix -> latest event_type
        self._lock = threading.Lock()

    def on_modified(self, event):
        if event.is_directory: return
        self._record(event.src_path, "modified")

    def on_created(self, event):
        if event.is_directory: return
        self._record(event.src_path, "created")

    def on_deleted(self, event):
        if event.is_directory: return
        self._record(event.src_path, "deleted")

    def on_moved(self, event):
        if event.is_directory: return
        # Decompose into deleted(src) + created(dest); SSE schema has no "moved".
        self._record(event.src_path, "deleted")
        self._record(getattr(event, "dest_path", event.src_path), "created")

    def _record(self, raw_path: str, event_type: str) -> None:
        try:
            p = Path(raw_path).resolve()
            if not is_markdown(p):
                return
            try:
                rel = to_rel_posix(p)
            except ValueError:
                # File no longer under ROOT (e.g. moved out of tree)
                return
        except Exception:
            return

        with self._lock:
            self._latest[rel] = event_type
            existing = self._timers.pop(rel, None)
            if existing is not None:
                existing.cancel()
            t = threading.Timer(DEBOUNCE_S, self._fire, args=(rel, p))
            self._timers[rel] = t
            t.start()

    def _fire(self, rel: str, abs_path: Path) -> None:
        with self._lock:
            event_type = self._latest.pop(rel, "modified")
            self._timers.pop(rel, None)
        # Hand off to the loop thread.
        self._loop.call_soon_threadsafe(self._dispatch, rel, abs_path, event_type)

    def _dispatch(self, rel: str, abs_path: Path, event_type: str) -> None:
        # Runs on the asyncio loop thread.
        invalidate_cache_for(abs_path)
        broadcast({"path": rel, "event": event_type})


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


VIEW_TEMPLATE = """<!doctype html>
<html lang="en"><head>
<meta charset="utf-8">
<title>{title}</title>
<link rel="stylesheet" href="/static/github-markdown.css">
<style>
  body {{ box-sizing: border-box; margin: 0; padding: 2rem; }}
  .markdown-body {{ max-width: 980px; margin: 0 auto; }}
  #banner {{ position: fixed; top: 0; left: 0; right: 0; padding: 0.5rem 1rem;
             background: #fff8c5; border-bottom: 1px solid #d4a72c; display: none; }}
  #banner button {{ float: right; background: none; border: none; cursor: pointer; }}
  #banner #banner-refresh {{ float: none; padding: 0; margin-left: 0.5rem;
                             color: #0366d6; text-decoration: underline; }}
</style>
</head><body>
<div id="banner">
  <span id="banner-text"></span>
  <button id="banner-refresh" type="button" onclick="refresh()">refresh</button>
  <button type="button" onclick="document.getElementById('banner').style.display='none'">×</button>
</div>
<article id="content" class="markdown-body">{html}</article>
<script>
  const PATH = {path_json};
  const article = document.getElementById('content');
  const banner = document.getElementById('banner');
  const bannerText = document.getElementById('banner-text');
  const bannerRefresh = document.getElementById('banner-refresh');

  function showBanner(msg, withRefresh) {{
    bannerText.textContent = msg;
    bannerRefresh.style.display = withRefresh ? '' : 'none';
    banner.style.display = 'block';
  }}

  async function refresh() {{
    banner.style.display = 'none';
    const r = await fetch('/raw/' + PATH);
    if (!r.ok) {{ article.innerHTML = '<p>Render error: ' + r.status + '</p>'; return; }}
    const mode = r.headers.get('X-Render-Mode');
    if (mode === 'local') showBanner('API rate-limited, using local renderer', false);
    const scrollY = window.scrollY;
    article.innerHTML = await r.text();
    window.scrollTo(0, scrollY);
  }}

  const es = new EventSource('/events');
  es.addEventListener('message', (e) => {{
    const evt = JSON.parse(e.data);
    if (evt.path === PATH && evt.event === 'modified') {{
      showBanner('File changed — click to re-render', true);
    }}
  }});
</script>
</body></html>
"""


@app.get("/view/{path:path}", response_class=HTMLResponse)
async def view(path: str) -> str:
    abs_path = safe_resolve(path)
    if not is_markdown(abs_path):
        raise HTTPException(status_code=404, detail="not a markdown file")
    html, mode = await render_path(abs_path)
    return VIEW_TEMPLATE.format(
        title=path,
        html=html,
        path_json=json.dumps(path),
    )


@app.get("/raw/{path:path}")
async def raw(path: str) -> Response:
    abs_path = safe_resolve(path)
    if not is_markdown(abs_path):
        raise HTTPException(status_code=404, detail="not a markdown file")
    try:
        html, mode = await render_path(abs_path)
    except Exception as e:
        return Response(content=f"render error: {e}", status_code=500, media_type="text/plain")
    return Response(content=html, media_type="text/html", headers={"X-Render-Mode": mode})


@app.get("/events")
async def events(request: Request) -> StreamingResponse:
    queue: asyncio.Queue = asyncio.Queue(maxsize=256)
    SSE_CLIENTS.add(queue)

    async def gen():
        try:
            while True:
                try:
                    evt = await asyncio.wait_for(queue.get(), timeout=15.0)
                    yield f"data: {json.dumps(evt)}\n\n"
                except asyncio.TimeoutError:
                    # Keepalive: doubles as disconnect detector — yielding to a
                    # closed connection raises and unwinds into the finally block.
                    yield ": keepalive\n\n"
        finally:
            SSE_CLIENTS.discard(queue)

    return StreamingResponse(gen(), media_type="text/event-stream")


@app.get("/assets/{path:path}")
async def assets(path: str) -> Response:
    abs_path = safe_resolve(path)
    if not abs_path.is_file():
        raise HTTPException(status_code=404, detail="not a file")
    mime, _ = mimetypes.guess_type(str(abs_path))
    return Response(content=abs_path.read_bytes(), media_type=mime or "application/octet-stream")


@app.get("/static/github-markdown.css")
async def github_markdown_css() -> Response:
    return Response(content=GITHUB_MD_CSS, media_type="text/css")


# ----- Entrypoint -----

def _check_port_available(host: str, port: int) -> None:
    """Probe-bind so we can fail fast with a friendly message before uvicorn starts.

    Uvicorn catches the bind OSError internally and prints its own ERROR log,
    so we can't catch it from `uvicorn.run()`. There's a tiny TOCTOU window
    between this probe and uvicorn's real bind, acceptable for a local dev tool.
    """
    import socket
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        s.bind((host, port))
    except OSError as e:
        if getattr(e, "errno", None) in (48, 98) or "address already in use" in str(e).lower():
            print(
                f"error: port {port} is already in use; pass --port to choose another",
                file=sys.stderr,
            )
            sys.exit(1)
        raise
    finally:
        s.close()


def main() -> None:
    global ROOT
    args = parse_args()
    ROOT = Path(args.dir).resolve()
    if not ROOT.is_dir():
        print(f"error: {ROOT} is not a directory", file=sys.stderr)
        sys.exit(1)

    _check_port_available(args.host, args.port)

    # Browser auto-open: 0.5s delay so uvicorn has time to bind.
    if not args.no_browser:
        display_host = "127.0.0.1" if args.host == "0.0.0.0" else args.host
        url = f"http://{display_host}:{args.port}/"
        threading.Timer(0.5, lambda: webbrowser.open(url)).start()

    # timeout_graceful_shutdown=0: don't wait for long-lived SSE connections
    # to drain on Ctrl+C — release the port immediately.
    uvicorn.run(app, host=args.host, port=args.port, log_level="info", timeout_graceful_shutdown=0)


# ============================================================================
# Vendored github-markdown-css (https://github.com/sindresorhus/github-markdown-css)
# Updated by hand. To refresh:
#   curl -sSL https://raw.githubusercontent.com/sindresorhus/github-markdown-css/main/github-markdown.css
# ============================================================================
GITHUB_MD_CSS = r""".markdown-body {
  --base-size-16: 1rem;
  --base-size-24: 1.5rem;
  --base-size-4: 0.25rem;
  --base-size-40: 2.5rem;
  --base-size-8: 0.5rem;
  --base-text-weight-medium: 500;
  --base-text-weight-normal: 400;
  --base-text-weight-semibold: 600;
  --fontStack-monospace: ui-monospace, SFMono-Regular, SF Mono, Menlo, Consolas, Liberation Mono, monospace;
  --fontStack-sansSerif: -apple-system, BlinkMacSystemFont, "Segoe UI", "Noto Sans", Helvetica, Arial, sans-serif, "Apple Color Emoji", "Segoe UI Emoji";
  --fgColor-accent: Highlight;
}
@media (prefers-color-scheme: dark) {
  .markdown-body, [data-theme="dark"] {
    /*dark */
    color-scheme: dark;
    --fgColor-accent: #4493f8;
    --bgColor-attention-muted: #bb800926;
    --bgColor-default: #0d1117;
    --bgColor-muted: #151b23;
    --bgColor-neutral-muted: #656c7633;
    --borderColor-accent-emphasis: #1f6feb;
    --borderColor-attention-emphasis: #9e6a03;
    --borderColor-danger-emphasis: #da3633;
    --borderColor-default: #3d444d;
    --borderColor-done-emphasis: #8957e5;
    --borderColor-success-emphasis: #238636;
    --color-prettylights-syntax-brackethighlighter-angle: #9198a1;
    --color-prettylights-syntax-brackethighlighter-unmatched: #f85149;
    --color-prettylights-syntax-carriage-return-bg: #b62324;
    --color-prettylights-syntax-carriage-return-text: #f0f6fc;
    --color-prettylights-syntax-comment: #9198a1;
    --color-prettylights-syntax-constant: #79c0ff;
    --color-prettylights-syntax-constant-other-reference-link: #a5d6ff;
    --color-prettylights-syntax-entity: #d2a8ff;
    --color-prettylights-syntax-entity-tag: #7ee787;
    --color-prettylights-syntax-keyword: #ff7b72;
    --color-prettylights-syntax-markup-bold: #f0f6fc;
    --color-prettylights-syntax-markup-changed-bg: #5a1e02;
    --color-prettylights-syntax-markup-changed-text: #ffdfb6;
    --color-prettylights-syntax-markup-deleted-bg: #67060c;
    --color-prettylights-syntax-markup-deleted-text: #ffdcd7;
    --color-prettylights-syntax-markup-heading: #1f6feb;
    --color-prettylights-syntax-markup-ignored-bg: #1158c7;
    --color-prettylights-syntax-markup-ignored-text: #f0f6fc;
    --color-prettylights-syntax-markup-inserted-bg: #033a16;
    --color-prettylights-syntax-markup-inserted-text: #aff5b4;
    --color-prettylights-syntax-markup-italic: #f0f6fc;
    --color-prettylights-syntax-markup-list: #f2cc60;
    --color-prettylights-syntax-meta-diff-range: #d2a8ff;
    --color-prettylights-syntax-storage-modifier-import: #f0f6fc;
    --color-prettylights-syntax-string: #a5d6ff;
    --color-prettylights-syntax-string-regexp: #7ee787;
    --color-prettylights-syntax-sublimelinter-gutter-mark: #3d444d;
    --color-prettylights-syntax-variable: #ffa657;
    --fgColor-attention: #d29922;
    --fgColor-danger: #f85149;
    --fgColor-default: #f0f6fc;
    --fgColor-done: #ab7df8;
    --fgColor-muted: #9198a1;
    --fgColor-success: #3fb950;
    --borderColor-muted: #3d444db3;
    --color-prettylights-syntax-invalid-illegal-bg: var(--bgColor-danger-muted);
    --color-prettylights-syntax-invalid-illegal-text: var(--fgColor-danger);
    --focus-outlineColor: var(--borderColor-accent-emphasis);
    --borderColor-neutral-muted: var(--borderColor-muted);
  }
}
@media (prefers-color-scheme: light) {
  .markdown-body, [data-theme="light"] {
    /*light */
    color-scheme: light;
    --fgColor-danger: #d1242f;
    --bgColor-attention-muted: #fff8c5;
    --bgColor-muted: #f6f8fa;
    --bgColor-neutral-muted: #818b981f;
    --borderColor-accent-emphasis: #0969da;
    --borderColor-attention-emphasis: #9a6700;
    --borderColor-danger-emphasis: #cf222e;
    --borderColor-default: #d1d9e0;
    --borderColor-done-emphasis: #8250df;
    --borderColor-success-emphasis: #1a7f37;
    --color-prettylights-syntax-brackethighlighter-angle: #59636e;
    --color-prettylights-syntax-brackethighlighter-unmatched: #82071e;
    --color-prettylights-syntax-carriage-return-bg: #cf222e;
    --color-prettylights-syntax-carriage-return-text: #f6f8fa;
    --color-prettylights-syntax-comment: #59636e;
    --color-prettylights-syntax-constant: #0550ae;
    --color-prettylights-syntax-constant-other-reference-link: #0a3069;
    --color-prettylights-syntax-entity: #6639ba;
    --color-prettylights-syntax-entity-tag: #0550ae;
    --color-prettylights-syntax-invalid-illegal-text: var(--fgColor-danger);
    --color-prettylights-syntax-keyword: #cf222e;
    --color-prettylights-syntax-markup-changed-bg: #ffd8b5;
    --color-prettylights-syntax-markup-changed-text: #953800;
    --color-prettylights-syntax-markup-deleted-bg: #ffebe9;
    --color-prettylights-syntax-markup-deleted-text: #82071e;
    --color-prettylights-syntax-markup-heading: #0550ae;
    --color-prettylights-syntax-markup-ignored-bg: #0550ae;
    --color-prettylights-syntax-markup-ignored-text: #d1d9e0;
    --color-prettylights-syntax-markup-inserted-bg: #dafbe1;
    --color-prettylights-syntax-markup-inserted-text: #116329;
    --color-prettylights-syntax-markup-list: #3b2300;
    --color-prettylights-syntax-meta-diff-range: #8250df;
    --color-prettylights-syntax-string: #0a3069;
    --color-prettylights-syntax-string-regexp: #116329;
    --color-prettylights-syntax-sublimelinter-gutter-mark: #818b98;
    --color-prettylights-syntax-variable: #953800;
    --fgColor-accent: #0969da;
    --fgColor-attention: #9a6700;
    --fgColor-done: #8250df;
    --fgColor-muted: #59636e;
    --fgColor-success: #1a7f37;
    --bgColor-default: #ffffff;
    --borderColor-muted: #d1d9e0b3;
    --color-prettylights-syntax-invalid-illegal-bg: var(--bgColor-danger-muted);
    --color-prettylights-syntax-markup-bold: #1f2328;
    --color-prettylights-syntax-markup-italic: #1f2328;
    --color-prettylights-syntax-storage-modifier-import: #1f2328;
    --fgColor-default: #1f2328;
    --focus-outlineColor: var(--borderColor-accent-emphasis);
    --borderColor-neutral-muted: var(--borderColor-muted);
  }
}

.markdown-body {
  /** CSS default easing. Use for hover state changes and micro-interactions. */
  /** Accelerating motion. Use for elements exiting the viewport (moving off-screen). */
  /** Smooth acceleration and deceleration. Use for elements moving or morphing within the viewport. */
  /** Decelerating motion. Use for elements entering the viewport or appearing on screen. */
  /** Constant motion with no acceleration. Use for continuous animations like progress bars or loaders. */
  -ms-text-size-adjust: 100%;
  -webkit-text-size-adjust: 100%;
  margin: 0;
  font-weight: var(--base-text-weight-normal, 400);
  color: var(--fgColor-default);
  background-color: var(--bgColor-default);
  font-family: var(--fontStack-sansSerif, -apple-system, BlinkMacSystemFont, "Segoe UI", "Noto Sans", Helvetica, Arial, sans-serif, "Apple Color Emoji", "Segoe UI Emoji");
  font-size: 16px;
  line-height: 1.5;
  word-wrap: break-word;
}

.markdown-body a {
  text-decoration: underline;
  text-underline-offset: .2rem;
}

.markdown-body .octicon {
  display: inline-block;
  fill: currentColor;
  vertical-align: text-bottom;
}

.markdown-body h1:hover .anchor .octicon-link:before,
.markdown-body h2:hover .anchor .octicon-link:before,
.markdown-body h3:hover .anchor .octicon-link:before,
.markdown-body h4:hover .anchor .octicon-link:before,
.markdown-body h5:hover .anchor .octicon-link:before,
.markdown-body h6:hover .anchor .octicon-link:before {
  width: 16px;
  height: 16px;
  content: ' ';
  display: inline-block;
  background-color: currentColor;
  -webkit-mask-image: url("data:image/svg+xml,<svg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 16 16' version='1.1' aria-hidden='true'><path fill-rule='evenodd' d='M7.775 3.275a.75.75 0 001.06 1.06l1.25-1.25a2 2 0 112.83 2.83l-2.5 2.5a2 2 0 01-2.83 0 .75.75 0 00-1.06 1.06 3.5 3.5 0 004.95 0l2.5-2.5a3.5 3.5 0 00-4.95-4.95l-1.25 1.25zm-4.69 9.64a2 2 0 010-2.83l2.5-2.5a2 2 0 012.83 0 .75.75 0 001.06-1.06 3.5 3.5 0 00-4.95 0l-2.5 2.5a3.5 3.5 0 004.95 4.95l1.25-1.25a.75.75 0 00-1.06-1.06l-1.25 1.25a2 2 0 01-2.83 0z'></path></svg>");
  mask-image: url("data:image/svg+xml,<svg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 16 16' version='1.1' aria-hidden='true'><path fill-rule='evenodd' d='M7.775 3.275a.75.75 0 001.06 1.06l1.25-1.25a2 2 0 112.83 2.83l-2.5 2.5a2 2 0 01-2.83 0 .75.75 0 00-1.06 1.06 3.5 3.5 0 004.95 0l2.5-2.5a3.5 3.5 0 00-4.95-4.95l-1.25 1.25zm-4.69 9.64a2 2 0 010-2.83l2.5-2.5a2 2 0 012.83 0 .75.75 0 001.06-1.06 3.5 3.5 0 00-4.95 0l-2.5 2.5a3.5 3.5 0 004.95 4.95l1.25-1.25a.75.75 0 00-1.06-1.06l-1.25 1.25a2 2 0 01-2.83 0z'></path></svg>");
}

.markdown-body details,
.markdown-body figcaption,
.markdown-body figure {
  display: block;
}

.markdown-body summary {
  display: list-item;
}

.markdown-body [hidden] {
  display: none !important;
}

.markdown-body a {
  background-color: rgba(0,0,0,0);
  color: var(--fgColor-accent);
  text-decoration: none;
}

.markdown-body abbr[title] {
  border-bottom: none;
  -webkit-text-decoration: underline dotted;
  text-decoration: underline dotted;
}

.markdown-body b,
.markdown-body strong {
  font-weight: var(--base-text-weight-semibold, 600);
}

.markdown-body dfn {
  font-style: italic;
}

.markdown-body h1 {
  margin: .67em 0;
  font-weight: var(--base-text-weight-semibold, 600);
  padding-bottom: .3em;
  font-size: 2em;
  border-bottom: 1px solid var(--borderColor-muted);
}

.markdown-body mark {
  background-color: var(--bgColor-attention-muted);
  color: var(--fgColor-default);
}

.markdown-body small {
  font-size: 90%;
}

.markdown-body sub,
.markdown-body sup {
  font-size: 75%;
  line-height: 0;
  position: relative;
  vertical-align: baseline;
}

.markdown-body sub {
  bottom: -0.25em;
}

.markdown-body sup {
  top: -0.5em;
}

.markdown-body img {
  border-style: none;
  max-width: 100%;
  box-sizing: content-box;
}

.markdown-body code,
.markdown-body kbd,
.markdown-body pre,
.markdown-body samp {
  font-family: monospace;
  font-size: 1em;
}

.markdown-body figure {
  margin: 1em var(--base-size-40);
}

.markdown-body hr {
  box-sizing: content-box;
  overflow: hidden;
  background: rgba(0,0,0,0);
  border-bottom: 1px solid var(--borderColor-muted);
  height: .25em;
  padding: 0;
  margin: var(--base-size-24) 0;
  background-color: var(--borderColor-default);
  border: 0;
}

.markdown-body input {
  font: inherit;
  margin: 0;
  overflow: visible;
  font-family: inherit;
  font-size: inherit;
  line-height: inherit;
}

.markdown-body [type=button],
.markdown-body [type=reset],
.markdown-body [type=submit] {
  -webkit-appearance: button;
  appearance: button;
}

.markdown-body [type=checkbox],
.markdown-body [type=radio] {
  box-sizing: border-box;
  padding: 0;
}

.markdown-body [type=number]::-webkit-inner-spin-button,
.markdown-body [type=number]::-webkit-outer-spin-button {
  height: auto;
}

.markdown-body [type=search]::-webkit-search-cancel-button,
.markdown-body [type=search]::-webkit-search-decoration {
  -webkit-appearance: none;
  appearance: none;
}

.markdown-body ::-webkit-input-placeholder {
  color: inherit;
  opacity: .54;
}

.markdown-body ::-webkit-file-upload-button {
  -webkit-appearance: button;
  appearance: button;
  font: inherit;
}

.markdown-body a:hover {
  text-decoration: underline;
}

.markdown-body ::placeholder {
  color: var(--fgColor-muted);
  opacity: 1;
}

.markdown-body hr::before {
  display: table;
  content: "";
}

.markdown-body hr::after {
  display: table;
  clear: both;
  content: "";
}

.markdown-body table {
  border-spacing: 0;
  border-collapse: collapse;
  display: block;
  width: max-content;
  max-width: 100%;
  overflow: auto;
  font-variant: tabular-nums;
}

.markdown-body td,
.markdown-body th {
  padding: 0;
}

.markdown-body details summary {
  cursor: pointer;
}

.markdown-body a:focus,
.markdown-body [role=button]:focus,
.markdown-body input[type=radio]:focus,
.markdown-body input[type=checkbox]:focus {
  outline: 2px solid var(--focus-outlineColor);
  outline-offset: -2px;
  box-shadow: none;
}

.markdown-body a:focus:not(:focus-visible),
.markdown-body [role=button]:focus:not(:focus-visible),
.markdown-body input[type=radio]:focus:not(:focus-visible),
.markdown-body input[type=checkbox]:focus:not(:focus-visible) {
  outline: solid 1px rgba(0,0,0,0);
}

.markdown-body a:focus-visible,
.markdown-body [role=button]:focus-visible,
.markdown-body input[type=radio]:focus-visible,
.markdown-body input[type=checkbox]:focus-visible {
  outline: 2px solid var(--focus-outlineColor);
  outline-offset: -2px;
  box-shadow: none;
}

.markdown-body a:not([class]):focus,
.markdown-body a:not([class]):focus-visible,
.markdown-body input[type=radio]:focus,
.markdown-body input[type=radio]:focus-visible,
.markdown-body input[type=checkbox]:focus,
.markdown-body input[type=checkbox]:focus-visible {
  outline-offset: 0;
}

.markdown-body kbd {
  display: inline-block;
  padding: var(--base-size-4);
  font: 11px var(--fontStack-monospace, ui-monospace, SFMono-Regular, SF Mono, Menlo, Consolas, Liberation Mono, monospace);
  line-height: 10px;
  color: var(--fgColor-default);
  vertical-align: middle;
  background-color: var(--bgColor-muted);
  border: solid 1px var(--borderColor-neutral-muted);
  border-bottom-color: var(--borderColor-neutral-muted);
  border-radius: 6px;
  box-shadow: inset 0 -1px 0 var(--borderColor-neutral-muted);
}

.markdown-body h1,
.markdown-body h2,
.markdown-body h3,
.markdown-body h4,
.markdown-body h5,
.markdown-body h6 {
  margin-top: var(--base-size-24);
  margin-bottom: var(--base-size-16);
  font-weight: var(--base-text-weight-semibold, 600);
  line-height: 1.25;
}

.markdown-body h2 {
  font-weight: var(--base-text-weight-semibold, 600);
  padding-bottom: .3em;
  font-size: 1.5em;
  border-bottom: 1px solid var(--borderColor-muted);
}

.markdown-body h3 {
  font-weight: var(--base-text-weight-semibold, 600);
  font-size: 1.25em;
}

.markdown-body h4 {
  font-weight: var(--base-text-weight-semibold, 600);
  font-size: 1em;
}

.markdown-body h5 {
  font-weight: var(--base-text-weight-semibold, 600);
  font-size: .875em;
}

.markdown-body h6 {
  font-weight: var(--base-text-weight-semibold, 600);
  font-size: .85em;
  color: var(--fgColor-muted);
}

.markdown-body p {
  margin-top: 0;
  margin-bottom: 10px;
}

.markdown-body blockquote {
  margin: 0;
  padding: 0 1em;
  color: var(--fgColor-muted);
  border-left: .25em solid var(--borderColor-default);
}

.markdown-body ul,
.markdown-body ol {
  margin-top: 0;
  margin-bottom: 0;
  padding-left: 2em;
}

.markdown-body ol ol,
.markdown-body ul ol {
  list-style-type: lower-roman;
}

.markdown-body ul ul ol,
.markdown-body ul ol ol,
.markdown-body ol ul ol,
.markdown-body ol ol ol {
  list-style-type: lower-alpha;
}

.markdown-body dd {
  margin-left: 0;
}

.markdown-body tt,
.markdown-body code,
.markdown-body samp {
  font-family: var(--fontStack-monospace, ui-monospace, SFMono-Regular, SF Mono, Menlo, Consolas, Liberation Mono, monospace);
  font-size: 12px;
}

.markdown-body pre {
  margin-top: 0;
  margin-bottom: 0;
  font-family: var(--fontStack-monospace, ui-monospace, SFMono-Regular, SF Mono, Menlo, Consolas, Liberation Mono, monospace);
  font-size: 12px;
  word-wrap: normal;
}

.markdown-body .octicon {
  display: inline-block;
  overflow: visible !important;
  vertical-align: text-bottom;
  fill: currentColor;
}

.markdown-body input::-webkit-outer-spin-button,
.markdown-body input::-webkit-inner-spin-button {
  margin: 0;
  appearance: none;
}

.markdown-body .mr-2 {
  margin-right: var(--base-size-8, 8px) !important;
}

.markdown-body::before {
  display: table;
  content: "";
}

.markdown-body::after {
  display: table;
  clear: both;
  content: "";
}

.markdown-body>*:first-child {
  margin-top: 0 !important;
}

.markdown-body>*:last-child {
  margin-bottom: 0 !important;
}

.markdown-body a:not([href]) {
  color: inherit;
  text-decoration: none;
}

.markdown-body .absent {
  color: var(--fgColor-danger);
}

.markdown-body .anchor {
  float: left;
  padding-right: var(--base-size-4);
  margin-left: -20px;
  line-height: 1;
}

.markdown-body .anchor:focus {
  outline: none;
}

.markdown-body p,
.markdown-body blockquote,
.markdown-body ul,
.markdown-body ol,
.markdown-body dl,
.markdown-body table,
.markdown-body pre,
.markdown-body details {
  margin-top: 0;
  margin-bottom: var(--base-size-16);
}

.markdown-body blockquote>:first-child {
  margin-top: 0;
}

.markdown-body blockquote>:last-child {
  margin-bottom: 0;
}

.markdown-body h1 .octicon-link,
.markdown-body h2 .octicon-link,
.markdown-body h3 .octicon-link,
.markdown-body h4 .octicon-link,
.markdown-body h5 .octicon-link,
.markdown-body h6 .octicon-link {
  color: var(--fgColor-default);
  vertical-align: middle;
  visibility: hidden;
}

.markdown-body h1:hover .anchor,
.markdown-body h2:hover .anchor,
.markdown-body h3:hover .anchor,
.markdown-body h4:hover .anchor,
.markdown-body h5:hover .anchor,
.markdown-body h6:hover .anchor {
  text-decoration: none;
}

.markdown-body h1:hover .anchor .octicon-link,
.markdown-body h2:hover .anchor .octicon-link,
.markdown-body h3:hover .anchor .octicon-link,
.markdown-body h4:hover .anchor .octicon-link,
.markdown-body h5:hover .anchor .octicon-link,
.markdown-body h6:hover .anchor .octicon-link {
  visibility: visible;
}

.markdown-body h1 tt,
.markdown-body h1 code,
.markdown-body h2 tt,
.markdown-body h2 code,
.markdown-body h3 tt,
.markdown-body h3 code,
.markdown-body h4 tt,
.markdown-body h4 code,
.markdown-body h5 tt,
.markdown-body h5 code,
.markdown-body h6 tt,
.markdown-body h6 code {
  padding: 0 .2em;
  font-size: inherit;
}

.markdown-body summary h1,
.markdown-body summary h2,
.markdown-body summary h3,
.markdown-body summary h4,
.markdown-body summary h5,
.markdown-body summary h6 {
  display: inline-block;
}

.markdown-body summary h1 .anchor,
.markdown-body summary h2 .anchor,
.markdown-body summary h3 .anchor,
.markdown-body summary h4 .anchor,
.markdown-body summary h5 .anchor,
.markdown-body summary h6 .anchor {
  margin-left: -40px;
}

.markdown-body summary h1,
.markdown-body summary h2 {
  padding-bottom: 0;
  border-bottom: 0;
}

.markdown-body ul.no-list,
.markdown-body ol.no-list {
  padding: 0;
  list-style-type: none;
}

.markdown-body ol[type="a s"] {
  list-style-type: lower-alpha;
}

.markdown-body ol[type="A s"] {
  list-style-type: upper-alpha;
}

.markdown-body ol[type="i s"] {
  list-style-type: lower-roman;
}

.markdown-body ol[type="I s"] {
  list-style-type: upper-roman;
}

.markdown-body ol[type="1"] {
  list-style-type: decimal;
}

.markdown-body div>ol:not([type]) {
  list-style-type: decimal;
}

.markdown-body ul ul,
.markdown-body ul ol,
.markdown-body ol ol,
.markdown-body ol ul {
  margin-top: 0;
  margin-bottom: 0;
}

.markdown-body li>p {
  margin-top: var(--base-size-16);
}

.markdown-body li+li {
  margin-top: .25em;
}

.markdown-body dl {
  padding: 0;
}

.markdown-body dl dt {
  padding: 0;
  margin-top: var(--base-size-16);
  font-size: 1em;
  font-style: italic;
  font-weight: var(--base-text-weight-semibold, 600);
}

.markdown-body dl dd {
  padding: 0 var(--base-size-16);
  margin-bottom: var(--base-size-16);
}

.markdown-body table th {
  font-weight: var(--base-text-weight-semibold, 600);
}

.markdown-body table th,
.markdown-body table td {
  padding: 6px 13px;
  border: 1px solid var(--borderColor-default);
}

.markdown-body table td>:last-child {
  margin-bottom: 0;
}

.markdown-body table tr {
  background-color: var(--bgColor-default);
  border-top: 1px solid var(--borderColor-muted);
}

.markdown-body table tr:nth-child(2n) {
  background-color: var(--bgColor-muted);
}

.markdown-body table img {
  background-color: rgba(0,0,0,0);
}

.markdown-body img[align=right] {
  padding-left: 20px;
}

.markdown-body img[align=left] {
  padding-right: 20px;
}

.markdown-body .emoji {
  max-width: none;
  vertical-align: text-top;
  background-color: rgba(0,0,0,0);
}

.markdown-body span.frame {
  display: block;
  overflow: hidden;
}

.markdown-body span.frame>span {
  display: block;
  float: left;
  width: auto;
  padding: 7px;
  margin: 13px 0 0;
  overflow: hidden;
  border: 1px solid var(--borderColor-default);
}

.markdown-body span.frame span img {
  display: block;
  float: left;
}

.markdown-body span.frame span span {
  display: block;
  padding: 5px 0 0;
  clear: both;
  color: var(--fgColor-default);
}

.markdown-body span.align-center {
  display: block;
  overflow: hidden;
  clear: both;
}

.markdown-body span.align-center>span {
  display: block;
  margin: 13px auto 0;
  overflow: hidden;
  text-align: center;
}

.markdown-body span.align-center span img {
  margin: 0 auto;
  text-align: center;
}

.markdown-body span.align-right {
  display: block;
  overflow: hidden;
  clear: both;
}

.markdown-body span.align-right>span {
  display: block;
  margin: 13px 0 0;
  overflow: hidden;
  text-align: right;
}

.markdown-body span.align-right span img {
  margin: 0;
  text-align: right;
}

.markdown-body span.float-left {
  display: block;
  float: left;
  margin-right: 13px;
  overflow: hidden;
}

.markdown-body span.float-left span {
  margin: 13px 0 0;
}

.markdown-body span.float-right {
  display: block;
  float: right;
  margin-left: 13px;
  overflow: hidden;
}

.markdown-body span.float-right>span {
  display: block;
  margin: 13px auto 0;
  overflow: hidden;
  text-align: right;
}

.markdown-body code,
.markdown-body tt {
  padding: .2em .4em;
  margin: 0;
  font-size: 85%;
  white-space: break-spaces;
  background-color: var(--bgColor-neutral-muted);
  border-radius: 6px;
}

.markdown-body code br,
.markdown-body tt br {
  display: none;
}

.markdown-body del code {
  text-decoration: inherit;
}

.markdown-body samp {
  font-size: 85%;
}

.markdown-body pre code {
  font-size: 100%;
}

.markdown-body pre>code {
  padding: 0;
  margin: 0;
  word-break: normal;
  white-space: pre;
  background: rgba(0,0,0,0);
  border: 0;
}

.markdown-body .highlight {
  margin-bottom: var(--base-size-16);
}

.markdown-body .highlight pre {
  margin-bottom: 0;
  word-break: normal;
}

.markdown-body .highlight pre,
.markdown-body pre {
  padding: var(--base-size-16);
  overflow: auto;
  font-size: 85%;
  line-height: 1.45;
  color: var(--fgColor-default);
  background-color: var(--bgColor-muted);
  border-radius: 6px;
}

.markdown-body pre code,
.markdown-body pre tt {
  display: inline;
  padding: 0;
  margin: 0;
  overflow: visible;
  line-height: inherit;
  word-wrap: normal;
  background-color: rgba(0,0,0,0);
  border: 0;
}

.markdown-body .csv-data td,
.markdown-body .csv-data th {
  padding: 5px;
  overflow: hidden;
  font-size: 12px;
  line-height: 1;
  text-align: left;
  white-space: nowrap;
}

.markdown-body .csv-data .blob-num {
  padding: 10px var(--base-size-8) 9px;
  text-align: right;
  background: var(--bgColor-default);
  border: 0;
}

.markdown-body .csv-data tr {
  border-top: 0;
}

.markdown-body .csv-data th {
  font-weight: var(--base-text-weight-semibold, 600);
  background: var(--bgColor-muted);
  border-top: 0;
}

.markdown-body [data-footnote-ref]::before {
  content: "[";
}

.markdown-body [data-footnote-ref]::after {
  content: "]";
}

.markdown-body .footnotes {
  font-size: 12px;
  color: var(--fgColor-muted);
  border-top: 1px solid var(--borderColor-default);
}

.markdown-body .footnotes ol {
  padding-left: var(--base-size-16);
}

.markdown-body .footnotes ol ul {
  display: inline-block;
  padding-left: var(--base-size-16);
  margin-top: var(--base-size-16);
}

.markdown-body .footnotes li {
  position: relative;
}

.markdown-body .footnotes li:target::before {
  position: absolute;
  top: calc(var(--base-size-8)*-1);
  right: calc(var(--base-size-8)*-1);
  bottom: calc(var(--base-size-8)*-1);
  left: calc(var(--base-size-24)*-1);
  pointer-events: none;
  content: "";
  border: 2px solid var(--borderColor-accent-emphasis);
  border-radius: 6px;
}

.markdown-body .footnotes li:target {
  color: var(--fgColor-default);
}

.markdown-body .footnotes .data-footnote-backref g-emoji {
  font-family: monospace;
}

.markdown-body .pl-c {
  color: var(--color-prettylights-syntax-comment);
}

.markdown-body .pl-c1,
.markdown-body .pl-s .pl-v {
  color: var(--color-prettylights-syntax-constant);
}

.markdown-body .pl-e,
.markdown-body .pl-en {
  color: var(--color-prettylights-syntax-entity);
}

.markdown-body .pl-smi,
.markdown-body .pl-s .pl-s1 {
  color: var(--color-prettylights-syntax-storage-modifier-import);
}

.markdown-body .pl-ent {
  color: var(--color-prettylights-syntax-entity-tag);
}

.markdown-body .pl-k {
  color: var(--color-prettylights-syntax-keyword);
}

.markdown-body .pl-s,
.markdown-body .pl-pds,
.markdown-body .pl-s .pl-pse .pl-s1,
.markdown-body .pl-sr,
.markdown-body .pl-sr .pl-cce,
.markdown-body .pl-sr .pl-sre,
.markdown-body .pl-sr .pl-sra {
  color: var(--color-prettylights-syntax-string);
}

.markdown-body .pl-v,
.markdown-body .pl-smw {
  color: var(--color-prettylights-syntax-variable);
}

.markdown-body .pl-bu {
  color: var(--color-prettylights-syntax-brackethighlighter-unmatched);
}

.markdown-body .pl-ii {
  color: var(--color-prettylights-syntax-invalid-illegal-text);
  background-color: var(--color-prettylights-syntax-invalid-illegal-bg);
}

.markdown-body .pl-c2 {
  color: var(--color-prettylights-syntax-carriage-return-text);
  background-color: var(--color-prettylights-syntax-carriage-return-bg);
}

.markdown-body .pl-sr .pl-cce {
  font-weight: bold;
  color: var(--color-prettylights-syntax-string-regexp);
}

.markdown-body .pl-ml {
  color: var(--color-prettylights-syntax-markup-list);
}

.markdown-body .pl-mh,
.markdown-body .pl-mh .pl-en,
.markdown-body .pl-ms {
  font-weight: bold;
  color: var(--color-prettylights-syntax-markup-heading);
}

.markdown-body .pl-mi {
  font-style: italic;
  color: var(--color-prettylights-syntax-markup-italic);
}

.markdown-body .pl-mb {
  font-weight: bold;
  color: var(--color-prettylights-syntax-markup-bold);
}

.markdown-body .pl-md {
  color: var(--color-prettylights-syntax-markup-deleted-text);
  background-color: var(--color-prettylights-syntax-markup-deleted-bg);
}

.markdown-body .pl-mi1 {
  color: var(--color-prettylights-syntax-markup-inserted-text);
  background-color: var(--color-prettylights-syntax-markup-inserted-bg);
}

.markdown-body .pl-mc {
  color: var(--color-prettylights-syntax-markup-changed-text);
  background-color: var(--color-prettylights-syntax-markup-changed-bg);
}

.markdown-body .pl-mi2 {
  color: var(--color-prettylights-syntax-markup-ignored-text);
  background-color: var(--color-prettylights-syntax-markup-ignored-bg);
}

.markdown-body .pl-mdr {
  font-weight: bold;
  color: var(--color-prettylights-syntax-meta-diff-range);
}

.markdown-body .pl-ba {
  color: var(--color-prettylights-syntax-brackethighlighter-angle);
}

.markdown-body .pl-sg {
  color: var(--color-prettylights-syntax-sublimelinter-gutter-mark);
}

.markdown-body .pl-corl {
  text-decoration: underline;
  color: var(--color-prettylights-syntax-constant-other-reference-link);
}

.markdown-body [role=button]:focus:not(:focus-visible),
.markdown-body [role=tabpanel][tabindex="0"]:focus:not(:focus-visible),
.markdown-body button:focus:not(:focus-visible),
.markdown-body summary:focus:not(:focus-visible),
.markdown-body a:focus:not(:focus-visible) {
  outline: none;
  box-shadow: none;
}

.markdown-body [tabindex="0"]:focus:not(:focus-visible),
.markdown-body details-dialog:focus:not(:focus-visible) {
  outline: none;
}

.markdown-body g-emoji {
  display: inline-block;
  min-width: 1ch;
  font-family: "Apple Color Emoji","Segoe UI Emoji","Segoe UI Symbol";
  font-size: 1em;
  font-style: normal !important;
  font-weight: var(--base-text-weight-normal, 400);
  line-height: 1;
  vertical-align: -0.075em;
}

.markdown-body g-emoji img {
  width: 1em;
  height: 1em;
}

.markdown-body a:has(>p,>div,>pre,>blockquote) {
  display: block;
}

.markdown-body a:has(>p,>div,>pre,>blockquote):not(:has(.snippet-clipboard-content,>pre)) {
  width: fit-content;
}

.markdown-body a:has(>p,>div,>pre,>blockquote):has(.snippet-clipboard-content,>pre):focus-visible {
  outline: 2px solid var(--focus-outlineColor);
  outline-offset: 2px;
}

.markdown-body .task-list-item {
  list-style-type: none;
}

.markdown-body .task-list-item label {
  font-weight: var(--base-text-weight-normal, 400);
}

.markdown-body .task-list-item.enabled label {
  cursor: pointer;
}

.markdown-body .task-list-item+.task-list-item {
  margin-top: var(--base-size-4);
}

.markdown-body .task-list-item .handle {
  display: none;
}

.markdown-body .task-list-item-checkbox {
  margin: 0 .2em .25em -1.4em;
  vertical-align: middle;
}

.markdown-body ul:dir(rtl) .task-list-item-checkbox {
  margin: 0 -1.6em .25em .2em;
}

.markdown-body ol:dir(rtl) .task-list-item-checkbox {
  margin: 0 -1.6em .25em .2em;
}

.markdown-body .contains-task-list:hover .task-list-item-convert-container,
.markdown-body .contains-task-list:focus-within .task-list-item-convert-container {
  display: block;
  width: auto;
  height: 24px;
  overflow: visible;
  clip-path: none;
}

.markdown-body ::-webkit-calendar-picker-indicator {
  filter: invert(50%);
}

.markdown-body .markdown-alert {
  padding: var(--base-size-8) var(--base-size-16);
  margin-bottom: var(--base-size-16);
  color: inherit;
  border-left: .25em solid var(--borderColor-default);
}

.markdown-body .markdown-alert>:first-child {
  margin-top: 0;
}

.markdown-body .markdown-alert>:last-child {
  margin-bottom: 0;
}

.markdown-body .markdown-alert .markdown-alert-title {
  display: flex;
  font-weight: var(--base-text-weight-medium, 500);
  align-items: center;
  line-height: 1;
}

.markdown-body .markdown-alert.markdown-alert-note {
  border-left-color: var(--borderColor-accent-emphasis);
}

.markdown-body .markdown-alert.markdown-alert-note .markdown-alert-title {
  color: var(--fgColor-accent);
}

.markdown-body .markdown-alert.markdown-alert-important {
  border-left-color: var(--borderColor-done-emphasis);
}

.markdown-body .markdown-alert.markdown-alert-important .markdown-alert-title {
  color: var(--fgColor-done);
}

.markdown-body .markdown-alert.markdown-alert-warning {
  border-left-color: var(--borderColor-attention-emphasis);
}

.markdown-body .markdown-alert.markdown-alert-warning .markdown-alert-title {
  color: var(--fgColor-attention);
}

.markdown-body .markdown-alert.markdown-alert-tip {
  border-left-color: var(--borderColor-success-emphasis);
}

.markdown-body .markdown-alert.markdown-alert-tip .markdown-alert-title {
  color: var(--fgColor-success);
}

.markdown-body .markdown-alert.markdown-alert-caution {
  border-left-color: var(--borderColor-danger-emphasis);
}

.markdown-body .markdown-alert.markdown-alert-caution .markdown-alert-title {
  color: var(--fgColor-danger);
}

.markdown-body>*:first-child>.heading-element:first-child {
  margin-top: 0 !important;
}

.markdown-body .highlight pre:has(+.zeroclipboard-container) {
  min-height: 52px;
}

"""


if __name__ == "__main__":
    main()
