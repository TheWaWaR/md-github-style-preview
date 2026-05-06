# Markdown GitHub-Style Live Preview Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build a single-file Python script that watches a directory of Markdown files and live-renders them in the browser with GitHub-style HTML output.

**Architecture:** FastAPI ASGI app with SSE for live updates. Watchdog runs in its own thread; events are debounced (1s per path) and bridged onto the asyncio loop via `loop.call_soon_threadsafe`. Renders go through a cooldown-aware GitHub API client (`httpx`) with a local `markdown` fallback, all behind a per-path cache invalidated explicitly by the file watcher. CSS is vendored as a Python string constant; HTTP server binds `127.0.0.1` by default for safety.

**Tech Stack:** Python 3.10+, FastAPI, uvicorn, httpx, watchdog, markdown + pymdown-extensions, pygments.

**Spec:** `docs/superpowers/specs/2026-05-06-md-github-preview-design.md`

**Note on testing:** The spec marks automated tests out of scope — the value is verifying behavior live in a browser. Each task ends with a **manual verification step** instead of `pytest`. Do not skip those steps.

---

## File Structure

```
md-github-preview/
├── md_preview.py                 # the entire script — single file
├── requirements.txt              # pinned deps
├── .gitignore                    # python ignores
├── docs/
│   ├── superpowers/
│   │   ├── specs/2026-05-06-md-github-preview-design.md   # already exists
│   │   └── plans/2026-05-06-md-github-preview.md          # this file
│   └── smoke-test.md             # manual verification steps (Task 14)
└── sample/                       # for manual testing only, not committed
    └── ... .md files
```

`md_preview.py` is organized in this order (top→bottom):
1. Imports
2. CLI / argparse
3. Path utilities
4. Renderer (local + API + cache + cooldown)
5. FileWatcher (debounce + broadcast)
6. SSE broadcast set + helpers
7. FastAPI app + routes
8. HTML template strings (small)
9. `GITHUB_MD_CSS` string constant (vendored CSS, at the bottom — long)
10. `main()` entrypoint

---

## Task 1: Project scaffolding & FastAPI hello-world

**Files:**
- Create: `requirements.txt`
- Create: `.gitignore`
- Create: `md_preview.py`

- [ ] **Step 1: Initialize git**

```bash
cd /Users/qianlinfeng/projects/thewawar/md-github-preview
git init
git add docs/
git commit -m "chore: import design spec and implementation plan"
```

- [ ] **Step 2: Write `requirements.txt`**

```
fastapi==0.115.4
uvicorn[standard]==0.32.0
httpx==0.27.2
watchdog==5.0.3
markdown==3.7
pymdown-extensions==10.11.2
pygments==2.18.0
```

- [ ] **Step 3: Write `.gitignore`**

```
__pycache__/
*.pyc
.venv/
venv/
.DS_Store
sample/
```

- [ ] **Step 4: Create venv and install**

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

Expected: install completes without errors.

- [ ] **Step 5: Write `md_preview.py` with imports, argparse, and a hello-world FastAPI app**

```python
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


# ----- FastAPI app -----

app = FastAPI()


@app.get("/", response_class=HTMLResponse)
async def index() -> str:
    return "<h1>md-preview: hello</h1>"


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
```

- [ ] **Step 6: Manual verification — server boots**

```bash
python md_preview.py &
sleep 1
curl -s http://127.0.0.1:8765/
kill %1
```

Expected: `<h1>md-preview: hello</h1>`.

- [ ] **Step 7: Commit**

```bash
git add requirements.txt .gitignore md_preview.py
git commit -m "feat: scaffold FastAPI app with CLI args"
```

---

## Task 2: Path utilities

**Files:**
- Modify: `md_preview.py` (add path helpers section after imports / globals)

- [ ] **Step 1: Add path utility functions**

Insert this block after the `ROOT: Path` global, before the `# ----- FastAPI app -----` comment:

```python
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
```

- [ ] **Step 2: Manual verification — list a sample directory**

```bash
mkdir -p sample/sub
echo "# Hello" > sample/a.md
echo "# Sub" > sample/sub/b.markdown
python -c "
import md_preview as m
from pathlib import Path
m.ROOT = Path('sample').resolve()
print(m.list_markdown_files())
print(m.to_rel_posix(m.ROOT / 'sub' / 'b.markdown'))
"
```

