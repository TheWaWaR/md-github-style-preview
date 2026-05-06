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
