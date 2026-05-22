import asyncio
import hashlib
import json
import os
import re
import secrets
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import List

from fastapi import FastAPI, File, HTTPException, Request, UploadFile, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, HTMLResponse, PlainTextResponse, Response
from pydantic import BaseModel


BASE_DIR = Path(__file__).parent.resolve()
STORAGE_DIR = BASE_DIR / "storage"
STORAGE_DIR.mkdir(parents=True, exist_ok=True)

USER_ID_RE = re.compile(r"^[0-9a-fA-F]{32}$")
MAX_FILE_SIZE = 500 * 1024 * 1024  # 500 MB


def _parse_retention_days() -> int:
    raw = os.getenv("RETENTION_DAYS", "7")
    try:
        value = int(raw)
        return max(value, 0)
    except ValueError:
        return 7


RETENTION_DAYS = _parse_retention_days()
CLEANUP_INTERVAL_SECONDS = int(os.getenv("CLEANUP_INTERVAL_SECONDS", str(6 * 60 * 60)))
_cleanup_task: asyncio.Task | None = None


class NewUserRequest(BaseModel):
    user_id: str | None = None


def hash_alias(alias: str) -> str:
    digest = hashlib.sha256(alias.encode("utf-8")).hexdigest()
    return digest[:32]


def validate_alias(alias: str) -> str:
    alias = alias.strip()
    if not (4 <= len(alias) <= 32):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Alias length must be 4-32 characters",
        )
    if not re.fullmatch(r"[A-Za-z0-9_-]+", alias):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Alias must be alphanumeric plus _ or -",
        )
    return alias


def normalize_user_ref(user_ref: str) -> tuple[str, str]:
    """
    Accept either a hashed user id or an alias and return (hashed_id, display_alias).
    If a hash is provided, the display alias is the hash itself.
    """
    user_ref = user_ref.strip()
    if USER_ID_RE.fullmatch(user_ref):
        lowered = user_ref.lower()
        return lowered, lowered
    alias = validate_alias(user_ref)
    return hash_alias(alias), alias


def ensure_user_dir_from_ref(user_ref: str) -> tuple[Path, str, str]:
    user_id, alias = normalize_user_ref(user_ref)
    user_dir = STORAGE_DIR / user_id
    user_dir.mkdir(parents=True, exist_ok=True)
    return user_dir, user_id, alias

app = FastAPI(title="Self-hosted File Share", version="1.0.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


def validate_user_id(user_id: str) -> str:
    if not USER_ID_RE.fullmatch(user_id):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid user id format"
        )
    return user_id.lower()


def ensure_user_dir(user_id: str) -> Path:
    safe_id = validate_user_id(user_id)
    user_dir = STORAGE_DIR / safe_id
    user_dir.mkdir(parents=True, exist_ok=True)
    return user_dir


def sanitize_filename(filename: str) -> str:
    name = Path(filename).name
    if not name or name in {".", ".."}:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid filename"
        )
    return name


def maybe_pretty_json(data: dict, pretty: bool) -> Response:
    if pretty:
        return Response(
            content=json.dumps(data, indent=2, ensure_ascii=False),
            media_type="application/json",
        )
    return Response(content=json.dumps(data, ensure_ascii=False), media_type="application/json")


def cleanup_old_files() -> None:
    if RETENTION_DAYS <= 0:
        return
    cutoff = datetime.now(timezone.utc) - timedelta(days=RETENTION_DAYS)
    for user_dir in STORAGE_DIR.iterdir():
        if not user_dir.is_dir():
            continue
        for file_path in user_dir.iterdir():
            if not file_path.is_file():
                continue
            try:
                mtime = datetime.fromtimestamp(
                    file_path.stat().st_mtime, tz=timezone.utc
                )
            except FileNotFoundError:
                continue
            if mtime < cutoff:
                try:
                    file_path.unlink(missing_ok=True)
                except OSError:
                    continue
        try:
            next(user_dir.iterdir())
        except StopIteration:
            try:
                user_dir.rmdir()
            except OSError:
                continue


async def _cleanup_loop() -> None:
    while True:
        cleanup_old_files()
        await asyncio.sleep(max(CLEANUP_INTERVAL_SECONDS, 60))


@app.on_event("startup")
async def on_startup() -> None:
    cleanup_old_files()
    global _cleanup_task
    _cleanup_task = asyncio.create_task(_cleanup_loop())