Expected: `['a.md', 'sub/b.markdown']` and `sub/b.markdown`.

- [ ] **Step 3: Commit**

```bash
git add md_preview.py
git commit -m "feat: add path utilities for ROOT-relative POSIX normalization"
```

---

## Task 3: File enumeration routes (`/files` and `/` index)

**Files:**
- Modify: `md_preview.py`

- [ ] **Step 1: Replace the placeholder `/` handler and add `/files`**

Replace the existing `index()` route block with:

```python
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
```

- [ ] **Step 2: Manual verification — index lists files**

```bash
python md_preview.py sample &
sleep 1
curl -s http://127.0.0.1:8765/files
echo
curl -s http://127.0.0.1:8765/ | grep -E '<li>|<h1>'
kill %1
```

Expected:
- `/files` → `{"files":["a.md","sub/b.markdown"]}`
- `/` → HTML containing `<li><a href="/view/a.md">a.md</a></li>` etc.

- [ ] **Step 3: Commit**

```bash
git add md_preview.py
git commit -m "feat: add /files JSON route and / HTML index page"
```

---

## Task 4: Local fallback renderer

**Files:**
- Modify: `md_preview.py`

- [ ] **Step 1: Add the local renderer**

Add this section after the path utilities block, before `# ----- Routes`:

```python
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
```

- [ ] **Step 2: Manual verification — local renderer works**

```bash
python -c "
import md_preview as m
print(m.render_local('# Hi\n\n- [x] done\n- [ ] todo\n\n\`\`\`python\nprint(1)\n\`\`\`'))
"
```

Expected: HTML output containing `<h1>Hi</h1>`, a task list with checkboxes, and a `<div class="highlight">` syntax-highlighted code block.

- [ ] **Step 3: Commit**

```bash
git add md_preview.py
git commit -m "feat: add local markdown renderer fallback"
```

---

## Task 5: GitHub API renderer with cooldown

**Files:**
- Modify: `md_preview.py`

- [ ] **Step 1: Add API renderer + cooldown state + render() entry point**

Add to the renderer section (after `render_local`):

```python
# ----- Renderer: GitHub API + cooldown -----

GITHUB_API_URL = "https://api.github.com/markdown"
COOLDOWN_RATE_LIMIT_S = 600.0   # 10 min for 403/429
COOLDOWN_TRANSIENT_S = 30.0     # 30s for network/5xx


@dataclass
class RenderState:
    """Process-wide renderer state (single instance, mutated under the asyncio loop)."""
    cooldown_until: float = 0.0
    client: Optional[httpx.AsyncClient] = None  # set in lifespan startup
    cache: dict[tuple[Path, float], str] = field(default_factory=dict)  # (abs_path, mtime) -> html


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
```

- [ ] **Step 2: Wire up `httpx.AsyncClient` in lifespan**

Replace the `app = FastAPI()` line with:

```python
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
```

- [ ] **Step 3: Manual verification — API renderer works (online)**

```bash
python -c "
import asyncio, md_preview as m, httpx
async def run():
    m.STATE.client = httpx.AsyncClient(timeout=httpx.Timeout(10.0, connect=5.0))
    try:
        html, mode = await m.render('# Hi from API')
        print(mode, '→', html[:200])
    finally:
        await m.STATE.client.aclose()
asyncio.run(run())
"
```

Expected: `api → <h1>...Hi from API</h1>` (or similar GitHub-flavored output). If you have no internet, expect `local → ...`.

- [ ] **Step 4: Manual verification — cooldown forces local**

```bash
python -c "
import asyncio, time, md_preview as m, httpx
async def run():
    m.STATE.client = httpx.AsyncClient()
    try:
        m.STATE.cooldown_until = time.monotonic() + 60
        html, mode = await m.render('# cooldown test')
        print(mode)
        assert mode == 'local'
    finally:
        await m.STATE.client.aclose()
asyncio.run(run())
"
```

Expected: prints `local`.

- [ ] **Step 5: Commit**

```bash
git add md_preview.py
git commit -m "feat: GitHub API renderer with differentiated cooldowns"
```

---

## Task 6: Render cache with explicit invalidation

**Files:**
- Modify: `md_preview.py`

