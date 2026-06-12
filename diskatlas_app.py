#!/usr/bin/env python3
"""
DiskAtlas Desktop v0.7 — "Connected Papers for your disk"

v0.7 changes — SECOND PRO FEATURE + THE DEMO MOMENT
  * NEW  Δ WHAT GREW? — every finished scan is saved as a snapshot
         (~/.diskatlas/snapshots). Rescanning the same folder diffs
         against it: bubbles get ▲ growth badges (orange) / ▼ shrink
         badges (green) / NEW tags, the status bar reports the total
         drift ("Δ +12.4 GB in 9d"), and the amber toolbar button opens
         a ranked growers list with growth bars. Ancestors whose growth
         is explained by a child are suppressed so the list names
         CAUSES, not containers.
  * NEW  Reclaim finale: the drive-free counter animates live from
         before -> after with an ease-out count-up — the screenshot moment.

v0.6 changes — FIRST PRO FEATURE
  * NEW  ✨ RECLAIM — the green button. Measures every guaranteed-safe
         location (Windows temp, Update leftovers, Delivery Optimization,
         thumbnail cache, crash dumps, Windows.old, Edge/Chrome page
         caches, the Recycle Bin itself via SHEmptyRecycleBinW) in a
         background thread, shows an itemized checkbox review with live
         per-row sizes and a running total, then frees everything you
         left checked in one click. Locked/in-use files are skipped
         silently and counted; per-row "freed X" feedback; drive-free
         indicator refreshes. Nothing user-created is ever in the bucket.

v0.5 changes:
  * NEW  FILE NODES — expanding a folder now also reveals its files as a
         distinct node type: document-shaped glyphs (paper w/ folded corner)
         colored by kind (code/text/image/media/archive/doc). Files are
         enumerated lazily on expand, so huge trees stay light. The biggest
         files show first; the rest group into "+N more files" documents
         that keep expanding.
  * NEW  CLICK A FILE -> built-in VIEWER window: text & code open with
         live search highlighting, PNG/GIF render inline (JPEG/WebP too if
         Pillow is installed), everything else gets a clean hex dump.
         Open / Show in folder / Copy path buttons included. Esc closes.

v0.4 changes:
  * FIX  grouped "+N smaller items" bubbles can no longer be deleted
         (they pointed at the PARENT path — deleting one deleted everything)
  * FIX  Windows junctions / reparse points are now skipped while scanning
         (no more double counting AppData legacy junctions)
  * FIX  sizes propagate up the graph after a delete; drive-free refreshes
  * NEW  progressive scanning — root bubble appears instantly, children pop
         in as they finish, live "files · GB" counter while scanning
  * NEW  auto-focus: expanding a node glides + zooms the camera onto the
         newly opened cluster; children spread at a larger distance
  * NEW  permanent delete now shows the same safe-score warning as recycle
  * NEW  separate real cleanup *command* per detector (copy button pastes
         something runnable, hints stay human prose)
  * PERF canvas only redraws when something actually changed

A force-directed GRAPH of storage entities (not a folder list):
  * bubbles sized by GB, colored by what they ARE (cache / env / model / vm-disk...)
  * green halo = safe to delete, red halo = dangerous
  * click a bubble  -> expand / collapse its children (physics animates them out)
  * drag a bubble   -> move it; drag empty space -> pan; mouse wheel -> zoom
  * right-click     -> real OS actions:
        Delete -> Recycle Bin   shell32.SHFileOperationW (FO_DELETE|FOF_ALLOWUNDO)
        Show in Explorer        explorer /select,"path"
        Open                    ShellExecuteW (os.startfile)
        Drive free space        kernel32.GetDiskFreeSpaceExW
  * side panel shows the selected entity: size, safe score, reclaim command

Single file, zero dependencies. Build the exe:  double-click build_exe.bat
Run from source:                                python diskatlas_app.py
"""

from __future__ import annotations

import ctypes
import hashlib
import json
import math
import os
import queue
import random
import shutil
import subprocess
import sys
import threading
import time
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, messagebox, ttk

IS_WIN = sys.platform.startswith("win")
GB, MB = 1024 ** 3, 1024 ** 2

FILE_ATTRIBUTE_REPARSE_POINT = 0x400


def is_reparse(entry) -> bool:
    """True for symlinks AND Windows junctions / reparse points.

    Python's is_symlink() misses NTFS junctions, which caused double
    counting (e.g. the legacy 'Application Data' junction in user homes).
    """
    try:
        if entry.is_symlink():
            return True
        if IS_WIN:
            st = entry.stat(follow_symlinks=False)
            return bool(getattr(st, "st_file_attributes", 0) & FILE_ATTRIBUTE_REPARSE_POINT)
    except OSError:
        return True          # unreadable -> safest to skip
    return False


def node_radius(size: int, is_file: bool = False) -> float:
    """Bubble radius from byte size — shared by GNode + live resizing."""
    if is_file:
        return max(9.0, min(34.0, 4.5 * math.sqrt(max(size, 1) / MB) ** 0.5 * 3))
    return max(12.0, min(64.0, 5.5 * math.sqrt(max(size, 1) / MB) ** 0.5 * 3))


# ---- file-kind classification (for the document-shaped file nodes) ----

FILE_KINDS = {
    "code":    {".py", ".js", ".ts", ".jsx", ".tsx", ".json", ".yaml", ".yml", ".toml",
                ".html", ".css", ".c", ".cpp", ".h", ".hpp", ".java", ".rs", ".go",
                ".sh", ".bat", ".ps1", ".sql", ".ipynb", ".rb", ".php", ".lua"},
    "text":    {".txt", ".md", ".rst", ".log", ".csv", ".tsv", ".ini", ".cfg",
                ".conf", ".xml", ".env"},
    "image":   {".png", ".jpg", ".jpeg", ".gif", ".bmp", ".webp", ".svg", ".ico", ".tiff"},
    "media":   {".mp4", ".mkv", ".avi", ".mov", ".webm", ".mp3", ".wav", ".flac",
                ".m4a", ".ogg", ".opus"},
    "archive": {".zip", ".7z", ".rar", ".tar", ".gz", ".xz", ".bz2", ".iso", ".img"},
    "doc":     {".pdf", ".docx", ".doc", ".xlsx", ".xls", ".pptx", ".ppt", ".odt", ".epub"},
}
FILE_KIND_COLORS = {
    "code": "#5e93c9", "text": "#8da7ae", "image": "#b06fa8", "media": "#3f9ea0",
    "archive": "#96915a", "doc": "#a8584e", "binary": "#6b7f88",
}


def file_kind(name: str) -> str:
    ext = os.path.splitext(name)[1].lower()
    for kind, exts in FILE_KINDS.items():
        if ext in exts:
            return kind
    return "binary"

# ===========================================================================
# 1. SEMANTIC DETECTORS
# ===========================================================================

CACHE_NAMES = {
    # name: (label, safe_score, human hint, runnable command or None)
    "pip": ("pip download cache", 98, "re-downloads packages on demand", "pip cache purge"),
    "uv": ("uv package cache", 98, "re-downloads packages on demand", "uv cache clean"),
    "huggingface": ("Hugging Face hub cache", 98, "models re-download on demand", "huggingface-cli delete-cache"),
    "whisper": ("Whisper model cache", 98, "re-downloads on first use", None),
    "torch": ("PyTorch hub cache", 96, "re-downloads on first use", None),
    "npm-cache": ("npm cache", 95, "rebuilt automatically", "npm cache clean --force"),
    "yarn": ("Yarn cache", 95, "rebuilt automatically", "yarn cache clean"),
    "ms-playwright": ("Playwright browsers", 95, "re-fetched by npx playwright install", None),
    "puppeteer": ("Puppeteer browsers", 95, "re-downloaded on install", None),
    "thumbnails": ("Thumbnail cache", 99, "regenerated automatically", None),
}


def detect(path: Path, is_dir: bool, size_hint: int = 0):
    """Return (category, label, safe_score, hint, prune, cmd) or None."""
    name = path.name
    n = name.lower()
    if not is_dir:
        if n.endswith((".vhdx", ".vhd")):
            return ("vm-disk", f"Virtual disk {name}", 10,
                    "WSL/Hyper-V disk — clean inside, then wsl --shutdown + Optimize-VHD. Never delete directly.",
                    True, None)
        if size_hint > 200 * MB and n.endswith((".safetensors", ".ckpt", ".pt", ".pth", ".gguf", ".onnx", ".bin")):
            return ("models", f"{name}", 45,
                    "Model weights. Re-downloadable if from a hub; KEEP if it's your fine-tune.", True, None)
        if size_hint > 500 * MB and n.endswith((".iso", ".img")):
            return ("archives", f"{name}", 60, "usually a re-downloadable installer image", True, None)
        return None
    parent = path.parent.name.lower()
    if parent in (".cache", "cache", "caches") and n in CACHE_NAMES:
        d, s, h, cm = CACHE_NAMES[n]
        return ("cache", d, s, h, True, cm)
    if n == ".cache":
        return ("cache", "User caches", 90, "almost entirely regenerable — click to expand", False, None)
    if n in ("__pycache__", ".pytest_cache", ".mypy_cache", ".ruff_cache"):
        return ("cache", f"{name}", 99, "tooling cache, regenerated automatically", True, None)
    if n == "node_modules":
        return ("environment", f"node_modules ({path.parent.name})", 92,
                "npm/yarn install regenerates from lockfile", True, None)
    if (path / "conda-meta").is_dir():
        if (path / "envs").is_dir():
            return ("environment", f"Conda: {name}", 25,
                    "clean caches; remove unused envs individually", False, "conda clean --all -y")
        return ("environment", f"conda env: {name}", 40,
                "export first: conda env export", True, f"conda env remove -n {name}")
    if n == "pkgs" and (path.parent / "conda-meta").is_dir():
        return ("cache", "conda pkgs cache", 95, "package cache, safe to clear", True, "conda clean --all -y")
    if (path / "pyvenv.cfg").exists():
        return ("environment", f"venv: {name} ({path.parent.name})", 85,
                "recreate any time: python -m venv + pip install -r requirements.txt", True, None)
    if n == "wsl" and parent == "local":
        return ("vm-disk", "WSL distributions", 15,
                "click to expand — clean inside Linux first, then compact the .vhdx", False, None)
    if n in ("outputs", "checkpoints", "runs", "wandb", "lightning_logs", "mlruns"):
        return ("artifacts", f"{name} ({path.parent.name})", 70,
                "training outputs — keep final checkpoints, drop intermediate runs", True, None)
    if (path / ".git").exists():
        return ("project", f"{name}", 20,
                "git project — source is usually on a remote; heavy parts are data/venvs inside", False, None)
    if n == ".rustup":
        return ("environment", "Rust toolchains", 60, "remove old toolchains you no longer use", True,
                "rustup toolchain list")
    if n == ".npm":
        return ("cache", "npm cache (~/.npm)", 95, "rebuilt automatically", True, "npm cache clean --force")
    if n in (".gradle", ".m2"):
        return ("cache", f"{name} build cache", 85, "re-downloaded on next build", True, None)
    if n == ".vscode-server":
        return ("environment", "VS Code Remote server", 75, "reinstalls on next remote connect", True, None)
    return None


