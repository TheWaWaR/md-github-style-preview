# Manual Smoke Test

Run through these steps after any change to `md_preview.py`.

## Setup

```bash
mkdir -p sample/sub sample/img
cat > sample/a.md <<'EOF'
# Hello

This is **bold** and ~~struck~~.

- [x] task done
- [ ] task open

```python
def f(x): return x * 2
```

![logo](img/test.png)
EOF
echo "# Sub file" > sample/sub/b.md
printf '\x89PNG\r\n\x1a\nfake' > sample/img/test.png

.venv/bin/python md_preview.py sample
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

`curl` normalizes `..` segments client-side before sending, so `--path-as-is` is required to actually transmit the dotted path:

```bash
curl -is --path-as-is "http://127.0.0.1:8765/raw/../../etc/passwd" | head -1
curl -is --path-as-is "http://127.0.0.1:8765/view/../../etc/passwd" | head -1
curl -is --path-as-is "http://127.0.0.1:8765/assets/../../etc/passwd" | head -1
```

- [ ] All three return `403`.

## API fallback (offline simulation)

Disconnect Wi-Fi. Save `sample/a.md` again.

- [ ] Banner appears: "API rate-limited, using local renderer".
- [ ] Content still updates.

Reconnect Wi-Fi, wait 35s+, save again.

- [ ] Banner disappears (`X-Render-Mode: api` returns).

## Asset image

- [ ] `a.md` references `img/test.png`. The browser DevTools network tab shows a 200 for `/assets/img/test.png`.

## Multi-tab fanout

- [ ] Open `/view/a.md` in two tabs. Edit the file once.
- [ ] Both tabs update. Server logs show one `/raw/a.md` request per tab; the cache means only the first tab's request triggers an actual API call (`X-Render-Mode: api`); the second tab gets the cached HTML.

## Port collision

```bash
.venv/bin/python md_preview.py sample --no-browser
# stderr: "error: port 8765 is already in use; pass --port to choose another"
# exit code: 1
```

## SSE keepalive

```bash
timeout 17 curl -sN http://127.0.0.1:8765/events | head -3
```

- [ ] Within 15s, prints `: keepalive`.