- [ ] **Step 1: Add cache helpers and a path-based renderer**

Add to the renderer section (after `render`):

```python
async def render_path(abs_path: Path) -> tuple[str, str]:
    """Render the markdown file at abs_path. Returns (html, mode).

    Uses the (abs_path, mtime) cache. The FileWatcher invalidates by path
    explicitly because some filesystems / atomic-write strategies leave mtime
    unchanged across content changes — DO NOT remove that invalidation.
    """
    mtime = abs_path.stat().st_mtime
    key = (abs_path, mtime)
    if key in STATE.cache:
        # Cache stores (html, mode); we only need html — mode is determined at render-time,
        # but for a cached entry we treat it as "api" since we cache successful API or local outputs.
        # See _cached_mode below.
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
```

Then update `RenderState` (already added in Task 5) — its `cache` value type comment needs updating. Replace this line in the dataclass:

```python
    cache: dict[tuple[Path, float], str] = field(default_factory=dict)  # (abs_path, mtime) -> html
```

with:

```python
    cache: dict[tuple[Path, float], tuple[str, str]] = field(default_factory=dict)  # (abs_path, mtime) -> (html, mode)
```

- [ ] **Step 2: Manual verification — cache hits and invalidation work**

```bash
python -c "
import asyncio, md_preview as m, httpx, time
from pathlib import Path
async def run():
    m.ROOT = Path('sample').resolve()
    m.STATE.client = httpx.AsyncClient()
    try:
        p = m.ROOT / 'a.md'
        h1, mode1 = await m.render_path(p)
        h2, _ = await m.render_path(p)  # should be cache hit
        assert h1 == h2
        assert (p, p.stat().st_mtime) in m.STATE.cache
        m.invalidate_cache_for(p)
        assert all(k[0] != p for k in m.STATE.cache)
        print('OK')
    finally:
        await m.STATE.client.aclose()
asyncio.run(run())
"
```

Expected: `OK`.

- [ ] **Step 3: Commit**

```bash
git add md_preview.py
git commit -m "feat: render cache keyed by (path, mtime) with path-wide invalidation"
```

---

## Task 7: `/raw` and `/view` routes

**Files:**
- Modify: `md_preview.py`

- [ ] **Step 1: Add the routes and the `/view` page template**

Add this template at the templates section (after `INDEX_TEMPLATE`):

```python
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
</style>
</head><body>
<div id="banner"><span id="banner-text"></span><button onclick="document.getElementById('banner').style.display='none'">×</button></div>
<article id="content" class="markdown-body">{html}</article>
<script>
  const PATH = {path_json};
  const article = document.getElementById('content');
  const banner = document.getElementById('banner');
  const bannerText = document.getElementById('banner-text');

  function showBanner(msg) {{ bannerText.textContent = msg; banner.style.display = 'block'; }}

  async function refresh() {{
    const r = await fetch('/raw/' + PATH);
    if (!r.ok) {{ article.innerHTML = '<p>Render error: ' + r.status + '</p>'; return; }}
    const mode = r.headers.get('X-Render-Mode');
    if (mode === 'local') showBanner('API rate-limited, using local renderer');
    const scrollY = window.scrollY;
    article.innerHTML = await r.text();
    window.scrollTo(0, scrollY);
  }}

  const es = new EventSource('/events');
  es.addEventListener('message', (e) => {{
    const evt = JSON.parse(e.data);
    if (evt.path === PATH && evt.event === 'modified') refresh();
  }});
</script>
</body></html>
"""
```

Add the routes (after the `/files` handler):

```python
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
```

- [ ] **Step 2: Manual verification — `/raw` and `/view` work**

```bash
python md_preview.py sample &
sleep 1
echo "--- /raw/a.md ---"
curl -is http://127.0.0.1:8765/raw/a.md | head -10
echo "--- /view/a.md ---"
curl -s http://127.0.0.1:8765/view/a.md | grep -E '<h1>|EventSource|markdown-body' | head -5
echo "--- traversal ---"
curl -is http://127.0.0.1:8765/raw/../../etc/passwd | head -3
kill %1
```

Expected:
- `/raw/a.md` returns `<h1>Hello</h1>` HTML with `X-Render-Mode: api` (or `local` offline).
- `/view/a.md` HTML contains `<h1>Hello</h1>`, `EventSource`, and `markdown-body`.
- Traversal returns `403 Forbidden`.

