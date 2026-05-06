# md-github-style-preview

本地 Markdown 实时预览服务，使用 GitHub 渲染样式。在浏览器中边写边看，效果与 GitHub 上一致。

## 目的

在本地编辑 Markdown 时，希望看到与 GitHub 完全一致的渲染效果（包括代码高亮、任务列表、表格、删除线等），并且在文件保存后立即刷新预览，无需手动操作。

实现方式：

- 优先调用 GitHub `/markdown` API 渲染，最大化保真度。
- API 不可用时自动回落到本地 `python-markdown` 渲染器，保证离线可用。
- `watchdog` 监听文件变化，通过 SSE 推送给浏览器，刷新时保留滚动位置。

## 特性

- **GitHub 风格**：内置 [github-markdown-css](https://github.com/sindresorhus/github-markdown-css)，自动适配浅色/深色模式。
- **双渲染器**：在线走 GitHub API（需要时带 `GITHUB_TOKEN`），离线/限流走本地渲染器，并显示横幅提示。
- **限流冷却**：API 返回 403/429 时进入 10 分钟冷却；网络/5xx 错误冷却 30 秒，期间直接走本地渲染。
- **文件监听**：递归监听目录，1 秒去抖；移动文件被分解为 `deleted` + `created` 事件。
- **实时刷新**：基于 SSE，正在查看的文件被修改后自动重渲染，保持滚动位置；文件新增/删除时索引页自动更新。
- **多标签共享缓存**：以 `(路径, mtime)` 为键缓存渲染结果，多个标签同时打开同一文件只触发一次 API 调用。
- **资源代理**：Markdown 中引用的图片等通过 `/assets/<path>` 提供。
- **路径穿越防护**：所有路径在 `ROOT` 下解析，越界请求返回 403。
- **端口占用提示**：启动前预探测端口，被占用时打印友好错误并退出，而不是让 uvicorn 抛栈。
- **可选自动打开浏览器**：默认启动后 0.5 秒打开浏览器，可用 `--no-browser` 关闭。

## 不足 / 已知限制

- **仅适合本地开发**：默认绑定 `127.0.0.1`，没有鉴权，不要暴露到公网。
- **GitHub API 限流**：未认证 60 次/小时、认证后 5000 次/小时；触发限流后 10 分钟内只能用本地渲染（视觉差异：GFM 的部分扩展本地渲染未必一致，例如 alert blocks `> [!NOTE]` 等）。
- **本地渲染器与 GitHub 不完全等价**：不支持 GitHub 特有扩展（mention、issue 引用、emoji shortcode、mermaid 等）。
- **轮询单文件粒度**：浏览器只会响应当前正在查看的那一个文件的修改事件；切到别的文件需要手动点击索引。
- **CSS 内联在 Python 文件里**：`github-markdown.css` 直接嵌在 `md_preview.py` 末尾，更新需要手动 `curl` 后替换。
- **没有持久化历史**：缓存只在进程内存中，重启即丢失。
- **没有打包**：单文件脚本，没有发布到 PyPI；通过克隆仓库使用。

## 使用方法

### 1. 安装依赖

需要 Python 3.10+。

```bash
git clone <this-repo>
cd md-github-style-preview
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

### 2. 启动

```bash
# 监听当前目录
python md_preview.py

# 监听指定目录
python md_preview.py path/to/notes

# 自定义端口、关闭自动打开浏览器
python md_preview.py path/to/notes --port 9000 --no-browser
```

启动后访问 `http://127.0.0.1:8765/` 查看 Markdown 文件列表，点击进入预览页。

### 3.（可选）配置 GitHub Token

未认证情况下 GitHub API 仅允许 60 次/小时，重度使用很容易触发限流。配置 token 可提升到 5000 次/小时：

```bash
export GITHUB_TOKEN=ghp_xxxxxxxxxxxxxxxx
python md_preview.py
```

Token 不需要任何额外权限，使用 fine-grained PAT 即可（无 scope）。

### 命令行参数

| 参数 | 默认 | 说明 |
| --- | --- | --- |
| `dir` | `.` | 要监听的根目录 |
| `--host` | `127.0.0.1` | 绑定地址 |
| `--port` | `8765` | HTTP 端口 |
| `--no-browser` | 关闭 | 启动后不自动打开浏览器 |

### 路由说明

| 路由 | 用途 |
| --- | --- |
| `GET /` | 索引页，列出所有 `.md` / `.markdown` 文件 |
| `GET /view/<path>` | 文件预览页（HTML 包裹） |
| `GET /raw/<path>` | 仅返回渲染后的 HTML 片段，响应头 `X-Render-Mode: api\|local` |
| `GET /assets/<path>` | 静态资源代理（图片等） |
| `GET /events` | SSE 事件流，事件格式 `{"path": "...", "event": "modified\|created\|deleted"}` |
| `GET /files` | 当前所有 Markdown 文件路径的 JSON |

## 开发 / 测试

修改代码后跑一次手动冒烟测试：见 [`docs/smoke-test.md`](docs/smoke-test.md)。