# ===========================================================================
# 2. WINDOWS API LAYER (real DLL calls via ctypes)
# ===========================================================================

def win_recycle(paths: list[str]) -> bool:
    """shell32.SHFileOperationW — move to Recycle Bin (undoable)."""
    if not IS_WIN:
        trash = Path.home() / ".local/share/Trash/files"
        trash.mkdir(parents=True, exist_ok=True)
        try:
            for p in paths:
                shutil.move(p, trash / Path(p).name)
            return True
        except OSError:
            return False

    class SHFILEOPSTRUCTW(ctypes.Structure):
        _fields_ = [("hwnd", ctypes.c_void_p), ("wFunc", ctypes.c_uint),
                    ("pFrom", ctypes.c_wchar_p), ("pTo", ctypes.c_wchar_p),
                    ("fFlags", ctypes.c_int), ("fAnyOperationsAborted", ctypes.c_bool),
                    ("hNameMappings", ctypes.c_void_p), ("lpszProgressTitle", ctypes.c_wchar_p)]
    FO_DELETE, FOF_ALLOWUNDO, FOF_NOCONFIRMATION = 3, 0x40, 0x10
    src = "\0".join(paths) + "\0\0"
    op = SHFILEOPSTRUCTW(None, FO_DELETE, src, None,
                         FOF_ALLOWUNDO | FOF_NOCONFIRMATION, False, None, None)
    return ctypes.windll.shell32.SHFileOperationW(ctypes.byref(op)) == 0


def win_reveal(path: str):
    if IS_WIN:
        subprocess.Popen(f'explorer /select,"{path}"')
    elif sys.platform == "darwin":
        subprocess.Popen(["open", "-R", path])
    else:
        subprocess.Popen(["xdg-open", str(Path(path).parent)])


def win_open(path: str):
    if IS_WIN:
        os.startfile(path)  # ShellExecuteW underneath
    elif sys.platform == "darwin":
        subprocess.Popen(["open", path])
    else:
        subprocess.Popen(["xdg-open", path])


def drive_free(path: str):
    """kernel32.GetDiskFreeSpaceExW."""
    if IS_WIN:
        free, total = ctypes.c_ulonglong(0), ctypes.c_ulonglong(0)
        ctypes.windll.kernel32.GetDiskFreeSpaceExW(
            ctypes.c_wchar_p(path), ctypes.byref(free), ctypes.byref(total), None)
        return free.value, total.value
    st = os.statvfs(path)
    return st.f_bavail * st.f_frsize, st.f_blocks * st.f_frsize


# ---------------------------------------------------------------- Recycle Bin

class _SHQUERYRBINFO(ctypes.Structure):
    _fields_ = [("cbSize", ctypes.c_ulong if ctypes.sizeof(ctypes.c_void_p) == 4
                 else ctypes.c_ulonglong),
                ("i64Size", ctypes.c_longlong),
                ("i64NumItems", ctypes.c_longlong)]


def recycle_bin_size() -> tuple[int, int]:
    """shell32.SHQueryRecycleBinW -> (bytes, item count)."""
    if not IS_WIN:
        return 0, 0
    info = _SHQUERYRBINFO()
    info.cbSize = ctypes.sizeof(info)
    res = ctypes.windll.shell32.SHQueryRecycleBinW(None, ctypes.byref(info))
    if res != 0:
        return 0, 0
    return int(info.i64Size), int(info.i64NumItems)


def empty_recycle_bin() -> bool:
    """shell32.SHEmptyRecycleBinW (no confirm / no progress UI / no sound)."""
    if not IS_WIN:
        return False
    SHERB = 0x1 | 0x2 | 0x4
    return ctypes.windll.shell32.SHEmptyRecycleBinW(None, None, SHERB) == 0


# ===========================================================================
# 2b. ✨ RECLAIM ENGINE (Pro) — the guaranteed-safe cleanup bucket
#     Every target here is regenerated automatically by Windows or the app
#     that owns it. Nothing user-created is ever touched.
# ===========================================================================

def _t(tid, label, desc, paths, kind="contents", admin=False):
    return {"id": tid, "label": label, "desc": desc,
            "paths": [Path(p) for p in paths if p],
            "kind": kind,          # contents | whole | recycle_bin
            "admin": admin, "size": None, "checked": True}


def build_reclaim_targets() -> list[dict]:
    home = Path.home()
    t: list[dict] = []
    if IS_WIN:
        local = Path(os.environ.get("LOCALAPPDATA", home / "AppData/Local"))
        windir = Path(os.environ.get("SystemRoot", r"C:\Windows"))
        sysdrive = Path(os.environ.get("SystemDrive", "C:") + "\\")
        t += [
            _t("temp", "Temporary files",
               "Windows + app temp folders — recreated constantly",
               [os.environ.get("TEMP"), windir / "Temp"]),
            _t("wu", "Windows Update leftovers",
               "downloaded update packages already installed",
               [windir / "SoftwareDistribution" / "Download"], admin=True),
            _t("do", "Delivery Optimization cache",
               "peer-to-peer update cache — Windows rebuilds it",
               [windir / "SoftwareDistribution" / "DeliveryOptimization"], admin=True),
            _t("thumbs", "Thumbnail cache",
               "Explorer regenerates thumbnails automatically",
               [local / "Microsoft" / "Windows" / "Explorer"]),
            _t("dumps", "Crash dumps & error reports",
               "diagnostic files from old crashes",
               [local / "CrashDumps", windir / "Minidump",
                local / "Microsoft" / "Windows" / "WER"]),
            _t("winold", "Windows.old (previous installation)",
               "left from a Windows upgrade — safe once you're staying",
               [sysdrive / "Windows.old"], kind="whole", admin=True),
            _t("edge", "Edge browser cache",
               "page cache only — passwords/history untouched",
               [local / "Microsoft" / "Edge" / "User Data" / "Default" / "Cache"]),
            _t("chrome", "Chrome browser cache",
               "page cache only — passwords/history untouched",
               [local / "Google" / "Chrome" / "User Data" / "Default" / "Cache"]),
            _t("bin", "Recycle Bin",
               "shell32.SHEmptyRecycleBinW — empties the bin itself",
               [], kind="recycle_bin"),
        ]
    else:                                   # dev caches keep this testable anywhere
        t += [
            _t("pipc", "pip download cache", "re-downloads on demand",
               [home / ".cache" / "pip"]),
            _t("npmc", "npm cache", "rebuilt automatically",
               [home / ".npm" / "_cacache"]),
            _t("thumbs", "Thumbnail cache", "regenerated automatically",
               [home / ".cache" / "thumbnails"]),
        ]
    # dev caches valuable on every OS
    t += [
        _t("pyc", "pip cache (user)", "re-downloads packages on demand",
           [home / "AppData/Local/pip/cache" if IS_WIN else home / ".cache/pip"]),
    ] if IS_WIN else []
    return t


def measure_target(t: dict) -> int:
    if t["kind"] == "recycle_bin":
        t["size"] = recycle_bin_size()[0]
        return t["size"]
    total = 0
    for p in t["paths"]:
        if p.exists():
            total += dir_size(p) if p.is_dir() else p.stat().st_size
    t["size"] = total
    return total


def _delete_contents(path: Path) -> tuple[int, int]:
    """Best-effort delete of a folder's contents (locked files skipped).
    Returns (bytes freed, items skipped)."""
    freed = skipped = 0
    if not path.exists():
        return 0, 0
    for dirpath, dirnames, filenames in os.walk(path, topdown=False):
        for fn in filenames:
            fp = Path(dirpath) / fn
            try:
                sz = fp.stat().st_size
                fp.unlink()
                freed += sz
            except OSError:
                skipped += 1
        for dn in dirnames:
            try:
                (Path(dirpath) / dn).rmdir()
            except OSError:
                pass
    return freed, skipped


def clean_target(t: dict) -> tuple[int, int]:
    """Execute one reclaim target. Returns (bytes freed, items skipped)."""
    if t["kind"] == "recycle_bin":
        sz = recycle_bin_size()[0]
        return (sz, 0) if empty_recycle_bin() else (0, 1)
    freed = skipped = 0
    for p in t["paths"]:
        if not p.exists():
            continue
        if t["kind"] == "whole":
            sz = dir_size(p)
            try:
                shutil.rmtree(p)
                freed += sz
            except OSError:
                skipped += 1
        else:
            f, s = _delete_contents(p)
            freed += f
            skipped += s
    return freed, skipped


# ===========================================================================
# 3. SCANNER — builds a nested dict tree in a background thread
# ===========================================================================