- [ ] **Step 3: Commit**

```bash
git add md_preview.py
git commit -m "feat: /raw render endpoint and /view page shell"
```

---

## Task 8: `/assets` route for relative-path images

**Files:**
- Modify: `md_preview.py`

- [ ] **Step 1: Add the `/assets` route**

Add after the `/raw` route:

```python
@app.get("/assets/{path:path}")
async def assets(path: str) -> Response:
    abs_path = safe_resolve(path)
    if not abs_path.is_file():
        raise HTTPException(status_code=404, detail="not a file")
    mime, _ = mimetypes.guess_type(str(abs_path))
    return Response(content=abs_path.read_bytes(), media_type=mime or "application/octet-stream")
```

- [ ] **Step 2: Manual verification — assets are served**

```bash
mkdir -p sample/img
printf '\x89PNG\r\n\x1a\nfake' > sample/img/test.png
python md_preview.py sample &
sleep 1
curl -is http://127.0.0.1:8765/assets/img/test.png | head -5
kill %1
```

Expected: `200 OK` with `content-type: image/png`.

- [ ] **Step 3: Commit**

```bash
git add md_preview.py
git commit -m "feat: /assets route for serving relative paths inside DIR"
```

---

## Task 9: Vendored CSS and `/static/github-markdown.css`

**Files:**
- Modify: `md_preview.py`

- [ ] **Step 1: Download the github-markdown.css content**

```bash
curl -sSL https://raw.githubusercontent.com/sindresorhus/github-markdown-css/main/github-markdown.css -o /tmp/github-markdown.css
wc -l /tmp/github-markdown.css
```

Expected: a non-zero line count (~1000 lines).

- [ ] **Step 2: Add the route handler at the top of `md_preview.py`'s routes section**

After the `/assets` route, add:

```python
@app.get("/static/github-markdown.css")
async def github_markdown_css() -> Response:
    return Response(content=GITHUB_MD_CSS, media_type="text/css")
```

- [ ] **Step 3: Append the vendored CSS at the bottom of `md_preview.py`**

At the very bottom of `md_preview.py` (after `if __name__ == "__main__": main()`), add:

```python
# ============================================================================
# Vendored github-markdown-css (https://github.com/sindresorhus/github-markdown-css)
# Updated by hand. To refresh:
#   curl -sSL https://raw.githubusercontent.com/sindresorhus/github-markdown-css/main/github-markdown.css
# ============================================================================
GITHUB_MD_CSS = r"""
<<paste the full contents of /tmp/github-markdown.css here>>
"""
```

Replace the `<<paste...>>` placeholder with the literal CSS content. Use `r"""..."""` so the CSS's `\` characters (in `content: "\2192"` etc.) survive unescaped.

**Important:** the `GITHUB_MD_CSS` constant must be defined **before** the route uses it at request time — but Python evaluates module-level code top-to-bottom only at import. The route function body is not executed at import; it's executed when a request arrives, by which point `GITHUB_MD_CSS` exists. So the placement at the bottom of the file is correct.

- [ ] **Step 4: Manual verification — CSS is served**

```bash
python md_preview.py sample &
sleep 1
curl -is http://127.0.0.1:8765/static/github-markdown.css | head -3
curl -s http://127.0.0.1:8765/static/github-markdown.css | wc -c
kill %1
```

Expected: `200 OK` with `content-type: text/css`, byte count > 10000.

- [ ] **Step 5: Commit**

```bash
git add md_preview.py
git commit -m "feat: vendor github-markdown.css as a string constant"
```

---

## Task 10: SSE `/events` endpoint

**Files:**
- Modify: `md_preview.py`

- [ ] **Step 1: Add SSE broadcast set + endpoint**

Add this section before the routes section:

```python
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
```

Add the route handler (after `/raw`, before `/assets`):

```python
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
```

- [ ] **Step 2: Manual verification — SSE keepalive works and broadcasts reach clients**