@app.on_event("shutdown")
async def on_shutdown() -> None:
    global _cleanup_task
    if _cleanup_task:
        _cleanup_task.cancel()


@app.post("/api/user/new")
async def create_user(body: NewUserRequest | None = None, pretty: bool = True) -> Response:
    cleanup_old_files()
    display_id = None
    if body and body.user_id:
        display_id = validate_alias(body.user_id)
        user_id = hash_alias(display_id)
    else:
        user_id = secrets.token_hex(16)
        display_id = user_id
    ensure_user_dir(user_id)
    return maybe_pretty_json({"user_id": user_id, "display_id": display_id}, pretty)


@app.post("/api/upload/{user_id}")
async def upload_file(
    user_id: str,
    request: Request,
    file: UploadFile = File(...),
    pretty: bool = True,
) -> Response:
    cleanup_old_files()

    is_newuser_flow = False
    alias: str | None = None
    created = False
    already_existed = False

    if user_id.lower().startswith("newuser"):
        is_newuser_flow = True
        alias_raw = user_id[len("newuser") :].lstrip("=")
        if alias_raw:
            alias = validate_alias(alias_raw)
        else:
            alias = secrets.token_hex(4)
        hashed = hash_alias(alias)
        user_id_effective = hashed
        user_dir = STORAGE_DIR / user_id_effective
        if user_dir.exists():
            already_existed = True
        else:
            created = True
        user_dir.mkdir(parents=True, exist_ok=True)
    else:
        user_dir, user_id_effective, alias = ensure_user_dir_from_ref(user_id)

    filename = sanitize_filename(file.filename or "")
    destination = user_dir / filename

    link_user_param = alias if alias and alias != user_id_effective else user_id_effective

    size = 0
    try:
        with destination.open("wb") as buffer:
            while True:
                chunk = await file.read(1024 * 1024 * 4)
                if not chunk:
                    break
                size += len(chunk)
                if size > MAX_FILE_SIZE:
                    buffer.close()
                    destination.unlink(missing_ok=True)
                    raise HTTPException(
                        status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
                        detail="File too large (limit 500MB)",
                    )
                buffer.write(chunk)
    finally:
        await file.close()

    base = str(request.base_url).rstrip("/")
    download_url = f"{base}/api/download/{link_user_param}/{filename}"
    list_url = f"{base}/api/files/{link_user_param}"
    dashboard_url = f"{base}/"

    payload = {
        "alias": alias,
        "user_id": user_id_effective,
        "created": created,
        "already_existed": already_existed,
        "filename": filename,
        "size": size,
        "download_url": download_url,
        "list_url": list_url,
        "dashboard": dashboard_url,
        "message": (
            "Alias already exists; uploaded to same user"
            if already_existed
            else "User created and file uploaded" if created else "File uploaded"
        ),
    }
    return maybe_pretty_json(payload, pretty)


@app.get("/api/download/{user_id}/{filename}")
async def download_file(user_id: str, filename: str):
    cleanup_old_files()
    user_dir, _, _ = ensure_user_dir_from_ref(user_id)
    name = sanitize_filename(filename)
    file_path = user_dir / name
    if not file_path.exists():
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Not found")
    return FileResponse(file_path, filename=name)


@app.get("/api/files/{user_id}")
async def list_files(user_id: str, pretty: bool = True) -> Response:
    cleanup_old_files()
    user_dir, _, _ = ensure_user_dir_from_ref(user_id)
    files: List[dict] = []
    for item in sorted(user_dir.iterdir()):
        if not item.is_file():
            continue
        files.append(
            {
                "name": item.name,
            }
        )
    data = {"files": files}
    return maybe_pretty_json(data, pretty)


@app.delete("/api/delete/{user_id}/{filename}")
async def delete_file(user_id: str, filename: str, pretty: bool = True) -> Response:
    cleanup_old_files()
    user_dir, _, _ = ensure_user_dir_from_ref(user_id)
    name = sanitize_filename(filename)
    file_path = user_dir / name
    if not file_path.exists():
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Not found")
    try:
        file_path.unlink()
    except FileNotFoundError:
        pass
    return maybe_pretty_json({"deleted": name}, pretty)