def fmt(b: int) -> str:
    if b >= GB:
        return f"{b/GB:.2f} GB"
    if b >= MB:
        return f"{b/MB:.0f} MB"
    return f"{b/1024:.0f} KB"


def fmt_delta(d: int) -> str:
    return ("+" if d >= 0 else "−") + fmt(abs(d))


# ===========================================================================
# 3b. Δ SNAPSHOT ENGINE (Pro) — "what grew since last scan?"
#     Every finished scan is saved as a flat {path: size} map. The next scan
#     of the same root is diffed against it: nodes get a "delta" annotation
#     and a ranked growers list answers the eternal question.
# ===========================================================================

def snapshot_dir() -> Path:
    d = Path.home() / ".diskatlas" / "snapshots"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _snap_file(root: Path) -> Path:
    h = hashlib.sha1(str(root).encode("utf-8", "replace")).hexdigest()[:16]
    return snapshot_dir() / f"{h}.json"


def flatten_sizes(data: dict, out: dict | None = None) -> dict:
    out = out if out is not None else {}
    out[data["path"]] = data["size"]
    for c in data.get("children", []):
        flatten_sizes(c, out)
    return out


def load_snapshot(root: Path) -> dict | None:
    try:
        with open(_snap_file(root), encoding="utf-8") as f:
            snap = json.load(f)
        return snap if "sizes" in snap and "ts" in snap else None
    except (OSError, ValueError):
        return None


def save_snapshot(root: Path, data: dict):
    try:
        with open(_snap_file(root), "w", encoding="utf-8") as f:
            json.dump({"root": str(root), "ts": time.time(),
                       "sizes": flatten_sizes(data)}, f)
    except OSError:
        pass


def apply_deltas(data: dict, prev_sizes: dict):
    """Annotate the live tree in place with growth since the snapshot."""
    old = prev_sizes.get(data["path"])
    if old is None:
        data["delta"] = data["size"]
        data["new_since_snap"] = True
    else:
        data["delta"] = data["size"] - old
    for c in data.get("children", []):
        apply_deltas(c, prev_sizes)


def top_growers(data: dict, prev_sizes: dict, limit: int = 25) -> list[dict]:
    """Ranked list of what grew, deepest-meaningful entries first."""
    rows = []

    def walk(d):
        old = prev_sizes.get(d["path"])
        delta = d["size"] - old if old is not None else d["size"]
        kids = d.get("children", [])
        kid_delta = 0
        for c in kids:
            kid_delta += walk(c)
        own = delta - kid_delta            # growth not explained by children
        if delta > MB:
            rows.append({"label": d["label"], "path": d["path"], "delta": delta,
                         "own": own, "new": old is None,
                         "is_file": d.get("is_file", False)})
        return delta

    walk(data)
    # prefer specific causes: sort by delta, drop ancestors whose growth is
    # ≥80% explained by a child already in the list
    rows.sort(key=lambda r: -r["delta"])
    keep, covered = [], []
    for r in rows:
        if any(c.startswith(r["path"] + os.sep) and cd >= 0.8 * r["delta"]
               for c, cd in covered):
            continue
        keep.append(r)
        covered.append((r["path"], r["delta"]))
        if len(keep) >= limit:
            break
    return keep


def dir_size(path: Path, stats: dict | None = None) -> int:
    total, stack = 0, [path]
    while stack:
        p = stack.pop()
        try:
            with os.scandir(p) as it:
                for e in it:
                    try:
                        if is_reparse(e):
                            continue
                        if e.is_file(follow_symlinks=False):
                            sz = e.stat(follow_symlinks=False).st_size
                            total += sz
                            if stats is not None:
                                stats["files"] += 1
                                stats["bytes"] += sz
                        elif e.is_dir(follow_symlinks=False):
                            stack.append(Path(e.path))
                    except OSError:
                        pass
        except OSError:
            pass
    return total


def make_node(path: Path, det) -> dict:
    return {"path": str(path), "name": path.name or str(path), "size": 0,
            "cat": det[0] if det else "folder",
            "label": det[1] if det else (path.name or str(path)),
            "safe": det[2] if det else 0,
            "hint": det[3] if det else "",
            "cmd": det[5] if det else None,
            "children": []}


def scan_tree(path: Path, depth: int = 0, max_depth: int = 7,
              stats: dict | None = None) -> dict:
    det = None
    try:
        det = detect(path, True)
    except OSError:
        pass
    node = make_node(path, det)
    prune = det is not None and det[4]
    try:
        entries = list(os.scandir(path))
    except OSError:
        return node
    if prune or depth >= max_depth:
        node["size"] = dir_size(path, stats)
        return node
    for e in entries:
        try:
            if is_reparse(e):
                continue
            p = Path(e.path)
            if e.is_file(follow_symlinks=False):
                sz = e.stat(follow_symlinks=False).st_size
                node["size"] += sz
                if stats is not None:
                    stats["files"] += 1
                    stats["bytes"] += sz
                fdet = detect(p, False, sz) if sz > 50 * MB else None
                if fdet:
                    node["children"].append({
                        "path": e.path, "name": e.name, "size": sz, "is_file": True,
                        "cat": fdet[0], "label": fdet[1], "safe": fdet[2],
                        "hint": fdet[3], "cmd": fdet[5], "children": []})
            elif e.is_dir(follow_symlinks=False):
                child = scan_tree(p, depth + 1, max_depth, stats)
                node["size"] += child["size"]
                if child["size"] > 5 * MB or child["cat"] != "folder":
                    node["children"].append(child)
        except OSError:
            continue
    node["children"].sort(key=lambda c: -c["size"])
    return node


def scan_worker(path: Path, q: queue.Queue, stats: dict):
    """Progressive scan: emit the root immediately, then each top-level
    entry as it finishes, so the graph grows live instead of freezing."""
    det = None
    try:
        det = detect(path, True)
    except OSError:
        pass
    q.put(("root", make_node(path, det)))
    try:
        entries = list(os.scandir(path))
    except OSError:
        q.put(("done", None))
        return
    for e in entries:
        try:
            if is_reparse(e):
                continue
            stats["current"] = e.name
            p = Path(e.path)
            if e.is_file(follow_symlinks=False):
                sz = e.stat(follow_symlinks=False).st_size
                stats["files"] += 1
                stats["bytes"] += sz
                q.put(("size", sz))
                fdet = detect(p, False, sz) if sz > 50 * MB else None
                if fdet:
                    q.put(("child", {"path": e.path, "name": e.name, "size": sz,
                                     "is_file": True,
                                     "cat": fdet[0], "label": fdet[1], "safe": fdet[2],
                                     "hint": fdet[3], "cmd": fdet[5], "children": []}))
            elif e.is_dir(follow_symlinks=False):
                child = scan_tree(p, 1, 7, stats)
                q.put(("size", child["size"]))
                if child["size"] > 5 * MB or child["cat"] != "folder":
                    q.put(("child", child))
        except OSError:
            continue
    q.put(("done", None))


# ===========================================================================
# 4. THE GRAPH (Connected Papers-style force layout on tk.Canvas)
# ===========================================================================

CAT_COLORS = {
    "cache": "#27a18f", "environment": "#cf8a2e", "models": "#7e68c0",
    "project": "#4a83c2", "vm-disk": "#c2504a", "artifacts": "#d8773a",
    "archives": "#8e8e60", "data": "#5aa05a", "folder": "#5d7d89",
}
BG = "#101b21"          # deep chart-room background
EDGE = "#2c4350"
TEXT = "#cfe3e8"
TEXT_DIM = "#7e99a3"


class GNode:
    __slots__ = ("data", "x", "y", "vx", "vy", "r", "parent", "kids",
                 "expanded", "pin")

    def __init__(self, data: dict, parent: "GNode|None"):
        self.data = data
        self.parent = parent
        self.kids: list[GNode] = []
        self.expanded = False
        self.pin = False
        if parent:
            ang = random.uniform(0, 6.283)
            d = parent.r + 110          # taller spawn distance — new clusters open up clearly
            self.x = parent.x + math.cos(ang) * d
            self.y = parent.y + math.sin(ang) * d
        else:
            self.x, self.y = 0.0, 0.0
        self.vx = self.vy = 0.0
        self.r = node_radius(data["size"], data.get("is_file", False))

    @property
    def has_children(self):
        return bool(self.data["children"])