```bash
python md_preview.py sample &
sleep 1
# In one shell, open the SSE stream (head -n3 to bound the test)
(curl -sN http://127.0.0.1:8765/events &
  CURL_PID=$!
  sleep 1
  # In python, broadcast a test event
  python -c "
import asyncio, md_preview as m
async def run():
    # Wait for the curl above to register; broadcast() must run on the same loop as the SSE client.
    # Easiest: use the running app via httpx? Actually, broadcast must run in the app's loop.
    # We test broadcast separately: just import and verify shape.
    print('broadcast() defined:', callable(m.broadcast))
asyncio.run(run())
"
  sleep 17
  kill $CURL_PID 2>/dev/null
) 2>&1 | head -20
kill %1
```

The cleanest manual verification is the end-to-end test in Task 14. For now, just confirm:

```bash
python md_preview.py sample &
sleep 1
timeout 17 curl -sN http://127.0.0.1:8765/events | head -2
kill %1
```

Expected: within 15s, the stream prints a `: keepalive` comment line.

- [ ] **Step 3: Commit**

```bash
git add md_preview.py
git commit -m "feat: SSE /events endpoint with keepalive-driven disconnect detection"
```

---

## Task 11: Watchdog observer with debounce + move decomposition

**Files:**
- Modify: `md_preview.py`

- [ ] **Step 1: Add the FileWatcher**

Add this section after the `# ----- SSE broadcast -----` block:

```python
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

    # FileSystemEventHandler hooks
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
            # If file no longer under ROOT, drop (e.g. file moved out of tree).
            try:
                rel = to_rel_posix(p)
            except ValueError:
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
```

- [ ] **Step 2: Wire the observer into the lifespan**

Update the `lifespan` function:

```python
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
```

- [ ] **Step 3: Manual verification — file edit triggers SSE event**

In terminal A:

```bash
python md_preview.py sample
```

In terminal B:

```bash
curl -sN http://127.0.0.1:8765/events &
CURL_PID=$!
sleep 1
echo "# Edited at $(date)" >> sample/a.md
sleep 2
kill $CURL_PID
```

Expected: terminal B prints `data: {"path": "a.md", "event": "modified"}` within ~1.5s of the `echo`.

Then test rename decomposition:

```bash
curl -sN http://127.0.0.1:8765/events &
CURL_PID=$!
sleep 1
mv sample/a.md sample/renamed.md
sleep 2
kill $CURL_PID
mv sample/renamed.md sample/a.md  # restore
```

Expected: two events — one `{"path":"a.md","event":"deleted"}` and one `{"path":"renamed.md","event":"created"}`.

- [ ] **Step 4: Commit**

```bash
git add md_preview.py
git commit -m "feat: watchdog observer with 1s debounce and move-event decomposition"
```

---

## Task 12: Browser auto-open + port collision handling

**Files:**
- Modify: `md_preview.py`

- [ ] **Step 1: Update `main()`**

Replace the existing `main()` function with:

```python
def main() -> None:
    global ROOT
    args = parse_args()
    ROOT = Path(args.dir).resolve()
    if not ROOT.is_dir():
        print(f"error: {ROOT} is not a directory", file=sys.stderr)
        sys.exit(1)

    # Browser auto-open: 0.5s delay so uvicorn has time to bind.
    if not args.no_browser:
        display_host = "127.0.0.1" if args.host == "0.0.0.0" else args.host
        url = f"http://{display_host}:{args.port}/"
        threading.Timer(0.5, lambda: webbrowser.open(url)).start()

    try:
        uvicorn.run(app, host=args.host, port=args.port, log_level="info")
    except OSError as e:
        if "address already in use" in str(e).lower() or getattr(e, "errno", None) in (48, 98):
            print(
                f"error: port {args.port} is already in use; pass --port to choose another",
                file=sys.stderr,
            )
            sys.exit(1)
        raise
```

- [ ] **Step 2: Manual verification — port collision message**

```bash
python md_preview.py sample &
sleep 1
python md_preview.py sample --no-browser
# second invocation should print the friendly error, not a raw traceback
```

Expected: stderr line `error: port 8765 is already in use; pass --port to choose another`, exit code 1.

```bash
kill %1
```

- [ ] **Step 3: Manual verification — `--no-browser` suppresses, default opens**

```bash
python md_preview.py sample --no-browser &
sleep 1
# (no browser tab opened)
kill %1
python md_preview.py sample &
sleep 2
# (a browser tab should have opened to http://127.0.0.1:8765/)
kill %1
```

