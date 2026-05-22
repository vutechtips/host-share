import asyncio
import contextlib
import hashlib
import json
import os
import re
import secrets
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import List, Optional

import uvicorn
from fastapi import FastAPI, File, HTTPException, Request, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, Response


USER_ID_PATTERN = re.compile(r"^[0-9a-fA-F]{32}$")
ALIAS_PATTERN = re.compile(r"^[A-Za-z0-9_-]{4,64}$")
MAX_FILE_SIZE = 500 * 1024 * 1024  # 500MB limit
RETENTION_DAYS = int(os.getenv("RETENTION_DAYS", "7"))
CLEANUP_INTERVAL_SECONDS = int(os.getenv("CLEANUP_INTERVAL_SECONDS", "3600"))
STORAGE_ROOT = Path(os.getenv("STORAGE_ROOT", "storage")).resolve()
FAVICON_PATH = Path(__file__).resolve().parent / "favicon.ico"

app = FastAPI(title="Self-host File Sharing")
cleanup_task: Optional[asyncio.Task] = None

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


def ensure_storage_root() -> None:
    STORAGE_ROOT.mkdir(parents=True, exist_ok=True)


def normalize_user_ref(user_ref: str) -> tuple[str, str | None]:
    """
    Accept either a 32-char hex id or an alias (4-64 chars, a-zA-Z0-9_-).
    Returns (hashed_id, alias_or_none).
    """
    user_ref = (user_ref or "").strip()
    if user_ref.startswith("newuser="):
        user_ref = user_ref.split("=", 1)[1]
    if USER_ID_PATTERN.fullmatch(user_ref):
        return user_ref.lower(), None
    if not ALIAS_PATTERN.fullmatch(user_ref):
        raise HTTPException(status_code=400, detail="Invalid user ID or alias")
    alias = user_ref
    hashed = hashlib.sha256(alias.encode("utf-8")).hexdigest()[:32]
    return hashed, alias


def get_user_dir(user_id: str, *, create: bool = False) -> Path:
    ensure_storage_root()
    user_dir = STORAGE_ROOT / user_id
    if create:
        user_dir.mkdir(parents=True, exist_ok=True)
    return user_dir


def safe_filename(filename: str) -> str:
    clean_name = Path(filename or "").name
    if not clean_name:
        raise HTTPException(status_code=400, detail="Missing filename")
    return clean_name


def resolve_file_path(user_id: str, filename: str, *, create_user_dir: bool = False) -> Path:
    user_dir = get_user_dir(user_id, create=create_user_dir)
    # Clean and normalize the filename path, rejecting any path traversal
    filename = (filename or "").strip().replace("\\", "/")
    parts = [p for p in filename.split("/") if p and p not in {".", ".."}]
    if not parts:
        raise HTTPException(status_code=400, detail="Missing filename")
    
    path = (user_dir / "/".join(parts)).resolve()
    if user_dir not in path.parents and user_dir != path:
        raise HTTPException(status_code=400, detail="Invalid path")
    if create_user_dir:
        path.parent.mkdir(parents=True, exist_ok=True)
    return path


def maybe_pretty_json(data: dict, pretty: bool) -> Response:
    if pretty:
        return Response(
            content=json.dumps(data, indent=2, ensure_ascii=False),
            media_type="application/json",
        )
    return JSONResponse(data)


FAVICON_SVG = """<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 64 64">
  <defs>
    <linearGradient id="g" x1="0%" y1="0%" x2="100%" y2="100%">
      <stop offset="0%" stop-color="#4fd1c5"/>
      <stop offset="100%" stop-color="#8b5cf6"/>
    </linearGradient>
  </defs>
  <rect width="64" height="64" rx="12" fill="#0f1624"/>
  <path d="M18 26h20l8 8v14H18z" fill="url(#g)" opacity="0.9"/>
  <path d="M26 16h14l8 8v8H34a8 8 0 0 1-8-8z" fill="#4fd1c5" opacity="0.9"/>
  <circle cx="28" cy="40" r="3" fill="#0f1624"/>
  <circle cx="40" cy="40" r="3" fill="#0f1624"/>
</svg>"""


def cleanup_old_files(now: datetime | None = None) -> List[str]:
    """Delete files older than RETENTION_DAYS. Returns list of deleted paths."""
    ensure_storage_root()
    now = now or datetime.now(timezone.utc)
    cutoff = now - timedelta(days=RETENTION_DAYS)
    removed: List[str] = []
    for user_dir in STORAGE_ROOT.iterdir():
        if not user_dir.is_dir():
            continue
        # Use rglob to scan recursively
        for file_path in list(user_dir.rglob("*")):
            if not file_path.is_file():
                continue
            try:
                mtime = datetime.fromtimestamp(file_path.stat().st_mtime, tz=timezone.utc)
            except OSError:
                continue
            if mtime < cutoff:
                try:
                    file_path.unlink()
                    removed.append(str(file_path))
                except OSError:
                    continue
        # Clean up empty subdirectories recursively
        for sub_dir in sorted(list(user_dir.rglob("*")), key=lambda p: len(str(p)), reverse=True):
            if sub_dir.is_dir():
                try:
                    sub_dir.rmdir()
                except OSError:
                    continue
    return removed


async def periodic_cleanup() -> None:
    while True:
        cleanup_old_files()
        await asyncio.sleep(CLEANUP_INTERVAL_SECONDS)


@app.on_event("startup")
async def startup_event() -> None:
    global cleanup_task
    ensure_storage_root()
    cleanup_task = asyncio.create_task(periodic_cleanup())


@app.on_event("shutdown")
async def shutdown_event() -> None:
    global cleanup_task
    if cleanup_task:
        cleanup_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await cleanup_task


@app.get("/", response_class=HTMLResponse)
async def root() -> str:
    return FRONTEND_HTML_V2


@app.get("/favicon.ico")
async def favicon() -> Response:
    if FAVICON_PATH.exists():
        return FileResponse(FAVICON_PATH, media_type="image/x-icon")
    return Response(content=FAVICON_SVG, media_type="image/svg+xml")


@app.post("/api/user/new")
async def create_user() -> JSONResponse:
    user_id = secrets.token_hex(16)
    return JSONResponse({"user_id": user_id})