class DiskAtlasApp:
    def __init__(self, root: tk.Tk):
        self.root = root
        root.title("DiskAtlas — connected storage graph")
        root.geometry("1180x720")
        root.configure(bg=BG)

        self.nodes: list[GNode] = []
        self.root_node: GNode | None = None
        self.selected: GNode | None = None
        self.hover: GNode | None = None
        self.drag_node: GNode | None = None
        self.panning = False
        self.cx = self.cy = 0.0           # camera center (world coords)
        self.zoom = 1.0
        self.energy = 0.0
        self.scan_q: queue.Queue = queue.Queue()
        self.scanning = False
        self.scan_stats = {"files": 0, "bytes": 0, "current": ""}
        self.scan_root: Path | None = None
        self._pending_children: list[dict] = []
        # camera auto-focus (animates onto freshly expanded clusters)
        self.focus_node: GNode | None = None
        self.focus_until = 0.0
        self.focus_zoom = 1.0
        self._dirty = True                # redraw only when something changed
        self.growers: list[dict] = []     # Δ snapshot diff results
        self.snap_ts: float | None = None

        self._build_ui()
        self.root.after(30, self._tick)
        self._poll_queue()

    # ------------------------------------------------- UI
    def _build_ui(self):
        style = ttk.Style()
        try:
            style.theme_use("clam")
        except tk.TclError:
            pass
        style.configure(".", background=BG, foreground=TEXT)
        style.configure("TButton", background="#1c2e38", foreground=TEXT, padding=6)
        style.map("TButton", background=[("active", "#27414f")])
        style.configure("Side.TFrame", background="#15242c")
        style.configure("Side.TLabel", background="#15242c", foreground=TEXT,
                        font=("Segoe UI", 10))
        style.configure("SideDim.TLabel", background="#15242c", foreground=TEXT_DIM,
                        font=("Segoe UI", 9))
        style.configure("SideBig.TLabel", background="#15242c", foreground=TEXT,
                        font=("Segoe UI", 12, "bold"))

        top = tk.Frame(self.root, bg=BG)
        top.pack(fill="x", padx=10, pady=(8, 4))
        ttk.Button(top, text="📂 Scan folder…", command=self.choose).pack(side="left")
        ttk.Button(top, text="⌂ Recenter", command=self.recenter).pack(side="left", padx=(6, 0))
        tk.Button(top, text="✨ Reclaim  ·  PRO", command=self.open_reclaim,
                  bg="#1f7a4d", fg="#ffffff", activebackground="#27995f",
                  activeforeground="#ffffff", relief="flat", padx=12, pady=4,
                  font=("Segoe UI", 9, "bold"), cursor="hand2"
                  ).pack(side="left", padx=(10, 0))
        tk.Button(top, text="Δ What grew?  ·  PRO", command=self.open_growth,
                  bg="#8a5a1f", fg="#ffffff", activebackground="#a86f27",
                  activeforeground="#ffffff", relief="flat", padx=12, pady=4,
                  font=("Segoe UI", 9, "bold"), cursor="hand2"
                  ).pack(side="left", padx=(6, 0))
        self.lbl_status = tk.Label(top, text="Choose a folder to survey", bg=BG, fg=TEXT_DIM,
                                   font=("Segoe UI", 9))
        self.lbl_status.pack(side="left", padx=14)
        self.lbl_drive = tk.Label(top, text="", bg=BG, fg="#27a18f",
                                  font=("Segoe UI", 9, "bold"))
        self.lbl_drive.pack(side="right")

        body = tk.Frame(self.root, bg=BG)
        body.pack(fill="both", expand=True)

        self.canvas = tk.Canvas(body, bg=BG, highlightthickness=0, cursor="hand2")
        self.canvas.pack(side="left", fill="both", expand=True)

        # ---- side panel (Connected Papers-style details) ----
        self.side = ttk.Frame(body, style="Side.TFrame", width=290)
        self.side.pack(side="right", fill="y")
        self.side.pack_propagate(False)
        pad = {"padx": 16, "anchor": "w"}
        self.sp_title = ttk.Label(self.side, text="Nothing selected", style="SideBig.TLabel",
                                  wraplength=255, justify="left")
        self.sp_title.pack(pady=(18, 2), **pad)
        self.sp_cat = ttk.Label(self.side, text="", style="SideDim.TLabel")
        self.sp_cat.pack(**pad)
        self.sp_size = ttk.Label(self.side, text="", style="Side.TLabel",
                                 font=("Consolas", 16, "bold"))
        self.sp_size.pack(pady=(10, 0), **pad)
        self.sp_safe = ttk.Label(self.side, text="", style="Side.TLabel")
        self.sp_safe.pack(pady=(4, 0), **pad)
        self.sp_hint = ttk.Label(self.side, text="", style="SideDim.TLabel",
                                 wraplength=255, justify="left")
        self.sp_hint.pack(pady=(10, 0), **pad)
        self.sp_path = ttk.Label(self.side, text="", style="SideDim.TLabel",
                                 wraplength=255, justify="left", font=("Consolas", 8))
        self.sp_path.pack(pady=(10, 0), **pad)
        bf = ttk.Frame(self.side, style="Side.TFrame")
        bf.pack(pady=16, padx=16, anchor="w", fill="x")
        ttk.Button(bf, text="🗑 Recycle Bin", command=self.delete_recycle).pack(fill="x", pady=2)
        ttk.Button(bf, text="📁 Show in Explorer", command=self.reveal).pack(fill="x", pady=2)
        ttk.Button(bf, text="📋 Copy cleanup command", command=self.copy_cmd).pack(fill="x", pady=2)
        ttk.Label(self.side, text="legend", style="SideDim.TLabel").pack(pady=(14, 2), **pad)
        leg = tk.Canvas(self.side, bg="#15242c", height=172, highlightthickness=0)
        leg.pack(fill="x", padx=16)
        y = 10
        for cat, col in CAT_COLORS.items():
            if cat == "folder":
                continue
            leg.create_oval(4, y - 5, 14, y + 5, fill=col, outline="")
            leg.create_text(22, y, text=cat, anchor="w", fill=TEXT_DIM, font=("Segoe UI", 9))
            y += 18
        # file document glyph
        leg.create_polygon(4, y - 6, 11, y - 6, 14, y - 3, 14, y + 6, 4, y + 6,
                           fill=FILE_KIND_COLORS["code"], outline="#0a1318")
        leg.create_text(22, y, text="file (click to read)", anchor="w",
                        fill=TEXT_DIM, font=("Segoe UI", 9))
        ttk.Label(self.side,
                  text="click bubble = expand/collapse\nclick file 📄 = open viewer\n"
                       "drag = move · wheel = zoom\nright-click = actions",
                  style="SideDim.TLabel", justify="left").pack(pady=(12, 0), **pad)

        # ---- context menu: each entry is a real OS call ----
        self.menu = tk.Menu(self.root, tearoff=0, bg="#1c2e38", fg=TEXT,
                            activebackground="#27414f", activeforeground="#fff")
        self.menu.add_command(label="👁  Read file   (built-in viewer)", command=self.read_selected)
        self.menu.add_separator()
        self.menu.add_command(label="🗑  Delete → Recycle Bin   (shell32.SHFileOperationW)",
                              command=self.delete_recycle)
        self.menu.add_command(label="❌  Delete permanently", command=self.delete_perm)
        self.menu.add_separator()
        self.menu.add_command(label="📋  Copy full path", command=self.copy_path)
        self.menu.add_command(label="📋  Copy cleanup command", command=self.copy_cmd)
        self.menu.add_separator()
        self.menu.add_command(label="📁  Show in Explorer   (explorer /select)", command=self.reveal)
        self.menu.add_command(label="↗  Open   (ShellExecuteW)", command=self.open_item)

        # ---- events ----
        c = self.canvas
        c.bind("<ButtonPress-1>", self.on_press)
        c.bind("<B1-Motion>", self.on_drag)
        c.bind("<ButtonRelease-1>", self.on_release)
        c.bind("<Button-3>", self.on_rclick)
        c.bind("<Motion>", self.on_motion)
        c.bind("<MouseWheel>", self.on_wheel)          # Windows / macOS
        c.bind("<Button-4>", lambda e: self.on_wheel(e, 1))   # Linux
        c.bind("<Button-5>", lambda e: self.on_wheel(e, -1))
        c.bind("<Configure>", lambda e: setattr(self, "_dirty", True))
        self.root.bind("<Delete>", lambda e: self.delete_recycle())

    # ------------------------------------------------- camera helpers
    def w2s(self, x, y):
        W, H = self.canvas.winfo_width(), self.canvas.winfo_height()
        return (x - self.cx) * self.zoom + W / 2, (y - self.cy) * self.zoom + H / 2

    def s2w(self, sx, sy):
        W, H = self.canvas.winfo_width(), self.canvas.winfo_height()
        return (sx - W / 2) / self.zoom + self.cx, (sy - H / 2) / self.zoom + self.cy

    def node_at(self, sx, sy) -> GNode | None:
        wx, wy = self.s2w(sx, sy)
        best, bd = None, 1e18
        for n in self.nodes:
            d = (n.x - wx) ** 2 + (n.y - wy) ** 2
            if d < (n.r + 4) ** 2 and d < bd:
                best, bd = n, d
        return best

    def recenter(self):
        self.cx = self.cy = 0.0
        self.zoom = 1.0
        self.focus_node = None
        self.energy = 1.0
        self._dirty = True

    # ------------------------------------------------- scanning
    def choose(self):
        folder = filedialog.askdirectory(initialdir=str(Path.home()))
        if folder:
            self.start_scan(Path(folder))

    def start_scan(self, path: Path):
        if self.scanning:
            return
        self.scanning = True
        self.nodes.clear()
        self.root_node = None
        self.selected = None
        self.focus_node = None
        self._pending_children = []
        self.scan_stats = {"files": 0, "bytes": 0, "current": ""}
        self.scan_root = path
        self.lbl_status.config(text=f"Scanning {path} …")
        self._refresh_drive()
        self._dirty = True
        threading.Thread(target=scan_worker, args=(path, self.scan_q, self.scan_stats),
                         daemon=True).start()

    def _refresh_drive(self):
        anchor = self.scan_root if self.scan_root is not None else Path.home()
        free, total = drive_free(str(anchor.anchor or anchor))
        self.lbl_drive.config(text=f"drive: {fmt(free)} free of {fmt(total)}")

    def _finish_scan_snapshot(self):
        """Diff against the previous snapshot of this root, then save a new one."""
        rn, root = self.root_node, self.scan_root
        if rn is None or root is None:
            return ""
        prev = load_snapshot(root)
        note = ""
        self.growers, self.snap_ts = [], None
        if prev:
            apply_deltas(rn.data, prev["sizes"])
            self.growers = top_growers(rn.data, prev["sizes"])
            self.snap_ts = prev["ts"]
            total_delta = rn.data.get("delta", 0)
            if abs(total_delta) > MB:
                ago = max(1, int((time.time() - prev["ts"]) / 86400))
                note = f" · Δ {fmt_delta(total_delta)} in {ago}d"
        save_snapshot(root, rn.data)
        return note

    def _poll_queue(self):
        try:
            while True:
                kind, payload = self.scan_q.get_nowait()
                if kind == "root":
                    self.root_node = GNode(payload, None)
                    self.root_node.expanded = True       # children stream in live
                    self.nodes = [self.root_node]
                    self.energy = 1.0
                elif kind == "size" and self.root_node:
                    self.root_node.data["size"] += payload
                    self.root_node.r = node_radius(self.root_node.data["size"])
                    self._dirty = True
                elif kind == "child" and self.root_node:
                    self.root_node.data["children"].append(payload)
                    if (self.root_node.expanded
                            and len(self.root_node.kids) < self.MAX_KIDS
                            and payload["size"] > 2 * MB):
                        g = GNode(payload, self.root_node)
                        self.root_node.kids.append(g)
                        self.nodes.append(g)
                        self.energy = 1.0
                    else:
                        self._pending_children.append(payload)
                elif kind == "done":
                    self.scanning = False
                    if self.root_node:
                        rn = self.root_node
                        rn.data["children"].sort(key=lambda c: -c["size"])
                        rest_size = sum(c["size"] for c in self._pending_children)
                        if rest_size > 2 * MB:
                            agg = {"path": rn.data["path"], "name": "…", "size": rest_size,
                                   "cat": "folder", "agg": True,
                                   "label": f"+{len(self._pending_children)} smaller items",
                                   "safe": 0, "hint": "smaller items grouped — expand to act on them",
                                   "cmd": None, "children": self._pending_children}
                            g = GNode(agg, rn)
                            rn.kids.append(g)
                            self.nodes.append(g)
                        self._pending_children = []
                        note = self._finish_scan_snapshot()
                        self.lbl_status.config(
                            text=f"{rn.data['label']} — {fmt(rn.data['size'])}{note} · "
                                 f"click bubbles to expand, right-click to act")
                        self.energy = 1.0
                        self._focus_on(rn)
        except queue.Empty:
            pass
        if self.scanning and self.root_node:
            s = self.scan_stats
            cur = s.get("current", "")
            self.lbl_status.config(
                text=f"Scanning… {s['files']:,} files · {fmt(s['bytes'])}"
                     + (f" · in {cur[:32]}" if cur else ""))
        self.root.after(60, self._poll_queue)

    # ------------------------------------------------- expand / collapse
    MAX_KIDS = 14          # folder bubbles per expansion
    MAX_FILES = 8          # file documents per expansion (rest group + keep expanding)

    def _merge_files(self, d: dict):
        """Lazily enumerate the immediate files of a folder the first time it
        is expanded, adding them as document nodes. Lazy = huge trees stay
        light: file lists exist only for folders the user actually opens."""
        if d.get("files_merged") or d.get("agg") or d.get("is_file"):
            return
        d["files_merged"] = True
        have = {c["path"] for c in d["children"]}
        try:
            entries = os.scandir(d["path"])
        except OSError:
            return
        count = 0
        for e in entries:
            if count >= 3000:        # sanity cap for pathological folders
                break
            try:
                if is_reparse(e) or not e.is_file(follow_symlinks=False):
                    continue
                if e.path in have:
                    continue
                sz = e.stat(follow_symlinks=False).st_size
                fdet = detect(Path(e.path), False, sz)
                kind = file_kind(e.name)
                d["children"].append({
                    "path": e.path, "name": e.name, "size": sz, "is_file": True,
                    "fkind": kind,
                    "cat": fdet[0] if fdet else "file",
                    "label": fdet[1] if fdet else e.name,
                    "safe": fdet[2] if fdet else 0,
                    "hint": fdet[3] if fdet else f"{kind} file — click to read",
                    "cmd": fdet[5] if fdet else None,
                    "children": []})
                count += 1
            except OSError:
                continue

    def expand(self, n: GNode):
        if n.expanded:
            return
        self._merge_files(n.data)
        if not n.data["children"]:
            return
        dirs = [c for c in n.data["children"] if not c.get("is_file")]
        files = [c for c in n.data["children"] if c.get("is_file")]
        dirs.sort(key=lambda c: -c["size"])
        files.sort(key=lambda c: -c["size"])

        big_dirs = [c for c in dirs if c["size"] > 2 * MB]
        show_d, rest_d = big_dirs[: self.MAX_KIDS], big_dirs[self.MAX_KIDS:] \
            + [c for c in dirs if c["size"] <= 2 * MB]
        show_f, rest_f = files[: self.MAX_FILES], files[self.MAX_FILES:]

        for c in show_d + show_f:
            g = GNode(c, n)
            n.kids.append(g)
            self.nodes.append(g)
        if rest_d:
            rest_size = sum(c["size"] for c in rest_d)
            if rest_size > 2 * MB:
                # NOTE "agg": True — these bubbles carry the PARENT's path and
                # must never be deletable (v0.3 bug: deleting one deleted the parent)
                agg = {"path": n.data["path"], "name": "…", "size": rest_size,
                       "cat": "folder", "agg": True, "files_merged": True,
                       "label": f"+{len(rest_d)} smaller items",
                       "safe": 0, "hint": "smaller items grouped — expand to act on them",
                       "cmd": None, "children": rest_d}
                g = GNode(agg, n)
                n.kids.append(g)
                self.nodes.append(g)
        if rest_f:
            fagg = {"path": n.data["path"], "name": "…", "size": sum(c["size"] for c in rest_f),
                    "cat": "file", "agg": True, "is_file": True, "fkind": "binary",
                    "files_merged": True,
                    "label": f"+{len(rest_f)} more files",
                    "safe": 0, "hint": "more files — click to reveal the next batch",
                    "cmd": None, "children": rest_f}
            g = GNode(fagg, n)
            n.kids.append(g)
            self.nodes.append(g)
        n.expanded = True
        self.energy = 1.0
        if n.kids:
            self._focus_on(n)

    def _focus_on(self, n: GNode):
        """Glide + zoom the camera onto a node and its freshly opened children."""
        self.focus_node = n
        self.focus_until = time.time() + 1.4
        spread = n.r + 85 + 70            # spring rest + typical child radius
        W = max(self.canvas.winfo_width(), 200)
        H = max(self.canvas.winfo_height(), 200)
        want = min(W, H) * 0.38           # cluster should fill ~76% of the short axis
        self.focus_zoom = max(0.25, min(3.0, want / max(spread, 1.0)))

    def collapse(self, n: GNode):
        def kill(k: GNode):
            for kk in k.kids:
                kill(kk)
            if k in self.nodes:
                self.nodes.remove(k)
        for k in n.kids:
            kill(k)
        n.kids.clear()
        n.expanded = False
        self.energy = 1.0

    def remove_node(self, n: GNode):
        delta = n.data["size"]
        self.collapse(n)
        if n.parent:
            if n in n.parent.kids:
                n.parent.kids.remove(n)
            if n.data in n.parent.data["children"]:
                n.parent.data["children"].remove(n.data)
        if n in self.nodes:
            self.nodes.remove(n)
        # propagate freed size up the chain so the graph stays truthful
        a = n.parent
        while a:
            a.data["size"] = max(0, a.data["size"] - delta)
            a.r = node_radius(a.data["size"])
            a = a.parent
        if self.selected is n:
            self.selected = None
            self._update_side()
        self._refresh_drive()
        self.energy = 1.0

    # ------------------------------------------------- physics + render
    def _tick(self):
        moved = False
        # camera glide toward freshly expanded cluster
        if self.focus_node is not None:
            if self.focus_node in self.nodes and time.time() < self.focus_until:
                n = self.focus_node
                self.cx += (n.x - self.cx) * 0.14
                self.cy += (n.y - self.cy) * 0.14
                self.zoom += (self.focus_zoom - self.zoom) * 0.14
                moved = True
            else:
                self.focus_node = None
        if self.nodes and (self.energy > 0.005 or self.drag_node):
            self._physics()
            moved = True
        if moved or self._dirty:
            self._render()
            self._dirty = False
        self.root.after(30, self._tick)

    def _physics(self):
        ns = self.nodes
        # pairwise repulsion
        for i in range(len(ns)):
            a = ns[i]
            for j in range(i + 1, len(ns)):
                b = ns[j]
                dx, dy = a.x - b.x, a.y - b.y
                d2 = dx * dx + dy * dy + 0.01
                if d2 > 360000:        # ignore beyond 600px
                    continue
                d = math.sqrt(d2)
                overlap = (a.r + b.r + 26) - d
                f = (9000.0 / d2) + (max(overlap, 0) * 0.08)
                fx, fy = f * dx / d, f * dy / d
                a.vx += fx; a.vy += fy
                b.vx -= fx; b.vy -= fy
        # springs on edges
        for n in ns:
            if n.parent:
                rest = n.parent.r + n.r + 85    # taller edge distance — opened clusters breathe
                dx, dy = n.x - n.parent.x, n.y - n.parent.y
                d = math.sqrt(dx * dx + dy * dy) + 0.01
                f = (d - rest) * 0.012
                fx, fy = f * dx / d, f * dy / d
                n.vx -= fx; n.vy -= fy
                n.parent.vx += fx * 0.4; n.parent.vy += fy * 0.4
        # gentle centering on root
        r0 = self.nodes[0]
        r0.vx -= r0.x * 0.01
        r0.vy -= r0.y * 0.01
        # integrate
        e = 0.0
        for n in ns:
            if n.pin:
                n.vx = n.vy = 0.0
                continue
            n.vx *= 0.82; n.vy *= 0.82
            v = math.hypot(n.vx, n.vy)
            if v > 18:
                n.vx *= 18 / v; n.vy *= 18 / v
            n.x += n.vx; n.y += n.vy
            e += v
        self.energy = e / max(len(ns), 1) / 18.0

    def _render(self):
        c = self.canvas
        c.delete("all")
        if not self.nodes:
            W, H = c.winfo_width(), c.winfo_height()
            c.create_text(W / 2, H / 2, text="DiskAtlas\n\nScan a folder to see your storage\nas a connected graph",
                          fill=TEXT_DIM, font=("Segoe UI", 13), justify="center")
            return
        # edges first
        for n in self.nodes:
            if n.parent:
                x1, y1 = self.w2s(n.parent.x, n.parent.y)
                x2, y2 = self.w2s(n.x, n.y)
                c.create_line(x1, y1, x2, y2, fill=EDGE, width=max(1, 1.4 * self.zoom))
        # nodes
        for n in self.nodes:
            x, y = self.w2s(n.x, n.y)
            r = n.r * self.zoom
            d = n.data
            is_file = d.get("is_file")
            if is_file and d.get("cat", "file") == "file":
                col = FILE_KIND_COLORS.get(d.get("fkind", "binary"), FILE_KIND_COLORS["binary"])
            else:
                col = CAT_COLORS.get(d["cat"], CAT_COLORS["folder"])
            # safety halo
            if d["safe"] >= 80:
                c.create_oval(x - r - 4, y - r - 4, x + r + 4, y + r + 4,
                              outline="#3ddc97", width=2)
            elif 0 < d["safe"] <= 20:
                c.create_oval(x - r - 4, y - r - 4, x + r + 4, y + r + 4,
                              outline="#ff6b5e", width=2)
            outline = "#ffffff" if n is self.selected else ("#9fd8cf" if n is self.hover else "")
            if is_file:
                # document glyph: paper with a folded top-right corner
                w, h = r * 0.82, r
                fold = min(w, h) * 0.45
                pts = [x - w, y - h, x + w - fold, y - h, x + w, y - h + fold,
                       x + w, y + h, x - w, y + h]
                c.create_polygon(*pts, fill=col, outline=outline or "#0a1318",
                                 width=2 if outline else 1)
                # fold crease
                c.create_line(x + w - fold, y - h, x + w - fold, y - h + fold,
                              x + w, y - h + fold, fill="#0a1318", width=1)
                if d.get("agg") and r > 8:
                    c.create_text(x, y, text="+", fill="#ffffff",
                                  font=("Segoe UI", max(8, min(14, int(r * 0.6))), "bold"))
            else:
                c.create_oval(x - r, y - r, x + r, y + r, fill=col,
                              outline=outline, width=2 if outline else 0)
                # expand badge
                if n.has_children and not n.expanded:
                    c.create_text(x, y, text="+", fill="#ffffff",
                                  font=("Segoe UI", max(9, min(15, int(r * 0.5))), "bold"))
            # labels
            if r > 11:
                c.create_text(x, y + r + 11, text=d["label"][:34], fill=TEXT,
                              font=("Segoe UI", max(8, int(9 * self.zoom))))
                c.create_text(x, y + r + 24, text=fmt(d["size"]), fill=TEXT_DIM,
                              font=("Consolas", max(7, int(8 * self.zoom))))
                delta = d.get("delta", 0)
                if abs(delta) > 10 * MB:
                    grew = delta > 0
                    badge = ("NEW " if d.get("new_since_snap") else
                             ("▲ " if grew else "▼ ")) + fmt_delta(delta)
                    c.create_text(x, y + r + 37, text=badge,
                                  fill="#ff9f43" if grew else "#3ddc97",
                                  font=("Consolas", max(7, int(8 * self.zoom)), "bold"))
        # hover tooltip
        if self.hover and self.hover is not self.drag_node:
            n = self.hover
            x, y = self.w2s(n.x, n.y)
            lines = [f"{n.data['label']}  —  {fmt(n.data['size'])}"]
            if n.data["safe"]:
                lines.append(f"safe to delete: {n.data['safe']}/100")
            if n.data["hint"]:
                lines.append("↳ " + n.data["hint"])
            txt = "\n".join(lines)
            tw = max(len(l) for l in lines) * 6.4 + 18
            th = len(lines) * 15 + 12
            tx = min(x + 18, c.winfo_width() - tw - 6)
            ty = max(y - th - 12, 6)
            c.create_rectangle(tx, ty, tx + tw, ty + th, fill="#0a1318",
                               outline="#2c4350")
            c.create_text(tx + 9, ty + 6, text=txt, anchor="nw", fill=TEXT,
                          font=("Segoe UI", 8), justify="left")

    # ------------------------------------------------- mouse
    def on_press(self, ev):
        self.focus_node = None            # user takes over the camera
        n = self.node_at(ev.x, ev.y)
        self._press_xy = (ev.x, ev.y)
        self._moved = False
        if n:
            self.drag_node = n
            n.pin = True
            self.selected = n
            self._update_side()
        else:
            self.panning = True
        self._dirty = True

    def on_drag(self, ev):
        dx = ev.x - self._press_xy[0]
        dy = ev.y - self._press_xy[1]
        if abs(dx) + abs(dy) > 4:
            self._moved = True
        if self.drag_node:
            wx, wy = self.s2w(ev.x, ev.y)
            self.drag_node.x, self.drag_node.y = wx, wy
            self.energy = 1.0
        elif self.panning:
            self.cx -= dx / self.zoom
            self.cy -= dy / self.zoom
            self._press_xy = (ev.x, ev.y)
            self._dirty = True

    def on_release(self, ev):
        if self.drag_node:
            n = self.drag_node
            n.pin = False
            self.drag_node = None
            if not self._moved:
                d = n.data
                if d.get("is_file") and not d.get("agg"):
                    self.open_viewer(n)              # ✨ click a file = read it
                elif n.expanded:
                    self.collapse(n)
                else:
                    self.expand(n)
        self.panning = False
        self._dirty = True

    def on_motion(self, ev):
        h = self.node_at(ev.x, ev.y)
        if h is not self.hover:
            self.hover = h
            self._dirty = True

    def on_wheel(self, ev, direction=None):
        self.focus_node = None            # user takes over the camera
        delta = direction if direction is not None else (1 if ev.delta > 0 else -1)
        old = self.zoom
        self.zoom = max(0.25, min(3.0, self.zoom * (1.12 if delta > 0 else 0.89)))
        # zoom around cursor
        wx, wy = self.s2w(ev.x, ev.y)
        self.cx = wx - (wx - self.cx) * old / self.zoom
        self.cy = wy - (wy - self.cy) * old / self.zoom
        self._dirty = True

    def on_rclick(self, ev):
        n = self.node_at(ev.x, ev.y)
        if n:
            self.selected = n
            self._update_side()
            self._dirty = True
            self.menu.tk_popup(ev.x_root, ev.y_root)

    # ------------------------------------------------- side panel
    def _update_side(self):
        n = self.selected
        if not n:
            self.sp_title.config(text="Nothing selected")
            for w in (self.sp_cat, self.sp_size, self.sp_safe, self.sp_hint, self.sp_path):
                w.config(text="")
            return
        d = n.data
        self.sp_title.config(text=d["label"])
        cat = d["cat"]
        if d.get("is_file") and not d.get("agg"):
            cat = f"file · {d.get('fkind') or file_kind(d['name'])}"
        self.sp_cat.config(text=cat + ("  ·  grouped (not deletable)" if d.get("agg") else ""))
        self.sp_size.config(text=fmt(d["size"]))
        s = d["safe"]
        verdict = ("SAFE — regenerates automatically" if s >= 95 else
                   "safe — easily recreated" if s >= 80 else
                   "review before deleting" if s >= 40 else
                   "KEEP — deleting is risky" if s else "")
        self.sp_safe.config(text=f"safe score {s}/100 · {verdict}" if s else "unclassified folder")
        self.sp_hint.config(text=("↳ " + d["hint"]) if d["hint"] else "")
        self.sp_path.config(text=d["path"])

    # ------------------------------------------------- actions (real OS calls)
    def _deletable(self, n: GNode | None) -> bool:
        """Root and grouped '+N smaller items' bubbles must never be deleted —
        an aggregate carries its PARENT's path, so deleting it would wipe the
        whole parent folder."""
        if not n or n.parent is None:
            return False
        if n.data.get("agg"):
            self.lbl_status.config(
                text="'+N smaller items' is a grouped view — expand it and act on individual items.")
            return False
        return True

    def delete_recycle(self):
        n = self.selected
        if not self._deletable(n):
            return
        d = n.data
        msg = f"Move to Recycle Bin?\n\n{d['label']} — {fmt(d['size'])}\n{d['path']}"
        if d["safe"] < 80:
            msg += f"\n\n⚠ safe score only {d['safe']}/100:\n{d['hint']}"
        if not messagebox.askyesno("Delete to Recycle Bin", msg,
                                   icon="warning" if d["safe"] < 80 else "question"):
            return
        if win_recycle([d["path"]]):
            self.remove_node(n)
            self.lbl_status.config(text=f"Recycled {d['label']} ({fmt(d['size'])}) — undoable from Recycle Bin.")
        else:
            messagebox.showerror("DiskAtlas", "Shell operation failed (file in use or access denied).")

    def delete_perm(self):
        n = self.selected
        if not self._deletable(n):
            return
        d = n.data
        msg = (f"Permanently delete?\n\n{d['label']} — {fmt(d['size'])}\n{d['path']}\n\n"
               "This CANNOT be undone.")
        if d["safe"] < 80:
            msg += f"\n\n⚠ safe score only {d['safe']}/100:\n{d['hint']}"
        if not messagebox.askyesno("Delete PERMANENTLY", msg, icon="warning"):
            return
        if d["cat"] == "vm-disk":
            if not messagebox.askyesno("VM disk!", "This is a virtual disk — deleting it destroys "
                                       "the entire Linux/VM system inside.\nABSOLUTELY sure?",
                                       icon="warning"):
                return
        try:
            p = Path(d["path"])
            shutil.rmtree(p) if p.is_dir() else p.unlink()
            self.remove_node(n)
            self.lbl_status.config(text=f"Permanently deleted {d['label']}.")
        except OSError as ex:
            messagebox.showerror("DiskAtlas", f"Could not delete:\n{ex}")

    def copy_path(self):
        if self.selected:
            self.root.clipboard_clear()
            self.root.clipboard_append(self.selected.data["path"])
            self.lbl_status.config(text="Path copied.")

    def copy_cmd(self):
        n = self.selected
        if n:
            d = n.data
            if IS_WIN:
                fallback = f'Remove-Item -Recurse -Force "{d["path"]}"'
            else:
                fallback = f'rm -rf "{d["path"]}"'
            cmd = d.get("cmd") or fallback
            comment = f"# {d['label']} — frees {fmt(d['size'])} (safe {d['safe']}/100)"
            if d.get("hint"):
                comment += f"\n# {d['hint']}"
            self.root.clipboard_clear()
            self.root.clipboard_append(f"{comment}\n{cmd}")
            self.lbl_status.config(text="Cleanup command copied — paste into your terminal.")

    def reveal(self):
        if self.selected:
            win_reveal(self.selected.data["path"])

    def read_selected(self):
        n = self.selected
        if not n:
            return
        if n.data.get("is_file") and not n.data.get("agg"):
            self.open_viewer(n)
        else:
            self.lbl_status.config(text="The viewer reads files — expand this and click a 📄 document.")

    # ------------------------------------------------- ✨ RECLAIM (Pro)
    def open_growth(self):
        win = tk.Toplevel(self.root)
        win.title("Δ What grew?")
        win.configure(bg=BG)
        win.geometry("620x520")
        win.transient(self.root)
        win.bind("<Escape>", lambda e: win.destroy())
        tk.Label(win, text="Δ What grew?", bg=BG, fg="#ff9f43",
                 font=("Segoe UI", 15, "bold")).pack(anchor="w", padx=18, pady=(14, 0))
        if self.snap_ts is None or not self.growers:
            msg = ("Scan a folder first." if self.root_node is None else
                   "First snapshot of this folder recorded. ✓\n\n"
                   "Scan it again after a few days (or after installing things)\n"
                   "and DiskAtlas will show exactly what grew, ranked.")
            if self.snap_ts is not None:
                msg = "Nothing grew more than 1 MB since the last snapshot. 🎉"
            tk.Label(win, text=msg, bg=BG, fg=TEXT_DIM, justify="left",
                     font=("Segoe UI", 11)).pack(anchor="w", padx=18, pady=20)
            return
        since = time.strftime("%Y-%m-%d %H:%M", time.localtime(self.snap_ts))
        tk.Label(win, text=f"compared to the snapshot from {since}",
                 bg=BG, fg=TEXT_DIM, font=("Segoe UI", 9)).pack(anchor="w", padx=18, pady=(2, 8))
        frame = tk.Frame(win, bg="#15242c")
        frame.pack(fill="both", expand=True, padx=18, pady=(0, 14))
        cv = tk.Canvas(frame, bg="#15242c", highlightthickness=0)
        ys = ttk.Scrollbar(frame, orient="vertical", command=cv.yview)
        inner = tk.Frame(cv, bg="#15242c")
        inner.bind("<Configure>", lambda e: cv.configure(scrollregion=cv.bbox("all")))
        cv.create_window((0, 0), window=inner, anchor="nw")
        cv.configure(yscrollcommand=ys.set)
        ys.pack(side="right", fill="y")
        cv.pack(side="left", fill="both", expand=True)
        biggest = max(r["delta"] for r in self.growers)
        for r in self.growers:
            row = tk.Frame(inner, bg="#15242c")
            row.pack(fill="x", padx=10, pady=4)
            tag = "NEW" if r["new"] else "▲"
            tk.Label(row, text=f"{tag} {fmt_delta(r['delta'])}", bg="#15242c",
                     fg="#ff9f43", width=12, anchor="w",
                     font=("Consolas", 10, "bold")).pack(side="left")
            tk.Label(row, text=r["label"][:40], bg="#15242c", fg=TEXT,
                     font=("Segoe UI", 10, "bold")).pack(side="left", padx=(4, 8))
            # growth bar
            bw = int(220 * r["delta"] / biggest)
            bar = tk.Canvas(row, width=224, height=10, bg="#15242c",
                            highlightthickness=0)
            bar.create_rectangle(0, 1, max(bw, 3), 9, fill="#8a5a1f", outline="")
            bar.pack(side="right")
            tk.Label(inner, text="      " + r["path"], bg="#15242c", fg=TEXT_DIM,
                     font=("Consolas", 8)).pack(fill="x", padx=10, anchor="w")

    def open_reclaim(self):
        targets = build_reclaim_targets()
        win = tk.Toplevel(self.root)
        win.title("✨ Reclaim — free space safely")
        win.configure(bg=BG)
        win.geometry("640x560")
        win.transient(self.root)
        win.bind("<Escape>", lambda e: win.destroy())

        tk.Label(win, text="✨ Reclaim", bg=BG, fg="#3ddc97",
                 font=("Segoe UI", 15, "bold")).pack(anchor="w", padx=18, pady=(14, 0))
        tk.Label(win, text="Everything below is regenerated automatically by Windows or the app\n"
                           "that owns it. Nothing you created is ever touched. Locked files are skipped.",
                 bg=BG, fg=TEXT_DIM, font=("Segoe UI", 9), justify="left"
                 ).pack(anchor="w", padx=18, pady=(2, 10))

        listf = tk.Frame(win, bg="#15242c")
        listf.pack(fill="both", expand=True, padx=18)

        rows: list[dict] = []
        total_lbl = tk.Label(win, text="measuring…", bg=BG, fg="#3ddc97",
                             font=("Segoe UI", 13, "bold"))

        def refresh_total():
            tot = sum(t["size"] for t in targets if t["size"] and t["var"].get())
            pending = any(t["size"] is None for t in targets)
            total_lbl.config(text=("measuring…  " if pending else "")
                             + f"selected: {fmt(tot)}")
            go_btn.config(text=f"  Free {fmt(tot)} now  " if tot else "  Nothing selected  ",
                          state="normal" if tot else "disabled")

        for i, t in enumerate(targets):
            t["var"] = tk.BooleanVar(master=win, value=True)
            row = tk.Frame(listf, bg="#15242c")
            row.pack(fill="x", padx=10, pady=3)
            cb = tk.Checkbutton(row, variable=t["var"], bg="#15242c",
                                activebackground="#15242c", selectcolor="#0a1318",
                                command=refresh_total)
            cb.pack(side="left")
            name = t["label"] + ("   🛡 needs admin" if t["admin"] else "")
            tk.Label(row, text=name, bg="#15242c", fg=TEXT,
                     font=("Segoe UI", 10, "bold")).pack(side="left", padx=(2, 8))
            szl = tk.Label(row, text="…", bg="#15242c", fg="#3ddc97",
                           font=("Consolas", 10, "bold"))
            szl.pack(side="right", padx=8)
            tk.Label(listf, text="     " + t["desc"], bg="#15242c", fg=TEXT_DIM,
                     font=("Segoe UI", 8)).pack(fill="x", padx=10, anchor="w")
            t["szl"] = szl
            rows.append(t)

        total_lbl.pack(anchor="w", padx=18, pady=(10, 2))
        prog = tk.Label(win, text="", bg=BG, fg=TEXT_DIM, font=("Segoe UI", 9))
        prog.pack(anchor="w", padx=18)
        drive_lbl = tk.Label(win, text="", bg=BG, fg=TEXT_DIM,
                             font=("Consolas", 11, "bold"))
        drive_lbl.pack(anchor="w", padx=18)
        go_btn = tk.Button(win, text="  measuring…  ", state="disabled",
                           bg="#1f7a4d", fg="#ffffff", activebackground="#27995f",
                           activeforeground="#ffffff", relief="flat", pady=8,
                           font=("Segoe UI", 12, "bold"), cursor="hand2")
        go_btn.pack(fill="x", padx=18, pady=(6, 14))

        # ---- measure all targets in a background thread, update rows live ----
        mq: queue.Queue = queue.Queue()

        def measure_all():
            for t in targets:
                try:
                    measure_target(t)
                except OSError:
                    t["size"] = 0
                mq.put(t)
            mq.put(None)

        def poll_measure():
            done = False
            try:
                while True:
                    item = mq.get_nowait()
                    if item is None:
                        done = True
                    else:
                        item["szl"].config(
                            text=fmt(item["size"]) if item["size"] else "—")
                        if not item["size"]:
                            item["var"].set(False)
            except queue.Empty:
                pass
            refresh_total()
            if not done and win.winfo_exists():
                win.after(120, poll_measure)

        threading.Thread(target=measure_all, daemon=True).start()
        win.after(120, poll_measure)

        # ---- execution ----
        def execute():
            chosen = [t for t in targets if t["var"].get() and t["size"]]
            if not chosen:
                return
            tot = sum(t["size"] for t in chosen)
            if not messagebox.askyesno(
                    "Reclaim", f"Free {fmt(tot)} across {len(chosen)} locations?\n\n"
                    "Caches and temp files are deleted permanently — every one of\n"
                    "them is rebuilt automatically. Locked files are skipped.",
                    parent=win):
                return
            anchor = self.scan_root if self.scan_root is not None else Path.home()
            drive_path = str(anchor.anchor or anchor)
            before_free = drive_free(drive_path)[0]
            go_btn.config(state="disabled", text="  working…  ")

            def worker():
                freed = skipped = 0
                for t in chosen:
                    prog_q.put(("now", t["label"]))
                    f, s = clean_target(t)
                    freed += f
                    skipped += s
                    prog_q.put(("done_one", (t, f)))
                prog_q.put(("all_done", (freed, skipped)))

            prog_q: queue.Queue = queue.Queue()

            def animate_drive(freed):
                """the before -> after moment: drive free counts up live"""
                after_free = drive_free(drive_path)[0]
                gain = max(after_free - before_free, freed)
                steps, dur = 24, 1300

                def step(i):
                    if not win.winfo_exists():
                        return
                    k = 1 - (1 - i / steps) ** 3          # ease-out
                    cur = before_free + gain * k
                    drive_lbl.config(
                        text=f"💾 drive free:  {fmt(before_free)}  →  {fmt(cur)}"
                             f"   ({fmt_delta(int(gain * k))})")
                    if i < steps:
                        win.after(dur // steps, lambda: step(i + 1))
                drive_lbl.config(fg="#3ddc97")
                step(1)

            def poll_exec():
                finished = False
                try:
                    while True:
                        kind, payload = prog_q.get_nowait()
                        if kind == "now":
                            prog.config(text=f"cleaning: {payload} …")
                        elif kind == "done_one":
                            t, f = payload
                            t["szl"].config(text=f"freed {fmt(f)}" if f else "—",
                                            fg="#9fd8cf")
                            t["var"].set(False)
                        elif kind == "all_done":
                            finished = True
                            freed, skipped = payload
                            prog.config(
                                text=f"✅ Freed {fmt(freed)}"
                                     + (f" · {skipped} locked items skipped" if skipped else ""))
                            go_btn.config(text=f"  Freed {fmt(freed)} ✓  ")
                            animate_drive(freed)
                            self._refresh_drive()
                            self.lbl_status.config(
                                text=f"✨ Reclaim freed {fmt(freed)} — drive updated.")
                except queue.Empty:
                    pass
                if not finished and win.winfo_exists():
                    win.after(120, poll_exec)

            threading.Thread(target=worker, daemon=True).start()
            win.after(120, poll_exec)

        go_btn.config(command=execute)

    def open_item(self):
        if self.selected:
            win_open(self.selected.data["path"])

    # ------------------------------------------------- ✨ built-in file viewer
    VIEW_TEXT_CAP = 2 * MB          # read at most this much text
    VIEW_HEX_CAP = 4096             # bytes shown in hex dumps
    VIEW_IMG_CAP = 25 * MB          # don't try to render giant images

    def open_viewer(self, n: GNode):
        d = n.data
        path = Path(d["path"])
        try:
            st = path.stat()
        except OSError as ex:
            messagebox.showerror("DiskAtlas", f"Cannot read file:\n{ex}")
            return
        size = st.st_size
        mtime = time.strftime("%Y-%m-%d %H:%M", time.localtime(st.st_mtime))
        kind = d.get("fkind") or file_kind(path.name)

        win = tk.Toplevel(self.root)
        win.title(f"{path.name} — {fmt(size)}")
        win.configure(bg=BG)
        win.geometry("780x580")
        win.bind("<Escape>", lambda e: win.destroy())

        head = tk.Frame(win, bg=BG)
        head.pack(fill="x", padx=12, pady=(10, 4))
        tk.Label(head, text=path.name, bg=BG, fg=TEXT,
                 font=("Segoe UI", 12, "bold")).pack(side="left")
        tk.Label(head, text=f"   {fmt(size)} · {kind} · modified {mtime}",
                 bg=BG, fg=TEXT_DIM, font=("Segoe UI", 9)).pack(side="left")
        btns = tk.Frame(win, bg=BG)
        btns.pack(fill="x", padx=12)
        ttk.Button(btns, text="↗ Open in default app",
                   command=lambda: win_open(str(path))).pack(side="left", padx=(0, 6))
        ttk.Button(btns, text="📁 Show in folder",
                   command=lambda: win_reveal(str(path))).pack(side="left", padx=(0, 6))

        def copy_p():
            win.clipboard_clear()
            win.clipboard_append(str(path))
        ttk.Button(btns, text="📋 Copy path", command=copy_p).pack(side="left")

        body = tk.Frame(win, bg=BG)
        body.pack(fill="both", expand=True, padx=12, pady=10)

        # ---- sniff content ----
        try:
            with open(path, "rb") as f:
                head_bytes = f.read(8192)
        except OSError as ex:
            tk.Label(body, text=f"Cannot read file:\n{ex}", bg=BG, fg=TEXT,
                     font=("Segoe UI", 10)).pack(pady=30)
            return
        if size == 0:
            tk.Label(body, text="(empty file)", bg=BG, fg=TEXT_DIM,
                     font=("Segoe UI", 11)).pack(pady=30)
            return

        ext = path.suffix.lower()
        if ext in (".png", ".gif", ".jpg", ".jpeg", ".bmp", ".webp") and size <= self.VIEW_IMG_CAP:
            if self._show_image(win, body, path, ext):
                return
        printable = sum(1 for b in head_bytes if 32 <= b < 127 or b in (9, 10, 13))
        is_text = b"\x00" not in head_bytes and printable / max(len(head_bytes), 1) > 0.85
        if is_text:
            self._show_text(win, body, path, size)
        else:
            self._show_hex(body, path, size, head_bytes)

    def _show_image(self, win, body, path: Path, ext: str) -> bool:
        img = None
        if ext in (".png", ".gif"):
            try:
                img = tk.PhotoImage(master=win, file=str(path))
            except tk.TclError:
                img = None
        if img is None:
            try:                                  # JPEG/WebP/BMP via Pillow if present
                from PIL import Image, ImageTk
                pil = Image.open(path)
                pil.thumbnail((720, 440))
                img = ImageTk.PhotoImage(pil, master=win)
            except Exception:
                return False                       # fall back to hex view
        else:
            f = max(1, math.ceil(max(img.width() / 720, img.height() / 440)))
            if f > 1:
                img = img.subsample(f, f)
        win._img_ref = img                         # keep alive
        cv = tk.Canvas(body, bg="#0a1318", highlightthickness=0)
        cv.pack(fill="both", expand=True)
        cv.create_image(360, 230, image=img, anchor="center")
        tk.Label(body, text=f"{img.width()} × {img.height()} preview",
                 bg=BG, fg=TEXT_DIM, font=("Segoe UI", 8)).pack(anchor="w")
        return True

    def _show_text(self, win, body, path: Path, size: int):
        bar = tk.Frame(body, bg=BG)
        bar.pack(fill="x", pady=(0, 6))
        tk.Label(bar, text="find:", bg=BG, fg=TEXT_DIM,
                 font=("Segoe UI", 9)).pack(side="left")
        find_var = tk.StringVar()
        ent = tk.Entry(bar, textvariable=find_var, bg="#1c2e38", fg=TEXT,
                       insertbackground=TEXT, relief="flat", width=28)
        ent.pack(side="left", padx=6, ipady=2)
        lbl_hits = tk.Label(bar, text="", bg=BG, fg="#3ddc97", font=("Segoe UI", 9))
        lbl_hits.pack(side="left", padx=6)

        frame = tk.Frame(body, bg=BG)
        frame.pack(fill="both", expand=True)
        txt = tk.Text(frame, bg="#0a1318", fg=TEXT, insertbackground=TEXT,
                      relief="flat", wrap="none", font=("Consolas", 10),
                      padx=10, pady=8)
        ys = ttk.Scrollbar(frame, orient="vertical", command=txt.yview)
        xs = ttk.Scrollbar(frame, orient="horizontal", command=txt.xview)
        txt.configure(yscrollcommand=ys.set, xscrollcommand=xs.set)
        ys.pack(side="right", fill="y")
        xs.pack(side="bottom", fill="x")
        txt.pack(side="left", fill="both", expand=True)

        try:
            with open(path, "rb") as f:
                raw = f.read(self.VIEW_TEXT_CAP + 1)
        except OSError as ex:
            txt.insert("1.0", f"Cannot read file: {ex}")
            txt.configure(state="disabled")
            return
        truncated = len(raw) > self.VIEW_TEXT_CAP
        content = raw[: self.VIEW_TEXT_CAP].decode("utf-8", errors="replace")
        txt.insert("1.0", content)
        if truncated:
            txt.insert("end", f"\n\n— … showing first {fmt(self.VIEW_TEXT_CAP)} "
                              f"of {fmt(size)} — open in default app for the rest —")
        txt.configure(state="disabled")
        txt.tag_configure("hit", background="#2f6b54", foreground="#ffffff")

        def do_find(*_):
            txt.tag_remove("hit", "1.0", "end")
            q = find_var.get()
            if not q:
                lbl_hits.config(text="")
                return
            count, idx, first = 0, "1.0", None
            while True:
                idx = txt.search(q, idx, nocase=True, stopindex="end")
                if not idx:
                    break
                end = f"{idx}+{len(q)}c"
                txt.tag_add("hit", idx, end)
                if first is None:
                    first = idx
                count += 1
                idx = end
            lbl_hits.config(text=f"{count} match{'es' if count != 1 else ''}")
            if first:
                txt.see(first)
        find_var.trace_add("write", do_find)
        ent.focus_set()

    def _show_hex(self, body, path: Path, size: int, head_bytes: bytes):
        data = head_bytes[: self.VIEW_HEX_CAP]
        lines = []
        for off in range(0, len(data), 16):
            chunk = data[off:off + 16]
            hx = " ".join(f"{b:02x}" for b in chunk).ljust(47)
            asc = "".join(chr(b) if 32 <= b < 127 else "·" for b in chunk)
            lines.append(f"{off:08x}  {hx}  {asc}")
        frame = tk.Frame(body, bg=BG)
        frame.pack(fill="both", expand=True)
        txt = tk.Text(frame, bg="#0a1318", fg=TEXT, relief="flat",
                      font=("Consolas", 9), padx=10, pady=8)
        ys = ttk.Scrollbar(frame, orient="vertical", command=txt.yview)
        txt.configure(yscrollcommand=ys.set)
        ys.pack(side="right", fill="y")
        txt.pack(side="left", fill="both", expand=True)
        txt.insert("1.0", "binary file — hex preview of the first "
                          f"{fmt(min(size, self.VIEW_HEX_CAP))} of {fmt(size)}\n\n")
        txt.insert("end", "\n".join(lines))
        txt.configure(state="disabled")


def main():
    root = tk.Tk()
    app = DiskAtlasApp(root)
    root.after(300, lambda: app.start_scan(Path.home()))
    root.mainloop()


if __name__ == "__main__":
    main()