- [ ] **Step 4: Commit**

```bash
git add md_preview.py
git commit -m "feat: browser auto-open with delayed timer and friendly port-in-use error"
```

---

## Task 13: End-to-end smoke test document

**Files:**
- Create: `docs/smoke-test.md`

- [ ] **Step 1: Write the smoke test playbook**

```markdown
# Manual Smoke Test

Run through these steps after any change to `md_preview.py`.

## Setup

```bash
mkdir -p sample/sub
cat > sample/a.md <<'EOF'
# Hello

This is **bold** and ~~struck~~.

- [x] task done
- [ ] task open

\`\`\`python
def f(x): return x * 2
\`\`\`

![logo](img/test.png)
EOF
echo "# Sub file" > sample/sub/b.md
mkdir -p sample/img && printf '\x89PNG\r\n\x1a\nfake' > sample/img/test.png

python md_preview.py sample
```

A browser tab should open at `http://127.0.0.1:8765/`.

## Index page

- [ ] Lists `a.md` and `sub/b.md` (alphabetical).
- [ ] Clicking a link navigates to `/view/<path>`.

## View page (online: GitHub API mode)

- [ ] `a.md` renders with GitHub styling (max-width centered article, proper headings).
- [ ] Code block has syntax highlighting.
- [ ] Task list shows real checkboxes.
- [ ] No yellow rate-limit banner is visible.

## Live update

- [ ] Edit `sample/a.md` (add a paragraph), save.
- [ ] Browser content updates within ~1-2s **without scrolling to top**.

## Index live update

- [ ] Open `http://127.0.0.1:8765/`.
- [ ] In another terminal: `touch sample/c.md`.
- [ ] Index list grows to include `c.md` within ~2s.
- [ ] `rm sample/c.md` — list shrinks.

## Rename = delete + create

- [ ] On the index page: `mv sample/a.md sample/renamed.md`.
- [ ] List loses `a.md`, gains `renamed.md` within ~2s.
- [ ] Restore: `mv sample/renamed.md sample/a.md`.

## Path traversal blocked

```bash
curl -is http://127.0.0.1:8765/raw/../../etc/passwd | head -1
curl -is http://127.0.0.1:8765/view/../../etc/passwd | head -1
curl -is http://127.0.0.1:8765/assets/../../etc/passwd | head -1
```

- [ ] All three return `403`.

## API fallback (offline simulation)

In a python REPL on the running server's process:

```bash
python md_preview.py sample &
sleep 1
# Force cooldown by calling the renderer with a bogus base URL — easier: just disconnect Wi-Fi for 30s.
```

Disconnect Wi-Fi, save the file again.
- [ ] Banner appears: "API rate-limited, using local renderer".
- [ ] Content still updates.

Reconnect Wi-Fi, wait 35s+, save again.
- [ ] Banner disappears (mode flips back to `api`).

## Asset image

- [ ] `a.md` shows `img/test.png` reference; image attempts to load via `/assets/img/test.png` (browser DevTools network tab confirms a 200).

## Multi-tab fanout

- [ ] Open `/view/a.md` in two tabs. Edit the file once.
- [ ] Both tabs update. (And: server logs should show a single `/raw/a.md` request per tab; first hits API/local, the rest hit cache — you can confirm via inspecting `STATE.cache` if you want.)

## Port collision

```bash
python md_preview.py sample --no-browser
# stderr: "error: port 8765 is already in use; pass --port to choose another"
# exit code: 1
```

## SSE keepalive

```bash
timeout 17 curl -sN http://127.0.0.1:8765/events | head -3
```

- [ ] Within 15s, prints `: keepalive`.
```

- [ ] **Step 2: Run the full smoke test**

Walk through every checkbox above. Fix any failures by jumping back to the relevant task in `2026-05-06-md-github-preview.md`.

- [ ] **Step 3: Commit**

```bash
git add docs/smoke-test.md
git commit -m "docs: manual smoke test playbook"
```

---

## Final Verification

- [ ] Re-run the smoke test in `docs/smoke-test.md`. Every checkbox green.
- [ ] `python md_preview.py --help` shows the documented CLI.
- [ ] Cold start time (script launch → first render in browser) < 2s on a sample directory of < 50 markdown files.
- [ ] `git log --oneline` shows one commit per task, in order.