@app.post("/api/upload/{user_id}")
async def upload_file(user_id: str, request: Request, file: UploadFile = File(...)) -> JSONResponse:
    user_id, _ = normalize_user_ref(user_id)
    content_length = request.headers.get("content-length")
    if content_length:
        try:
            if int(content_length) > MAX_FILE_SIZE + 1024:
                raise HTTPException(status_code=413, detail="File too large")
        except ValueError:
            raise HTTPException(status_code=400, detail="Invalid content-length")

    # Ưu tiên đọc đường dẫn tương đối từ header X-File-Path (để giữ cấu trúc thư mục)
    # Starlette/FastAPI hay cắt mất phần thư mục trong file.filename khi parse multipart
    relative_path = request.headers.get("x-file-path") or file.filename or ""
    dest_path = resolve_file_path(user_id, relative_path, create_user_dir=True)
    size = 0
    try:
        with dest_path.open("wb") as buffer:
            while True:
                chunk = await file.read(1024 * 1024)
                if not chunk:
                    break
                size += len(chunk)
                if size > MAX_FILE_SIZE:
                    dest_path.unlink(missing_ok=True)
                    raise HTTPException(status_code=413, detail="File too large")
                buffer.write(chunk)
    except HTTPException:
        raise
    except Exception as exc:
        dest_path.unlink(missing_ok=True)
        raise HTTPException(status_code=500, detail="Failed to save file") from exc
    return JSONResponse({"filename": dest_path.name, "size": size})


@app.get("/api/download/{user_id}/{filename:path}")
async def download_file(user_id: str, filename: str) -> FileResponse:
    user_id, _ = normalize_user_ref(user_id)
    path = resolve_file_path(user_id, filename)
    if not path.is_file():
        raise HTTPException(status_code=404, detail="File not found")
    return FileResponse(path, filename=path.name, media_type="application/octet-stream")


@app.get("/api/files/{user_id}")
async def list_files(user_id: str, request: Request, pretty: bool = True) -> Response:
    user_id, _ = normalize_user_ref(user_id)
    user_dir = get_user_dir(user_id)
    if not user_dir.exists():
        return maybe_pretty_json({"files": []}, pretty)
    files = []
    # Use rglob to scan recursively
    for file_path in user_dir.rglob("*"):
        if not file_path.is_file():
            continue
        rel_name = str(file_path.relative_to(user_dir)).replace("\\", "/")
        url = str(
            request.url_for(
                "download_file",
                user_id=user_id,
                filename=rel_name,
            )
        )
        files.append(
            {
                "name": rel_name,
                "url": url,
            }
        )
    files.sort(key=lambda f: f["name"])
    data = {"files": files}
    return maybe_pretty_json(data, pretty)


@app.delete("/api/delete/{user_id}/{filename:path}")
async def delete_file(user_id: str, filename: str) -> JSONResponse:
    user_id, _ = normalize_user_ref(user_id)
    path = resolve_file_path(user_id, filename)
    if not path.exists():
        raise HTTPException(status_code=404, detail="File not found")
    try:
        path.unlink()
        # Clean up empty parent directories up to user_dir
        user_dir = get_user_dir(user_id)
        parent = path.parent
        while parent != user_dir and parent.exists():
            if not any(parent.iterdir()):
                parent.rmdir()
                parent = parent.parent
            else:
                break
    except OSError as exc:
        raise HTTPException(status_code=500, detail="Unable to delete file") from exc
    return JSONResponse({"deleted": filename})


@app.delete("/api/clear/{user_id}")
async def clear_files(user_id: str) -> JSONResponse:
    import shutil
    user_id, _ = normalize_user_ref(user_id)
    user_dir = get_user_dir(user_id)
    if not user_dir.exists():
        return JSONResponse({"deleted": 0})
    deleted = 0
    for item in list(user_dir.iterdir()):
        try:
            if item.is_dir():
                shutil.rmtree(item)
                deleted += 1
            else:
                item.unlink()
                deleted += 1
        except OSError:
            continue
    return JSONResponse({"deleted": deleted})


@app.get("/api/download-zip/{user_id}")
async def download_zip(user_id: str) -> Response:
    """Nén toàn bộ file của user thành ZIP và trả về để tải xuống."""
    import io
    import zipfile as zf
    user_id, _ = normalize_user_ref(user_id)
    user_dir = get_user_dir(user_id)
    if not user_dir.exists():
        raise HTTPException(status_code=404, detail="No files found")
    files = [p for p in user_dir.rglob("*") if p.is_file()]
    if not files:
        raise HTTPException(status_code=404, detail="No files found")
    buf = io.BytesIO()
    with zf.ZipFile(buf, mode="w", compression=zf.ZIP_DEFLATED) as z:
        for file_path in files:
            arcname = str(file_path.relative_to(user_dir)).replace("\\", "/")
            z.write(file_path, arcname)
    buf.seek(0)
    return Response(
        content=buf.read(),
        media_type="application/zip",
        headers={"Content-Disposition": f'attachment; filename="files-{user_id[:8]}.zip"'},
    )


def _zip_dir(target_dir: Path, zip_name: str) -> Response:
    """Nén toàn bộ nội dung target_dir thành ZIP trả về trực tiếp."""
    import io, zipfile as zf
    files = [p for p in target_dir.rglob("*") if p.is_file()]
    if not files:
        raise HTTPException(status_code=404, detail="No files found")
    buf = io.BytesIO()
    with zf.ZipFile(buf, mode="w", compression=zf.ZIP_DEFLATED) as z:
        for fp in files:
            arcname = str(fp.relative_to(target_dir)).replace("\\", "/")
            z.write(fp, arcname)
    buf.seek(0)
    return Response(
        content=buf.read(),
        media_type="application/zip",
        headers={"Content-Disposition": f'attachment; filename="{zip_name}.zip"'},
    )


@app.get("/get/{user_id}")
async def get_all(user_id: str) -> Response:
    """Tải toàn bộ file của user dưới dạng ZIP."""
    user_id, alias = normalize_user_ref(user_id)
    user_dir = get_user_dir(user_id)
    if not user_dir.exists():
        raise HTTPException(status_code=404, detail="Not found")
    return _zip_dir(user_dir, alias or user_id[:8])