@app.delete("/api/clear/{user_id}")
async def clear_user_files(user_id: str, pretty: bool = True) -> Response:
    cleanup_old_files()
    user_dir, _, _ = ensure_user_dir_from_ref(user_id)
    deleted = 0
    for item in list(user_dir.iterdir()):
        if item.is_file():
            try:
                item.unlink()
                deleted += 1
            except OSError:
                continue
    return maybe_pretty_json({"deleted": deleted}, pretty)


@app.get("/newuser", response_class=PlainTextResponse)
async def new_user_cli(request: Request, alias: str | None = None) -> str:
    cleanup_old_files()
    if alias:
        display_id = validate_alias(alias)
        user_id = hash_alias(display_id)
    else:
        display_id = secrets.token_hex(4)
        user_id = hash_alias(display_id)
    ensure_user_dir(user_id)

    base = str(request.base_url).rstrip("/")
    upload_cmd = f'curl -X POST -F "file=@/path/to/file" {base}/api/upload/{user_id}'
    list_cmd = f"curl {base}/api/files/{user_id}"
    download_cmd = f"curl -O {base}/api/download/{user_id}/<filename>"
    delete_cmd = f"curl -X DELETE {base}/api/delete/{user_id}/<filename>"
    clear_cmd = f"curl -X DELETE {base}/api/clear/{user_id}"

    lines = [
        f"user_alias: {display_id}",
        f"user_hash: {user_id}",
        "",
        "Upload:",
        f"  {upload_cmd}",
        "List files:",
        f"  {list_cmd}",
        "Download one:",
        f"  {download_cmd}",
        "Delete one:",
        f"  {delete_cmd}",
        "Clear all:",
        f"  {clear_cmd}",
    ]
    return "\n".join(lines)


