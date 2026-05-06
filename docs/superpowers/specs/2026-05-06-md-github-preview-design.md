# Markdown GitHub-Style Live Preview — Design

## Goal

A single-file Python script that watches a directory of Markdown files and live-renders them in the browser with GitHub-style HTML output.

## CLI

```
python md_preview.py [DIR] [--port 8765] [--host 127.0.0.1] [--no-browser]
```

- `DIR` — directory to watch (default: current directory)
- `--port` — HTTP port (default: 8765). If the port is in use, the script prints a clear error to stderr and exits with status 1. No port rotation.
- `--host` — bind address (default: `127.0.0.1`). Default is loopback only — `/assets/<path>` would otherwise expose arbitrary files in `DIR` to anyone on the same network. Overriding to `0.0.0.0` is allowed but the user takes responsibility.
- `--no-browser` — skip auto-opening the browser on start. By default the script opens `http://<host>:<port>/` (the index page).

If the environment variable `GITHUB_TOKEN` is set, the renderer uses it as a Bearer token when calling the GitHub API (raises the rate limit from 60/hr anonymous to 5000/hr).

## Dependencies

- `fastapi` + `uvicorn[standard]` — ASGI web framework and server. Native support for async SSE, lifespan startup/shutdown, static file mounts, and Pydantic-typed responses.
- `httpx` — async HTTP client for the GitHub Markdown API (idiomatic in the FastAPI ecosystem; `requests` would force sync calls in async handlers).
- `watchdog` — cross-platform file system events.
- `markdown` + `pymdown-extensions` — local fallback renderer with GFM extensions.
- `pygments` — code syntax highlighting in fallback mode.

## Architecture

Single Python file. FastAPI app started via `uvicorn.run(app, ...)`. Watchdog runs in its own thread (its native model); a small bridge marshals events onto the FastAPI event loop using `loop.call_soon_threadsafe(queue.put_nowait, event)`.

Conventions:
- All paths exposed in URLs, JSON, and SSE payloads are **relative to `DIR`, with POSIX `/` separators**, regardless of host OS. The script normalizes platform paths (`PurePath.as_posix()`) at every boundary.
- Markdown files are matched by the suffixes `.md` and `.markdown` (case-insensitive). This applies uniformly to the watcher filter, the index listing, and the `/files` JSON.

### Components

1. **FileWatcher**
   - `watchdog.observers.Observer` watching `DIR` recursively.
   - Filters to `.md` / `.markdown` files (case-insensitive).
   - **Debounce / coalescing**: a per-path 1-second timer collapses bursts (editors often emit modify→rename→modify on save). The timer resets on every new event for that path; when it fires, the latest event type for that path is broadcast.
   - On post-debounce event:
     1. Invalidate the render cache for that path (see Renderer note below).
     2. Push `{path: <rel-posix>, event: "modified"|"created"|"deleted"}` onto every active SSE client's `asyncio.Queue` (via `call_soon_threadsafe`).
   - **Move/rename handling**: watchdog's `moved` event carries both `src_path` and `dest_path`. The watcher decomposes it into two broadcast events: `{src, "deleted"}` followed by `{dest, "created"}`. This keeps the SSE payload schema uniform — no `moved` event type — and the index page's existing `created`/`deleted` handler refreshes naturally.

2. **Renderer**
   - Primary path: `httpx.AsyncClient.post("https://api.github.com/markdown", json={"text": ..., "mode": "gfm"})`. If `GITHUB_TOKEN` is set, sends `Authorization: Bearer <token>`.
   - Fallback: local `markdown` library with extensions (`extra`, `tables`, `fenced_code`, `codehilite`, `pymdownx.tilde`, `pymdownx.tasklist`).
   - **Differentiated cooldowns**:
     - HTTP 403 / 429 (rate-limit) → 600s cooldown.
     - Network errors / timeouts / 5xx → 30s cooldown.
     - During cooldown, `render()` skips the API entirely and renders locally. After cooldown expires, the next call retries the API.
   - **Render cache**: keyed by `(abs_path, mtime)`. Checked before invoking either the API or local renderer. Populated on every successful render (cold loads included, not just SSE-triggered re-renders).
   - **Why the FileWatcher invalidates the cache explicitly** (in addition to the mtime in the key): on some filesystems / editor write strategies, mtime granularity is 1 second or coarser, and an atomic-write-then-rename can leave mtime unchanged across content changes. Without explicit invalidation, the next fetch would stat the file, get the same mtime, and serve a stale cached render. **Do not "optimize" this invalidation away.**
   - **Invalidation removes ALL entries for the given path**, regardless of mtime — not only the entry matching the current mtime. This handles edge cases like `git checkout` reverting a file to an older mtime that's still cached.
   - **Cache eviction**: none. Bounded in practice by the number of `.md` files in `DIR` × number of distinct mtimes seen during the process's lifetime. Acceptable for a local dev tool.
   - The render mode (`api` or `local`) is exposed via the `X-Render-Mode` HTTP header on `/raw/<path>` responses.