@app.get("/get/{user_id}/{path:path}")
async def get_path(user_id: str, path: str) -> Response:
    """
    Shorthand download:
      - Nếu path là file  → tải thẳng file đó
      - Nếu path là thư mục → nén thành ZIP và tải về
    """
    user_id, _ = normalize_user_ref(user_id)
    target = resolve_file_path(user_id, path)

    if target.is_file():
        return FileResponse(target, filename=target.name, media_type="application/octet-stream")

    if target.is_dir():
        folder_name = target.name
        return _zip_dir(target, folder_name)

    raise HTTPException(status_code=404, detail="Not found")


FRONTEND_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8" />
    <meta name="viewport" content="width=device-width, initial-scale=1.0" />
    <link rel="icon" href="/favicon.ico" />
    <title>Self-host File Sharing</title>
    <style>
        :root {
            --bg: #0f1117;
            --card: #171b24;
            --accent: #4fd1c5;
            --accent-2: #8b5cf6;
            --text: #e5e7eb;
            --muted: #9ca3af;
            --danger: #f87171;
            --radius: 12px;
            --shadow: 0 14px 40px rgba(0, 0, 0, 0.35);
        }
        * { box-sizing: border-box; }
        body {
            margin: 0;
            font-family: "Segoe UI", "Inter", system-ui, -apple-system, sans-serif;
            background: radial-gradient(circle at 20% 20%, rgba(79, 209, 197, 0.08), transparent 25%),
                        radial-gradient(circle at 80% 0%, rgba(139, 92, 246, 0.12), transparent 25%),
                        var(--bg);
            color: var(--text);
            min-height: 100vh;
            display: flex;
            align-items: center;
            justify-content: center;
            padding: 30px 14px 60px;
        }
        .container {
            width: min(1100px, 100%);
            background: var(--card);
            border: 1px solid #1f2937;
            border-radius: var(--radius);
            box-shadow: var(--shadow);
            padding: 26px;
        }
        h1 {
            margin: 0 0 4px;
            font-size: 26px;
            letter-spacing: 0.3px;
        }
        p { margin: 6px 0 18px; color: var(--muted); }
        .row {
            display: grid;
            grid-template-columns: 2fr 1fr;
            gap: 18px;
        }
        .panel {
            background: linear-gradient(145deg, rgba(255,255,255,0.02), rgba(255,255,255,0));
            border: 1px solid #1f2937;
            border-radius: var(--radius);
            padding: 18px;
        }
        .panel + .panel { margin-top: 12px; }
        .label { color: var(--muted); font-size: 13px; letter-spacing: 0.3px; text-transform: uppercase; }
        .user-id {
            font-family: "JetBrains Mono", "Fira Code", ui-monospace, SFMono-Regular, monospace;
            background: #0b0d12;
            border: 1px solid #1f2937;
            padding: 12px;
            border-radius: 10px;
            display: flex;
            align-items: center;
            justify-content: space-between;
            gap: 10px;
            word-break: break-all;
        }
        button {
            background: linear-gradient(120deg, var(--accent), var(--accent-2));
            border: none;
            color: #0f1117;
            font-weight: 700;
            padding: 10px 14px;
            border-radius: 10px;
            cursor: pointer;
            box-shadow: 0 10px 30px rgba(79, 209, 197, 0.25);
            transition: transform 120ms ease, box-shadow 120ms ease;
        }
        button.secondary {
            background: #0b0d12;
            color: var(--text);
            border: 1px solid #1f2937;
            box-shadow: none;
        }
        button:hover { transform: translateY(-1px); }
        .upload-zone {
            border: 1.5px dashed #2f3848;
            border-radius: 14px;
            padding: 24px;
            text-align: center;
            background: #0f1117;
            transition: border-color 120ms ease, background 120ms ease;
        }
        .upload-zone.drag {
            border-color: var(--accent);
            background: rgba(79, 209, 197, 0.06);
        }
        .files-table {
            width: 100%;
            border-collapse: collapse;
            margin-top: 12px;
        }
        .files-table th, .files-table td {
            padding: 10px 8px;
            text-align: left;
            border-bottom: 1px solid #1f2937;
        }
        .files-table th { color: var(--muted); font-size: 13px; letter-spacing: 0.2px; }
        .files-table td.actions {
            display: flex;
            gap: 8px;
        }
        .file-cards {
            display: none;
            gap: 10px;
            margin-top: 10px;
        }
        .file-card {
            border: 1px solid #1f2937;
            border-radius: 12px;
            padding: 12px;
            background: #0b0d12;
            display: flex;
            justify-content: space-between;
            align-items: center;
            gap: 10px;
        }
        .file-card .name { font-weight: 700; word-break: break-word; }
        .file-card .meta { color: var(--muted); font-size: 12px; margin-top: 4px; }
        .file-card .actions { display: flex; gap: 8px; flex-wrap: wrap; }
        .text-input {
            flex: 1;
            min-width: 220px;
            padding: 10px 12px;
            border-radius: 10px;
            border: 1px solid #1f2937;
            background: #0b0d12;
            color: var(--text);
            outline: none;
        }
        .text-input:focus {
            border-color: rgba(79, 209, 197, 0.55);
            box-shadow: 0 0 0 3px rgba(79, 209, 197, 0.12);
        }
        .code-box {
            background: #0b0d12;
            border: 1px solid #1f2937;
            border-radius: 10px;
            padding: 10px 12px;
            margin-top: 8px;
            font-family: "JetBrains Mono", "Fira Code", ui-monospace, SFMono-Regular, monospace;
            font-size: 13px;
            word-break: break-all;
        }
        .badge {
            background: #0b0d12;
            padding: 6px 10px;
            border-radius: 999px;
            border: 1px solid #1f2937;
            font-size: 12px;
            color: var(--muted);
        }
        .progress {
            width: 100%;
            background: #111420;
            border-radius: 10px;
            overflow: hidden;
            border: 1px solid #1f2937;
            margin-top: 10px;
            height: 10px;
        }
        .progress > div {
            height: 100%;
            width: 0%;
            background: linear-gradient(120deg, var(--accent), var(--accent-2));
            transition: width 120ms ease;
        }
        #toast {
            position: fixed;
            top: 16px;
            right: 16px;
            display: flex;
            flex-direction: column;
            gap: 8px;
            z-index: 10;
        }
        .toast {
            background: #0b0d12;
            border: 1px solid #1f2937;
            padding: 12px 14px;
            border-radius: 10px;
            box-shadow: var(--shadow);
            display: flex;
            align-items: center;
            gap: 10px;
            min-width: 240px;
        }
        .toast.danger { border-color: rgba(248, 113, 113, 0.6); color: #fecdd3; }
        .muted { color: var(--muted); }
        .small { font-size: 13px; }
        @media (max-width: 900px) {
            .row { grid-template-columns: 1fr; }
        }
        @media (max-width: 720px) {
            body { padding: 18px 12px 40px; }
            .container { padding: 18px; }
            .files-table { display: none !important; }
            .file-cards { display: grid !important; }
            .user-id { flex-direction: column; align-items: flex-start; }
        }
    </style>
</head>
<body>
    <div id="toast"></div>
    <div class="container">
        <h1>Self-host File Sharing</h1>
        <div class="panel">
            <div class="label">User ID</div>
            <div class="user-id">
                <span id="user-id-value">--</span>
                <div style="display:flex; gap:8px; flex-wrap: wrap;">
                    <button class="secondary" onclick="copyUserId()">Copy</button>
                    <button onclick="createNewUser()">Tạo user mới</button>
                </div>
            </div>
            <div style="display:flex; gap:8px; flex-wrap: wrap; margin-top: 10px;">
                <input id="user-id-input" class="text-input" placeholder="Nhập user ID (32 ký tự hex)..." />
                <button class="secondary" onclick="applyUserId()">Dùng ID này</button>
            </div>
        </div>

        <div class="row" style="margin-top: 14px;">
            <div class="panel">
                <div class="label">Upload</div>
                <div class="upload-zone" id="upload-zone">
                    <p class="small muted">Kéo & thả file hoặc chọn file để upload (tối đa 500MB).</p>
                    <input id="file-input" type="file" multiple style="margin-top: 12px;" />
                    <div class="progress" aria-label="upload-progress">
                        <div id="progress-bar"></div>
                    </div>
                </div>
            </div>
            <div class="panel">
                <div class="label">Thông tin</div>
                <div class="badge">File cũ tự xóa sau <span id="retention-days"></span> ngày</div>
                <p class="small muted" style="margin-top:10px;">Dữ liệu chỉ nằm trên máy chủ của bạn.</p>
            </div>
        </div>

        <div class="panel" style="margin-top: 18px;">
            <div class="label">Danh sách file</div>
            <div id="files-empty" class="muted small" style="margin-top: 6px;">Chưa có file nào.</div>
            <table class="files-table" id="files-table" style="display:none;">
                <thead>
                    <tr>
                        <th>Tên file</th>
                        <th>Kích thước</th>
                        <th>Cập nhật</th>
                        <th>Hành động</th>
                    </tr>
                </thead>
                <tbody id="files-body"></tbody>
            </table>
        </div>
    </div>

    <script>
        const apiBase = "/api";
        const progressBar = document.getElementById("progress-bar");
        const fileInput = document.getElementById("file-input");
        const uploadZone = document.getElementById("upload-zone");
        const userIdEl = document.getElementById("user-id-value");
        const userIdInput = document.getElementById("user-id-input");
        const retentionEl = document.getElementById("retention-days");
        const filesEmptyEl = document.getElementById("files-empty");
        const filesTable = document.getElementById("files-table");
        const filesBody = document.getElementById("files-body");

        retentionEl.textContent = `${parseInt({retention_days})} `;

        function toast(message, isError = false) {
            const el = document.createElement("div");
            el.className = `toast ${isError ? "danger" : ""}`;
            el.textContent = message;
            document.getElementById("toast").appendChild(el);
            setTimeout(() => el.remove(), 3200);
        }

        function setUserId(id) {
            const normalized = (id || "").trim().toLowerCase();
            localStorage.setItem("user_id", normalized);
            userIdEl.textContent = normalized || "--";
            if (userIdInput) userIdInput.value = normalized;
            listFiles();
        }

        function applyUserId() {
            const raw = (userIdInput?.value || "").trim();
            if (!raw) return toast("Nhập user ID trước đã", true);
            if (!/^[0-9a-fA-F]{32}$/.test(raw)) return toast("User ID phải là 32 ký tự hex", true);
            setUserId(raw);
            toast("Đã đổi user ID");
        }

        async function createNewUser() {
            try {
                const res = await fetch(`${apiBase}/user/new`, { method: "POST" });
                const data = await res.json();
                if (!data.user_id) throw new Error("Không thể tạo user");
                setUserId(data.user_id);
                toast("Tạo user mới thành công");
            } catch (err) {
                toast(err.message, true);
            }
        }

        function copyUserId() {
            const id = userIdEl.textContent;
            navigator.clipboard.writeText(id).then(() => toast("Đã copy ID"));
        }

        function formatSize(bytes) {
            if (bytes >= 1e9) return (bytes / 1e9).toFixed(2) + " GB";
            if (bytes >= 1e6) return (bytes / 1e6).toFixed(2) + " MB";
            if (bytes >= 1e3) return (bytes / 1e3).toFixed(1) + " KB";
            return bytes + " B";
        }

        async function listFiles() {
            const userId = localStorage.getItem("user_id");
            if (!userId) return;
            try {
                const res = await fetch(`${apiBase}/files/${userId}`);
                const data = await res.json();
                const files = data.files || [];
                filesBody.innerHTML = "";
                if (files.length === 0) {
                    filesTable.style.display = "none";
                    filesEmptyEl.style.display = "block";
                    return;
                }
                filesEmptyEl.style.display = "none";
                filesTable.style.display = "table";
                for (const f of files) {
                    const tr = document.createElement("tr");
                    tr.innerHTML = `
                        <td>${f.name}</td>
                        <td>${formatSize(f.size)}</td>
                        <td class="muted small">${new Date(f.modified).toLocaleString()}</td>
                        <td class="actions">
                            <button class="secondary" onclick="downloadFile('${f.name}')">Tải</button>
                            <button class="secondary" onclick="deleteFile('${f.name}')">Xóa</button>
                        </td>`;
                    filesBody.appendChild(tr);
                }
            } catch (err) {
                toast("Không thể tải danh sách file", true);
            }
        }

        function uploadFiles(files) {
            const userId = localStorage.getItem("user_id");
            if (!userId) return toast("Chưa có user ID", true);
            if (!files || files.length === 0) return;
            let uploaded = 0;
            const total = files.length;
            const uploadNext = () => {
                const file = files[uploaded];
                const xhr = new XMLHttpRequest();
                xhr.open("POST", `${apiBase}/upload/${userId}`);
                const form = new FormData();
                form.append("file", file);
                xhr.upload.onprogress = (evt) => {
                    if (evt.lengthComputable) {
                        const percent = Math.min(100, (evt.loaded / evt.total) * 100);
                        progressBar.style.width = percent + "%";
                    }
                };
                xhr.onload = () => {
                    progressBar.style.width = "0%";
                    if (xhr.status >= 200 && xhr.status < 300) {
                        toast(`Đã upload: ${file.name}`);
                        uploaded++;
                        listFiles();
                        if (uploaded < total) uploadNext();
                    } else {
                        toast(xhr.responseText || "Upload lỗi", true);
                    }
                };
                xhr.onerror = () => {
                    progressBar.style.width = "0%";
                    toast("Kết nối lỗi", true);
                };
                xhr.send(form);
            };
            uploadNext();
        }

        function downloadFile(name) {
            const userId = localStorage.getItem("user_id");
            window.location.href = `${apiBase}/download/${userId}/${encodeURIComponent(name)}`;
        }

        async function deleteFile(name) {
            const userId = localStorage.getItem("user_id");
            const ok = confirm(`Xóa file "${name}"?`);
            if (!ok) return;
            try {
                const res = await fetch(`${apiBase}/delete/${userId}/${encodeURIComponent(name)}`, { method: "DELETE" });
                if (!res.ok) throw new Error("Xóa thất bại");
                toast(`Đã xóa: ${name}`);
                listFiles();
            } catch (err) {
                toast(err.message, true);
            }
        }

        // Drag & drop
        uploadZone.addEventListener("dragover", (e) => { e.preventDefault(); uploadZone.classList.add("drag"); });
        uploadZone.addEventListener("dragleave", () => uploadZone.classList.remove("drag"));
        uploadZone.addEventListener("drop", (e) => {
            e.preventDefault();
            uploadZone.classList.remove("drag");
            uploadFiles(e.dataTransfer.files);
        });
        fileInput.addEventListener("change", (e) => uploadFiles(e.target.files));

        // Init
        (async () => {
            let id = localStorage.getItem("user_id");
            if (userIdInput) {
                userIdInput.addEventListener("keydown", (e) => {
                    if (e.key === "Enter") {
                        e.preventDefault();
                        applyUserId();
                    }
                });
                if (id) userIdInput.value = id;
            }
            if (!id) await createNewUser();
            else setUserId(id);
        })();
    </script>
</body>
</html>
""".replace("{retention_days}", str(RETENTION_DAYS))

# Cleaner UI (ASCII text to avoid mojibake)
FRONTEND_HTML_V2 = """<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1.0" />
  <link rel="icon" href="/favicon.ico" />
  <meta http-equiv="Cache-Control" content="no-store" />
  <title>Self-host File Sharing</title>
  <style>
    :root {
      --bg: #0d1117;
      --card: #0f1624;
      --panel: #121c2d;
      --text: #e7edf7;
      --muted: #93a4c0;
      --accent: #4fd1c5;
      --accent-2: #8b5cf6;
      --danger: #f87171;
      --border: rgba(255,255,255,0.08);
      --radius: 14px;
      --shadow: 0 18px 50px rgba(0,0,0,0.35);
    }
    * { box-sizing: border-box; }
    body {
      margin: 0;
      font-family: "Segoe UI", "Inter", system-ui, -apple-system, sans-serif;
      background:
        radial-gradient(circle at 15% 20%, rgba(79,209,197,0.12), transparent 30%),
        radial-gradient(circle at 75% 10%, rgba(139,92,246,0.15), transparent 32%),
        var(--bg);
      color: var(--text);
      min-height: 100vh;
      padding: 28px 14px 60px;
    }
    .container { max-width: 1080px; margin: 0 auto; display: grid; gap: 14px; }
    .card {
      background: linear-gradient(145deg, rgba(255,255,255,0.02), rgba(255,255,255,0));
      border: 1px solid var(--border);
      border-radius: var(--radius);
      padding: 18px 20px;
      box-shadow: var(--shadow);
    }
    h1 { margin: 0 0 6px; letter-spacing: -0.4px; }
    p { margin: 6px 0 0; color: var(--muted); }
    .row { display: grid; grid-template-columns: 2fr 1fr; gap: 12px; }
    @media (max-width: 900px) { .row { grid-template-columns: 1fr; } }
    .label { font-size: 13px; color: var(--muted); letter-spacing: 0.3px; text-transform: uppercase; }
    .user-box {
      margin-top: 10px;
      padding: 12px;
      border: 1px solid var(--border);
      background: #0a1020;
      border-radius: 12px;
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 10px;
      word-break: break-all;
      font-family: "JetBrains Mono", "Fira Code", ui-monospace, SFMono-Regular, monospace;
    }
    button {
      background: linear-gradient(120deg, var(--accent), var(--accent-2));
      color: #0b0f1b;
      border: none;
      border-radius: 10px;
      padding: 10px 14px;
      font-weight: 700;
      cursor: pointer;
      box-shadow: 0 10px 30px rgba(79,209,197,0.25);
      transition: transform 120ms ease, box-shadow 120ms ease;
    }
    button.secondary {
      background: #0f1624;
      color: var(--text);
      border: 1px solid var(--border);
      box-shadow: none;
    }
    button.small { padding: 8px 10px; font-size: 13px; }
    button:hover { transform: translateY(-1px); }
    .pill {
      display: inline-flex;
      gap: 8px;
      align-items: center;
      padding: 8px 12px;
      border-radius: 999px;
      border: 1px solid var(--border);
      background: rgba(255,255,255,0.04);
      color: var(--muted);
    }
    .upload {
      border: 1.3px dashed var(--border);
      border-radius: var(--radius);
      padding: 20px;
      background: #0c1322;
      text-align: center;
      transition: border-color 120ms ease, background 120ms ease;
    }
    .upload.drag { border-color: var(--accent); background: rgba(79,209,197,0.08); }
    .progress {
      width: 100%;
      height: 10px;
      border-radius: 999px;
      background: rgba(255,255,255,0.06);
      border: 1px solid var(--border);
      overflow: hidden;
      margin-top: 12px;
    }
    .progress > div {
      height: 100%;
      width: 0%;
      background: linear-gradient(120deg, var(--accent), var(--accent-2));
      transition: width 120ms ease;
    }
    table { width: 100%; border-collapse: collapse; margin-top: 10px; }
    th, td { padding: 10px 8px; border-bottom: 1px solid var(--border); text-align: left; }
    th { color: var(--muted); font-size: 13px; letter-spacing: 0.3px; }
    td.actions { display: flex; gap: 8px; }
    .muted { color: var(--muted); }
    .small { font-size: 13px; }
    .file-cards { display: none; gap: 10px; margin-top: 10px; }
    .file-card {
      border: 1px solid var(--border);
      border-radius: 12px;
      padding: 10px 12px;
      background: #0c1322;
      display: flex;
      justify-content: space-between;
      align-items: center;
      gap: 10px;
    }
    .file-card .meta { color: var(--muted); font-size: 12px; }
    .file-card .actions { display: flex; gap: 8px; }
    .code-row {
      display: flex;
      gap: 10px;
      align-items: center;
      margin-top: 10px;
      flex-wrap: wrap;
    }
    .code-box {
      flex: 1;
      background: #0b1322;
      border: 1px solid var(--border);
      border-radius: 10px;
      padding: 10px 12px;
      font-family: "JetBrains Mono", "Fira Code", ui-monospace, SFMono-Regular, monospace;
      font-size: 13px;
      color: var(--text);
      word-break: break-all;
    }
    .code-label { display: block; color: var(--muted); font-size: 12px; margin-bottom: 6px; }
    .input {
      flex: 1;
      min-width: 220px;
      padding: 10px 12px;
      border-radius: 10px;
      border: 1px solid var(--border);
      background: #0c1322;
      color: var(--text);
      outline: none;
    }
    .input:focus {
      border-color: rgba(79, 209, 197, 0.55);
      box-shadow: 0 0 0 3px rgba(79, 209, 197, 0.12);
    }
    #toast {
      position: fixed;
      top: 14px;
      right: 14px;
      display: flex;
      flex-direction: column;
      gap: 8px;
      z-index: 50;
    }
    .toast {
      background: #0b0f1b;
      border: 1px solid var(--border);
      padding: 12px 14px;
      border-radius: 10px;
      box-shadow: var(--shadow);
      min-width: 240px;
    }
    .toast.danger { border-color: rgba(248,113,113,0.6); color: #fecdd3; }
    @media (max-width: 720px) {
      body { padding: 16px; }
      .card { padding: 14px 14px; }
      .row { grid-template-columns: 1fr; }
      table.files-table { display: none; }
      .file-cards { display: grid; }
      button { width: auto; }
      .user-box { flex-direction: column; align-items: flex-start; }
    }
  </style>
</head>
<body>
  <div id="toast"></div>
  <div class="container">
    <div class="card">
      <h1>Self-host File Sharing</h1>
      <p>Nhanh, không database, mỗi user có thư mục riêng.</p>
      <div class="user-box">
        <span id="user-id-value">--</span>
        <div style="display:flex; gap:8px; flex-wrap: wrap;">
          <button class="secondary small" onclick="copyUserId()">Copy</button>
          <button class="small" onclick="createNewUser()">Tạo user mới</button>
        </div>
      </div>
      <div style="display:flex; gap:8px; flex-wrap: wrap; margin-top: 10px;">
        <input id="user-input" class="input" placeholder="Nhập user ID (≥ 4 ký tự)..." />
        <button class="secondary small" id="set-user-btn">Dùng ID này</button>
      </div>
      <div style="display:flex; gap:8px; flex-wrap: wrap; margin-top: 8px;">
        <span class="pill">Retention: <strong id="retention-days">{retention_days}</strong> days</span>
        <span class="pill">Limit: 500MB</span>
      </div>
    </div>

    <div class="row">
      <div class="card">
        <div class="label">Upload</div>
        <div class="upload" id="upload-zone">
          <p class="muted small">Kéo thả file/thư mục hoặc chọn bên dưới (tối đa 500MB).</p>
          <div style="display: flex; gap: 8px; justify-content: center; margin-top: 12px; flex-wrap: wrap;">
            <button class="secondary small" onclick="document.getElementById('file-input').click()">Chọn file</button>
            <button class="secondary small" onclick="document.getElementById('folder-input').click()">Chọn thư mục</button>
          </div>
          <input id="file-input" type="file" multiple style="display: none;" />
          <input id="folder-input" type="file" webkitdirectory directory multiple style="display: none;" />
          <div class="progress" aria-label="upload-progress">
            <div id="progress-bar"></div>
          </div>
        </div>
      </div>
      <div class="card">
        <div class="label">CLI &mdash; Download</div>
        <div class="code-row">
          <div class="code-box">
            <span class="code-label">T\u1ea3i to\u00e0n b\u1ed9 (ZIP)</span>
            <code id="get-all">curl -OJ &lt;origin&gt;/get/&lt;user_id&gt;</code>
          </div>
          <button class="secondary small" onclick="copySnippet('get-all')">Copy</button>
        </div>
        <div class="code-row">
          <div class="code-box">
            <span class="code-label">T\u1ea3i th\u01b0 m\u1ee5c (ZIP)</span>
            <code id="get-folder">curl -OJ &lt;origin&gt;/get/&lt;user_id&gt;/&lt;folder&gt;</code>
          </div>
          <button class="secondary small" onclick="copySnippet('get-folder')">Copy</button>
        </div>
        <div class="code-row">
          <div class="code-box">
            <span class="code-label">T\u1ea3i 1 file</span>
            <code id="get-file">curl -OJ &lt;origin&gt;/get/&lt;user_id&gt;/&lt;folder&gt;/&lt;file&gt;</code>
          </div>
          <button class="secondary small" onclick="copySnippet('get-file')">Copy</button>
        </div>
        <div class="code-row">
          <div class="code-box">
            <span class="code-label">Upload file/th\u01b0 m\u1ee5c</span>
            <code id="curl-upload">curl -X POST -F "file=@/path/to/file" &lt;origin&gt;/api/upload/newuser=your_alias</code>
          </div>
          <button class="secondary small" onclick="copySnippet('curl-upload')">Copy</button>
        </div>
      </div>
    </div>

    <div class="card">
        <div style="display:flex; justify-content: space-between; align-items: center; gap: 10px;">
          <div class="label">Danh sách file</div>
          <button class="secondary small" id="delete-all-btn">Xóa tất cả</button>
        </div>
        <div id="files-empty" class="muted small" style="margin-top: 6px;">Chưa có file nào.</div>
        <table id="files-table" style="display:none;">
          <thead>
            <tr>
              <th>Ten file</th>
            <th>Actions</th>
          </tr>
        </thead>
        <tbody id="files-body"></tbody>
      </table>
      <div id="file-cards" class="file-cards"></div>
    </div>
  </div>

  <script>
    const apiBase = "/api";
    const MAX_SIZE = 500 * 1024 * 1024;
    const userIdEl = document.getElementById("user-id-value");
  const filesEmptyEl = document.getElementById("files-empty");
  const filesTable = document.getElementById("files-table");
  const filesBody = document.getElementById("files-body");
  const fileCards = document.getElementById("file-cards");
  const uploadZone = document.getElementById("upload-zone");
  const fileInput = document.getElementById("file-input");
  const progressBar = document.getElementById("progress-bar");
  const userInput = document.getElementById("user-input");
  const setUserBtn = document.getElementById("set-user-btn");
  const deleteAllBtn = document.getElementById("delete-all-btn");
  let currentUserId = null;

  function toast(message, isError = false) {
    const el = document.createElement("div");
    el.className = `toast ${isError ? "danger" : ""}`;
    el.textContent = message;
    document.getElementById("toast").appendChild(el);
    setTimeout(() => el.remove(), 3000);
  }

  function setUserId(id) {
    currentUserId = id;
    localStorage.setItem("user_id", id);
    userIdEl.textContent = id;
    if (userInput) userInput.value = id;
    updateCliSnippets();
    listFiles();
  }

  async function createNewUser() {
    try {
      const res = await fetch(`${apiBase}/user/new`, { method: "POST" });
      const data = await res.json();
      if (!data.user_id) throw new Error("Không tạo được user");
      setUserId(data.user_id);
      toast("Đã tạo user mới");
    } catch (err) {
      toast(err.message, true);
    }
  }

  function applyUserInput() {
    const raw = (userInput?.value || "").trim();
    if (!raw) return toast("Nhập user ID trước đã", true);
    if (raw.length < 4) return toast("User ID phải từ 4 ký tự trở lên", true);
    setUserId(raw);
    toast("Đã đổi user ID");
  }

    function copyUserId() {
      if (!currentUserId) return;
      navigator.clipboard.writeText(currentUserId).then(() => toast("Đã copy ID"));
    }


  async function listFiles() {
    if (!currentUserId) return;
    try {
      const res = await fetch(`${apiBase}/files/${currentUserId}`);
      const data = await res.json();
      const files = data.files || [];
      filesBody.innerHTML = "";
      fileCards.innerHTML = "";
      // reset inline display so CSS (desktop vs mobile) can control visibility
      filesTable.style.removeProperty("display");
      fileCards.style.removeProperty("display");

      if (files.length === 0) {
        filesTable.hidden = true;
        fileCards.hidden = true;
        filesEmptyEl.style.display = "block";
        return;
      }
      filesEmptyEl.style.display = "none";
      filesTable.hidden = false;
      fileCards.hidden = false;

      for (const f of files) {
        const tr = document.createElement("tr");

        const tdName = document.createElement("td");
        tdName.textContent = f.name;
        tr.appendChild(tdName);

        const tdActions = document.createElement("td");
        tdActions.className = "actions";

        const btnDownload = document.createElement("button");
        btnDownload.className = "secondary small";
        btnDownload.textContent = "Tải";
        btnDownload.addEventListener("click", () => downloadFile(f.name));

        const btnDelete = document.createElement("button");
        btnDelete.className = "secondary small";
        btnDelete.textContent = "Xóa";
        btnDelete.addEventListener("click", () => deleteFile(f.name));

        tdActions.appendChild(btnDownload);
        tdActions.appendChild(btnDelete);
        tr.appendChild(tdActions);
        filesBody.appendChild(tr);

        // Mobile card view
        const card = document.createElement("div");
        card.className = "file-card";
        
        const cardTitle = document.createElement("div");
        cardTitle.textContent = f.name;
        card.appendChild(cardTitle);

        const cardActions = document.createElement("div");
        cardActions.className = "actions";

        const btnCardDownload = document.createElement("button");
        btnCardDownload.className = "secondary small";
        btnCardDownload.textContent = "Tải";
        btnCardDownload.addEventListener("click", () => downloadFile(f.name));

        const btnCardDelete = document.createElement("button");
        btnCardDelete.className = "secondary small";
        btnCardDelete.textContent = "Xóa";
        btnCardDelete.addEventListener("click", () => deleteFile(f.name));

        cardActions.appendChild(btnCardDownload);
        cardActions.appendChild(btnCardDelete);
        card.appendChild(cardActions);
        fileCards.appendChild(card);
      }
    } catch (err) {
      toast("Không tải được danh sách file", true);
    }
  }

  function copySnippet(elId) {
    const el = document.getElementById(elId);
    if (!el) return;
    const text = el.textContent;
    navigator.clipboard.writeText(text).then(() => toast("Đã copy"));
  }

  async function deleteAllFiles() {
    if (!currentUserId) return toast("Chưa có user ID", true);
    const ok = confirm("Xóa tất cả file?");
    if (!ok) return;
    try {
      const res = await fetch(`${apiBase}/clear/${currentUserId}`, { method: "DELETE" });
      if (!res.ok) throw new Error("Xóa tất cả thất bại");
      toast("Đã xóa tất cả file");
      listFiles();
    } catch (err) {
      toast(err.message, true);
    }
  }

  async function handleDroppedItems(items) {
    // Thu thập [file, relativePath] từ các entry kéo thả
    const pairs = [];

    async function traverseEntry(entry, pathPrefix) {
      if (entry.isFile) {
        const file = await new Promise((resolve) => entry.file(resolve));
        pairs.push([file, pathPrefix + entry.name]);
      } else if (entry.isDirectory) {
        const dirReader = entry.createReader();
        // readEntries chỉ trả tối đa 100 entries mỗi lần, cần loop
        const allEntries = [];
        const readBatch = () => new Promise((resolve) => dirReader.readEntries(resolve));
        let batch;
        do {
          batch = await readBatch();
          allEntries.push(...batch);
        } while (batch.length > 0);
        for (const child of allEntries) {
          await traverseEntry(child, pathPrefix + entry.name + '/');
        }
      }
    }

    for (let i = 0; i < items.length; i++) {
      const item = items[i];
      if (item.kind === 'file') {
        const entry = item.webkitGetAsEntry();
        if (entry) await traverseEntry(entry, '');
      }
    }
    uploadPairs(pairs);
  }

  // uploadFiles: dành cho file picker thông thường (File có sẵn webkitRelativePath)
  function uploadFiles(fileList) {
    if (!currentUserId) return toast("Chưa có user ID", true);
    if (!fileList || fileList.length === 0) return;
    const pairs = Array.from(fileList).map(f => [f, f.webkitRelativePath || f.name]);
    uploadPairs(pairs);
  }

  // uploadPairs: nhân hàm upload dùng chung, nhận mảng [File, relativePath]
  function uploadPairs(pairs) {
    if (!currentUserId) return toast("Chưa có user ID", true);
    if (!pairs || pairs.length === 0) return;
    const queue = [...pairs];

    const uploadNext = () => {
      if (queue.length === 0) {
        progressBar.style.width = "0%";
        return;
      }
      const [file, relativePath] = queue.shift();

      if (file.size > MAX_SIZE) {
        toast(`${relativePath} vượt 500MB`, true);
        uploadNext();
        return;
      }

      const xhr = new XMLHttpRequest();
      xhr.open("POST", `${apiBase}/upload/${currentUserId}`);

      // Truyền đường dẫn tương đối qua header riêng
      // (FastAPI/Starlette cắt mất phần thư mục trong file.filename khi parse multipart)
      xhr.setRequestHeader("X-File-Path", relativePath);

      const form = new FormData();
      form.append("file", file, file.name); // chỉ truyền tên file, path đi qua header

      xhr.upload.onprogress = (evt) => {
        if (evt.lengthComputable) {
          progressBar.style.width = Math.min(100, (evt.loaded / evt.total) * 100) + "%";
        }
      };
      xhr.onload = () => {
        progressBar.style.width = "0%";
        if (xhr.status >= 200 && xhr.status < 300) {
          toast(`Đã upload: ${relativePath}`);
          listFiles();
          uploadNext();
        } else {
          let msg = "Upload lỗi";
          try { msg = JSON.parse(xhr.responseText).detail || msg; } catch (_) {}
          toast(msg, true);
        }
      };
      xhr.onerror = () => {
        progressBar.style.width = "0%";
        toast("Kết nối lỗi", true);
      };
      xhr.send(form);
    };

    uploadNext();
  }


  function downloadFile(name) {
    if (!currentUserId) return;
    const safePath = name.split('/').map(encodeURIComponent).join('/');
    window.location.href = `${apiBase}/download/${currentUserId}/${safePath}`;
  }

  async function deleteFile(name) {
    if (!currentUserId) return;
    const ok = confirm(`Xóa file "${name}"?`);
    if (!ok) return;
    try {
      const safePath = name.split('/').map(encodeURIComponent).join('/');
      const res = await fetch(`${apiBase}/delete/${currentUserId}/${safePath}`, { method: "DELETE" });
      if (!res.ok) throw new Error("Xóa thất bại");
      toast(`Đã xóa: ${name}`);
      listFiles();
    } catch (err) {
      toast(err.message, true);
    }
  }

  function updateCliSnippets() {
    const origin = window.location.origin;
    const aliasSample = currentUserId || "your_alias";
    const user = currentUserId || "<user_id>";

    const uploadEl   = document.getElementById("curl-upload");
    const getAllEl   = document.getElementById("get-all");
    const getFolderEl = document.getElementById("get-folder");
    const getFileEl  = document.getElementById("get-file");

    if (uploadEl)    uploadEl.textContent    = 'curl -X POST -F "file=@/path/to/file" ' + origin + '/api/upload/newuser=' + aliasSample;
    if (getAllEl)    getAllEl.textContent    = 'curl -OJ ' + origin + '/get/' + user;
    if (getFolderEl) getFolderEl.textContent = 'curl -OJ ' + origin + '/get/' + user + '/<folder>';
    if (getFileEl)  getFileEl.textContent   = 'curl -OJ ' + origin + '/get/' + user + '/<folder>/<file>';
  }


  uploadZone.addEventListener("dragover", (e) => { e.preventDefault(); uploadZone.classList.add("drag"); });
  uploadZone.addEventListener("dragleave", () => uploadZone.classList.remove("drag"));
  uploadZone.addEventListener("drop", (e) => {
    e.preventDefault();
    uploadZone.classList.remove("drag");
    if (e.dataTransfer.items) {
      handleDroppedItems(e.dataTransfer.items);
    } else {
      uploadFiles(e.dataTransfer.files);
    }
  });
  fileInput.addEventListener("change", (e) => uploadFiles(e.target.files));
  const folderInput = document.getElementById("folder-input");
  if (folderInput) {
    folderInput.addEventListener("change", (e) => uploadFiles(e.target.files));
  }

  (async () => {
    if (setUserBtn) setUserBtn.addEventListener("click", applyUserInput);
    if (deleteAllBtn) deleteAllBtn.addEventListener("click", deleteAllFiles);
    if (userInput) {
      userInput.addEventListener("keydown", (e) => {
        if (e.key === "Enter") {
          e.preventDefault();
          applyUserInput();
        }
      });
    }
    updateCliSnippets();
    let id = localStorage.getItem("user_id");
    if (!id) {
      await createNewUser();
    } else {
        setUserId(id);
      }
    })();
  </script>
</body>
</html>
""".replace("{retention_days}", str(RETENTION_DAYS))


if __name__ == "__main__":
    ensure_storage_root()
    uvicorn.run("app:app", host="0.0.0.0", port=int(os.getenv("PORT", "8000")), reload=False)