HTML_PAGE = """
<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1.0" />
  <meta http-equiv="Cache-Control" content="no-store" />
  <title>Self-host File Share</title>
  <style>
    :root {
      --bg: #090b11;
      --panel: #0f131b;
      --muted: #9aa3b5;
      --text: #eef1f7;
      --accent: #7f6bff;
      --accent-2: #6fe7c2;
      --danger: #ff6b7a;
      --border: rgba(255,255,255,0.08);
      --shadow: 0 24px 80px rgba(0,0,0,0.45);
      --radius: 16px;
    }
    * { box-sizing: border-box; }
    body {
      margin: 0;
      font-family: "Inter", "Segoe UI", Arial, sans-serif;
      background:
        linear-gradient(135deg, rgba(127,107,255,0.15), transparent 30%),
        linear-gradient(225deg, rgba(111,231,194,0.1), transparent 40%),
        radial-gradient(circle at 15% 20%, rgba(127,107,255,0.12), transparent 45%),
        var(--bg);
      color: var(--text);
      min-height: 100vh;
      padding: 28px;
    }
    .app {
      max-width: 1100px;
      margin: 0 auto;
      display: grid;
      gap: 16px;
    }
    .card {
      background: linear-gradient(140deg, rgba(255,255,255,0.03), rgba(255,255,255,0));
      border: 1px solid var(--border);
      border-radius: var(--radius);
      padding: 18px 20px;
      box-shadow: var(--shadow);
      backdrop-filter: blur(8px);
    }
    h1 {
      margin: 0 0 8px;
      font-size: 28px;
      letter-spacing: -0.3px;
    }
    h2 {
      margin: 0;
      letter-spacing: -0.2px;
    }
    .row {
      display: flex;
      align-items: center;
      gap: 12px;
      flex-wrap: wrap;
    }
    .pill {
      background: rgba(255,255,255,0.04);
      border: 1px solid var(--border);
      border-radius: 12px;
      padding: 9px 12px;
      font-weight: 700;
      letter-spacing: 0.2px;
    }
    .pill .label { color: var(--muted); margin-right: 8px; font-weight: 500; }
    button {
      border: 1px solid var(--border);
      background: linear-gradient(120deg, rgba(127,107,255,0.15), rgba(111,231,194,0.15));
      color: var(--text);
      border-radius: 12px;
      padding: 10px 14px;
      cursor: pointer;
      font-weight: 700;
      letter-spacing: 0.2px;
      transition: transform 0.12s ease, border-color 0.12s ease, background 0.2s ease;
    }
    button:hover { transform: translateY(-1px); border-color: var(--accent); }
    button:active { transform: translateY(0); }
    .ghost-btn {
      background: rgba(255,255,255,0.02);
      border-color: var(--border);
    }
    #drop-zone {
      border: 1.4px dashed var(--border);
      border-radius: var(--radius);
      padding: 30px;
      text-align: center;
      background: linear-gradient(160deg, rgba(255,255,255,0.03), rgba(255,255,255,0.01));
      transition: border-color 0.15s ease, box-shadow 0.15s ease, transform 0.15s ease;
    }
    #drop-zone.dragover {
      border-color: var(--accent);
      box-shadow: 0 10px 40px rgba(127,107,255,0.22);
      transform: translateY(-2px);
    }
    .hint { color: var(--muted); font-size: 14px; margin-top: 6px; }
    .actions { display: flex; gap: 10px; justify-content: center; margin-top: 12px; flex-wrap: wrap; }
    .progress {
      margin-top: 14px;
      height: 12px;
      border-radius: 999px;
      background: rgba(255,255,255,0.05);
      overflow: hidden;
      border: 1px solid var(--border);
      display: none;
    }
    .progress.active { display: block; }
    .progress-bar {
      height: 100%;
      width: 0%;
      background: linear-gradient(120deg, var(--accent), var(--accent-2));
      color: #0b0d12;
      font-size: 11px;
      font-weight: 700;
      display: flex;
      align-items: center;
      justify-content: center;
      transition: width 0.15s ease;
    }
    table {
      width: 100%;
      border-collapse: collapse;
      margin-top: 8px;
    }
    th, td {
      padding: 12px 10px;
      text-align: left;
      border-bottom: 1px solid var(--border);
    }
    th { color: var(--muted); font-weight: 600; font-size: 13px; letter-spacing: 0.3px; }
    tr:hover td { background: rgba(255,255,255,0.02); }
    .badge {
      display: inline-flex;
      align-items: center;
      gap: 6px;
      padding: 6px 10px;
      border-radius: 10px;
      background: rgba(127,107,255,0.14);
      color: var(--text);
      border: 1px solid rgba(127,107,255,0.4);
      font-weight: 600;
      font-size: 13px;
    }
    .muted { color: var(--muted); }
    #toast-container {
      position: fixed;
      top: 18px;
      right: 18px;
      display: grid;
      gap: 10px;
      z-index: 9999;
    }
    .toast {
      background: rgba(15,19,27,0.95);
      border: 1px solid var(--border);
      padding: 12px 14px;
      border-radius: 12px;
      box-shadow: var(--shadow);
      min-width: 240px;
      animation: fadeIn 0.18s ease;
    }
    .toast.success { border-color: rgba(111,231,194,0.7); }
    .toast.error { border-color: rgba(255,107,122,0.7); }
    @keyframes fadeIn { from { opacity: 0; transform: translateY(-4px); } to { opacity: 1; transform: translateY(0); } }
    a { color: var(--accent); text-decoration: none; font-weight: 600; }
    a:hover { text-decoration: underline; }
    .tagline { color: var(--muted); margin: 0; }
    .file-actions { display: flex; gap: 8px; align-items: center; }
    .text-button {
      background: transparent;
      border: none;
      color: var(--accent);
      padding: 0;
      cursor: pointer;
      font-weight: 700;
    }
    .text-button.danger { color: var(--danger); }
    .input {
      background: rgba(255,255,255,0.04);
      border: 1px solid var(--border);
      border-radius: 12px;
      padding: 10px 12px;
      color: var(--text);
      min-width: 260px;
      font-size: 14px;
    }
    .input:focus { outline: 1px solid var(--accent); }
    .subtext {
      display: block;
      color: var(--muted);
      font-size: 12px;
      margin-top: 4px;
      letter-spacing: 0.2px;
    }
    .code-block {
      background: rgba(255,255,255,0.03);
      border: 1px solid var(--border);
      border-radius: 12px;
      padding: 12px;
      font-family: "JetBrains Mono", "SFMono-Regular", Consolas, monospace;
      color: var(--text);
      overflow-x: auto;
      font-size: 13px;
      white-space: pre-wrap;
    }
    @media (max-width: 640px) {
      body { padding: 16px; }
    }
  </style>
</head>
<body>
  <div class="app">
    <div class="card">
      <h1>Self-host File Share</h1>
      <p class="tagline">Minimal fast file drop with per-user isolation.</p>
      <div class="row" style="margin-top: 12px;">
        <div class="pill">
          <span class="label">User ID</span>
          <span id="user-id-display">-</span>
          <span class="subtext" id="user-id">hash: -</span>
        </div>
        <button id="copy-btn" class="ghost-btn">Copy ID</button>
        <button id="new-user-btn">New user</button>
        <div class="badge">Auto cleanup: <span id="retention">{retention_days}</span> days</div>
        <div class="badge">Limit: 500MB</div>
      </div>
      <div class="row" style="margin-top: 10px;">
        <input id="user-input" class="input" placeholder="Alias 4-32 chars (a-z0-9_-)" maxlength="32" />
        <button id="set-user-btn" class="ghost-btn">Use this ID</button>
      </div>
    </div>

    <div class="card">
      <div class="row" style="justify-content: space-between;">
        <div>
          <h2 style="margin: 0;">CLI Upload</h2>
          <p class="tagline" style="margin-top: 6px;">Copy ready-to-run curl for Linux/macOS.</p>
        </div>
        <button id="copy-curl-btn">Copy curl upload</button>
      </div>
      <div class="code-block" style="margin-top: 10px;">
        <code id="curl-snippet">curl -X POST -F "file=@/path/to/file" &lt;origin&gt;/api/upload/newuser=your_alias</code>
      </div>
      <p class="tagline" style="margin-top: 10px;">List files for current user:</p>
      <div class="code-block">
        <code id="list-snippet">curl &lt;origin&gt;/api/files/&lt;user_hash_or_alias&gt;</code>
      </div>
      <p class="tagline" style="margin-top: 10px;">Download a file (replace &lt;filename&gt;):</p>
      <div class="code-block">
        <code id="download-snippet">curl -O &lt;origin&gt;/api/download/&lt;user_hash_or_alias&gt;/&lt;filename&gt;</code>
      </div>
    </div>

    <div class="card">
      <div id="drop-zone">
        <div style="font-size: 18px; font-weight: 700;">Drop files here</div>
        <div class="hint">Drag & drop or choose files to upload</div>
        <div class="actions">
          <button id="browse-btn">Choose files</button>
          <input type="file" id="file-input" multiple style="display: none;" />
        </div>
        <div class="progress" id="progress">
          <div class="progress-bar" id="progress-bar"></div>
        </div>
      </div>
    </div>

    <div class="card">
      <div class="row" style="justify-content: space-between; margin-bottom: 6px;">
        <h2 style="margin: 0;">Files</h2>
        <div class="row" style="gap: 10px;">
          <button id="clear-btn" class="ghost-btn" style="padding: 8px 12px;">Clear all</button>
          <span class="muted" id="file-count">- files</span>
        </div>
      </div>
      <div id="file-table-wrapper">
        <table id="file-table">
          <thead>
            <tr>
              <th>Name</th>
              <th></th>
            </tr>
          </thead>
          <tbody id="file-body">
          </tbody>
        </table>
      </div>
    </div>
  </div>

  <div id="toast-container"></div>

  <script>
    const maxFileSize = 500 * 1024 * 1024;
    let userId = null; // hashed id used by API
    let displayId = null; // user-friendly alias

    function showToast(message, type = "info", timeout = 2800) {
      const container = document.getElementById("toast-container");
      const el = document.createElement("div");
      el.className = `toast ${type}`;
      el.textContent = message;
      container.appendChild(el);
      setTimeout(() => {
        el.style.opacity = "0";
        el.style.transform = "translateY(-4px)";
        setTimeout(() => container.removeChild(el), 180);
      }, timeout);
    }

    function setUser(id, alias) {
      userId = id;
      displayId = alias || id;
      localStorage.setItem("fs_user_id", id);
      document.getElementById("user-id").textContent = `hash: ${id}`;
      document.getElementById("user-id-display").textContent = displayId;
      const input = document.getElementById("user-input");
      if (input) input.value = displayId;
      updateCurlSnippet();
      fetchFiles();
    }

    async function createUser(customId) {
      const payload = customId ? { user_id: customId } : {};
      const res = await fetch("/api/user/new", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(payload),
      });
      if (!res.ok) {
        let msg = "Cannot create user";
        try { msg = (await res.json()).detail || msg; } catch {}
        showToast(msg, "error");
        return;
      }
      const data = await res.json();
      setUser(data.user_id, data.display_id);
      showToast(customId ? "User ID set" : "New user created", "success");
    }

    async function initUser() {
      const saved = localStorage.getItem("fs_user_id");
      if (saved && /^[0-9a-f]{32}$/i.test(saved)) {
        setUser(saved, saved);
      } else {
        await createUser();
      }
    }

    function setProgress(percent, label) {
      const bar = document.getElementById("progress-bar");
      const wrapper = document.getElementById("progress");
      if (percent > 0) {
        wrapper.classList.add("active");
        bar.style.width = percent + "%";
        bar.textContent = label ? `${label} (${percent}%)` : percent + "%";
      } else {
        wrapper.classList.remove("active");
        bar.style.width = "0%";
        bar.textContent = "";
      }
    }

    function handleFiles(files) {
      if (!userId) {
        showToast("Create a user first", "error");
        return;
      }
      const list = Array.from(files);
      if (!list.length) return;
      const uploadNext = () => {
        const file = list.shift();
        if (!file) {
          setProgress(0);
          return;
        }
        if (file.size > maxFileSize) {
          showToast(`${file.name} exceeds 500MB`, "error");
          uploadNext();
          return;
        }
        uploadSingle(file).finally(uploadNext);
      };
      uploadNext();
    }

    function uploadSingle(file) {
      return new Promise((resolve) => {
        const xhr = new XMLHttpRequest();
        xhr.open("POST", `/api/upload/${userId}`);
        xhr.upload.onprogress = (event) => {
          if (event.lengthComputable) {
            const percent = Math.round((event.loaded / event.total) * 100);
            setProgress(percent, file.name);
          }
        };
        xhr.onload = () => {
          setProgress(0);
          if (xhr.status >= 200 && xhr.status < 300) {
            showToast(`Uploaded ${file.name}`, "success");
            fetchFiles();
          } else {
            let msg = xhr.responseText || "Upload failed";
            try {
              msg = JSON.parse(xhr.responseText).detail || msg;
            } catch (e) {}
            showToast(msg, "error");
          }
          resolve();
        };
        xhr.onerror = () => {
          setProgress(0);
          showToast("Network error during upload", "error");
          resolve();
        };
        const form = new FormData();
        form.append("file", file);
        xhr.send(form);
      });
    }

    async function fetchFiles() {
      if (!userId) return;
      const res = await fetch(`/api/files/${userId}`);
      if (!res.ok) {
        showToast("Cannot fetch files", "error");
        return;
      }
      const data = await res.json();
      renderFiles(data.files || []);
    }

    function renderFiles(files) {
      const body = document.getElementById("file-body");
      body.innerHTML = "";
      document.getElementById("file-count").textContent = `${files.length} file(s)`;
      if (!files.length) {
        const row = document.createElement("tr");
        const cell = document.createElement("td");
        cell.colSpan = 2;
        cell.className = "muted";
        cell.textContent = "No files yet";
        row.appendChild(cell);
        body.appendChild(row);
        return;
      }
      files.forEach((file) => {
        const tr = document.createElement("tr");
        const nameTd = document.createElement("td");
        nameTd.textContent = file.name;
        const actionTd = document.createElement("td");
        actionTd.className = "file-actions";

        const downloadLink = document.createElement("a");
        downloadLink.href = file.url || `/api/download/${userId}/${encodeURIComponent(file.name)}`;
        downloadLink.textContent = "Download";
        downloadLink.className = "text-button";

        const copyBtn = document.createElement("button");
        copyBtn.className = "text-button";
        copyBtn.textContent = "Copy link";
        copyBtn.onclick = () => copyLink(file);

        const deleteBtn = document.createElement("button");
        deleteBtn.className = "text-button danger";
        deleteBtn.textContent = "Delete";
        deleteBtn.onclick = () => deleteFile(file.name);

        actionTd.appendChild(downloadLink);
        actionTd.appendChild(copyBtn);
        actionTd.appendChild(deleteBtn);

        tr.appendChild(nameTd);
        tr.appendChild(actionTd);
        body.appendChild(tr);
      });
    }

    async function copyLink(file) {
      if (!file?.name) {
        showToast("No link", "error");
        return;
      }
      const url = file.url || `${window.location.origin}/api/download/${userId}/${encodeURIComponent(file.name)}`;
      try {
        await navigator.clipboard.writeText(url);
        showToast("Link copied", "success");
      } catch (e) {
        showToast("Clipboard not available", "error");
      }
    }

    async function deleteFile(name) {
      if (!confirm(`Delete ${name}?`)) return;
      const res = await fetch(`/api/delete/${userId}/${encodeURIComponent(name)}`, { method: "DELETE" });
      if (res.ok) {
        showToast(`Deleted ${name}`, "success");
        fetchFiles();
      } else {
        showToast("Delete failed", "error");
      }
    }

    async function clearAll() {
      if (!userId) return;
      if (!confirm("Clear all files for this user?")) return;
      const res = await fetch(`/api/clear/${userId}`, { method: "DELETE" });
      if (res.ok) {
        showToast("All files cleared", "success");
        fetchFiles();
      } else {
        showToast("Clear failed", "error");
      }
    }

    function formatDateHuman(human, iso) {
      if (human) return human;
      const d = new Date(iso);
      if (isNaN(d.getTime())) return "-";
      return d.toLocaleString();
    }

    document.getElementById("copy-btn").addEventListener("click", async () => {
      if (!userId) return;
      try {
        await navigator.clipboard.writeText(userId);
        showToast("Copied", "success");
      } catch (e) {
        showToast("Clipboard not available", "error");
      }
    });

    document.getElementById("new-user-btn").addEventListener("click", () => {
      createUser();
    });
    document.getElementById("set-user-btn").addEventListener("click", () => {
      const val = document.getElementById("user-input").value.trim();
      if (val.length < 4 || val.length > 32) {
        showToast("ID length 4-32 chars", "error");
        return;
      }
      if (!/^[A-Za-z0-9_-]+$/.test(val)) {
        showToast("Only letters, numbers, _ or -", "error");
        return;
      }
      createUser(val);
    });
    document.getElementById("clear-btn").addEventListener("click", clearAll);

    document.getElementById("copy-curl-btn").addEventListener("click", async () => {
      if (!userId) {
        showToast("Create a user first", "error");
        return;
      }
      const snippet = buildCurlSnippet();
      try {
        await navigator.clipboard.writeText(snippet);
        showToast("curl copied", "success");
      } catch (e) {
        showToast("Clipboard not available", "error");
      }
    });

    const fileInput = document.getElementById("file-input");
    document.getElementById("browse-btn").addEventListener("click", () => fileInput.click());
    fileInput.addEventListener("change", (e) => handleFiles(e.target.files));

    const dropZone = document.getElementById("drop-zone");
    dropZone.addEventListener("dragover", (e) => {
      e.preventDefault();
      dropZone.classList.add("dragover");
    });
    dropZone.addEventListener("dragleave", () => dropZone.classList.remove("dragover"));
    dropZone.addEventListener("drop", (e) => {
      e.preventDefault();
      dropZone.classList.remove("dragover");
      handleFiles(e.dataTransfer.files);
    });

    function buildCurlSnippet() {
      const origin = window.location.origin;
      const aliasSample = displayId || "your_alias";
      return `curl -X POST -F \"file=@/path/to/file\" ${origin}/api/upload/newuser=${aliasSample}`;
    }

    function updateCurlSnippet() {
      const origin = window.location.origin;
      const idForSnippet = displayId || userId || "<user_hash_or_alias>";
      const el = document.getElementById("curl-snippet");
      if (el) {
        el.textContent = buildCurlSnippet();
      }
      const listEl = document.getElementById("list-snippet");
      if (listEl) {
        listEl.textContent = `curl ${origin}/api/files/${idForSnippet}`;
      }
      const dlEl = document.getElementById("download-snippet");
      if (dlEl) {
        dlEl.textContent = `curl -O ${origin}/api/download/${idForSnippet}/<filename>`;
      }
    }

    updateCurlSnippet();
    initUser();
  </script>
</body>
</html>
""".replace("{retention_days}", str(RETENTION_DAYS))


@app.get("/", response_class=HTMLResponse)
async def root() -> HTMLResponse:
    return HTMLResponse(content=HTML_PAGE)


if __name__ == "__main__":
    import uvicorn

    uvicorn.run("main:app", host="0.0.0.0", port=int(os.getenv("PORT", "8000")), reload=True)