3. **HTTP routes** (FastAPI app)
   - `GET /` — directory index page. Server-rendered HTML listing every `.md`/`.markdown` file under `DIR` as links to `/view/<rel-path>`. Inline JS subscribes to SSE and refreshes the list on `created`/`deleted` events (ignores `modified`).
   - `GET /view/{path:path}` — page shell: links the `/static/github-markdown.css` stylesheet, contains an `<article>` populated server-side via the renderer, and a small inline SSE client.
   - `GET /raw/{path:path}` — returns the rendered HTML fragment for the file (no shell). Sets `X-Render-Mode: api|local`.
   - `GET /files` — JSON. Shape: `{"files": ["a.md", "sub/b.md", ...]}` (sorted, relative POSIX paths). Used by the index page after SSE create/delete events.
   - `GET /events` — SSE endpoint via `StreamingResponse(media_type="text/event-stream")`. Per-connection `asyncio.Queue`. Streams `data: {"path": "...", "event": "modified"|"created"|"deleted"}\n\n`. The generator wraps `await queue.get()` in `asyncio.wait_for(..., timeout=15)`; on timeout it yields `: keepalive\n\n`, which doubles as both heartbeat and disconnect detector — yielding to a closed connection raises and unwinds into the `try/finally` that removes the queue from the broadcast set. Sketch:
     ```python
     queue = asyncio.Queue()
     sse_clients.add(queue)
     try:
         while True:
             try:
                 evt = await asyncio.wait_for(queue.get(), timeout=15)
                 yield f"data: {json.dumps(evt)}\n\n"
             except asyncio.TimeoutError:
                 yield ": keepalive\n\n"
     finally:
         sse_clients.discard(queue)
     ```
   - `GET /static/github-markdown.css` — single route returning the vendored CSS string with `media_type="text/css"`. No `StaticFiles` mount (it would need a directory; we have a string constant).
   - `GET /assets/{path:path}` — serves files from `DIR` (used for `<img>` tags with relative paths in markdown). Validates path stays under `DIR` before serving. Mime type via `mimetypes.guess_type`.

4. **Frontend**
   - **CSS**: `github-markdown.css` is **vendored as a string constant** in the script (`GITHUB_MD_CSS = """..."""`), served via the `/static/github-markdown.css` route. No CDN, no startup network call, no first-run cache file. The CSS is updated by hand when there's a reason to bump the version.
   - **`/view/<path>` page**: inline JS opens `EventSource('/events')`. On an event whose `path` matches the current page: `fetch('/raw/<path>')`, replace `<article>.innerHTML`, preserve scroll position. If the response's `X-Render-Mode` is `local`, show a dismissible "API rate-limited, using local renderer" banner.
   - **`/` index page**: inline JS opens `EventSource('/events')`. On any `created`/`deleted` event: `fetch('/files')` and re-render the file list. `modified` events are ignored on the index page. (Move/rename has already been decomposed into `deleted` + `created` by the watcher, so it's covered without a separate event type.)

## Data Flow

```
file save
   → watchdog FileSystemEventHandler (worker thread)
   → 1s per-path debounce timer
   → loop.call_soon_threadsafe:
        invalidate render cache for path
        push event onto every SSE client's asyncio.Queue
   → browsers receive SSE event:
       • /view page if event.path matches current view:
            fetch /raw/<path>
            → cache hit? return cached HTML.
              miss → API (unless in cooldown) → fall back to local on failure
            → return HTML fragment + X-Render-Mode header
            → browser swaps <article>.innerHTML, keeps scroll position
       • / index page on created/deleted:
            fetch /files → re-render the file list
```

## Application Lifecycle

- **Startup** (FastAPI lifespan), in this order:
  1. Capture `asyncio.get_running_loop()` into a module-level ref — must happen **before** the observer starts, otherwise the first event handler invocation may race ahead of the ref being populated and crash.
  2. Create the shared `httpx.AsyncClient` with `timeout=httpx.Timeout(10.0, connect=5.0)` so a stalled GitHub API connection can't pile up SSE-triggered renders. Reused across requests for connection pooling.
  3. Start the watchdog `Observer`.
- **Shutdown**: stop the watchdog observer, close the `httpx.AsyncClient`, drain SSE queues with a final terminator so client connections close cleanly.

**Browser auto-open**: a `threading.Timer(0.5, lambda: webbrowser.open(url))` is started just before `uvicorn.run()` blocks. The 0.5s delay gives uvicorn time to bind the listening socket. `url` resolves `0.0.0.0` to `127.0.0.1` for the displayed address (browsers can't connect to the meta-address `0.0.0.0` on most platforms).

## Error Handling

- **API failures**: caught at the renderer boundary. Two cooldown durations (10 min for rate-limit, 30s for transient). During cooldown, `render()` goes straight to local.
- **File not found** in `/view`, `/raw`, `/assets`: return 404.
- **Path traversal**: any resolved path that escapes `DIR` returns 403. Use `Path.resolve()` and check `is_relative_to(root)`.
- **Port in use**: `uvicorn` raises; the script catches, prints a clear stderr message ("port <N> is already in use; pass --port to choose another"), exits 1.
- **SSE disconnect**: detected via the 15s keepalive yield. When the client is gone, yielding `: keepalive` raises through the generator; the `finally` block removes the queue from the broadcast set. See the SSE generator sketch in the routes section.
- **Renderer total failure** (both paths fail): return 500 with a plain-text error message; the frontend shows it in the `<article>` area.

## Testing

Manual smoke test plan documented separately. Automated tests are out of scope for this single-file utility — the value is in seeing it live-render in a browser, which a unit test cannot validate.

## Out of Scope

- Authentication / multi-user.
- Editing markdown in the browser.
- Mermaid diagrams, math (KaTeX), and other GitHub features beyond what the API renders by default.
- Persistence / caching across runs.
- Windows-specific path edge cases beyond what `pathlib` handles natively.
