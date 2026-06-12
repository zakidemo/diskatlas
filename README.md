# DiskAtlas 🗺️

**Connected Papers for your disk.** A force-directed graph of your storage that doesn't just show you *what's big* — it tells you *what it is* and *whether it's safe to delete*.

> Your disk isn't full of folders. It's full of **things**: conda environments, model weights, npm caches, WSL virtual disks, training checkpoints. DiskAtlas recognizes them, explains them in plain words, scores how safe they are to remove, and hands you the exact command to reclaim the space.

<!-- HERO GIF GOES HERE: record the Reclaim drive counter animating up. This one GIF is the whole pitch. -->
<!-- ![DiskAtlas reclaiming space](docs/hero.gif) -->

---

## Why another disk tool?

WinDirStat, WizTree and TreeSize answer one question: *"which folder is big?"*
DiskAtlas answers the question you actually have: *"what IS this thing, and can I delete it without breaking something?"*

| | Treemap tools | DiskAtlas |
|---|---|---|
| Shows size | ✅ | ✅ |
| Knows a conda env from a photo album | ❌ | ✅ |
| Safe-to-delete score (0–100) on every item | ❌ | ✅ |
| Hands you the real cleanup command | ❌ | ✅ (`conda clean --all -y`, `pip cache purge`…) |
| Warns you before you destroy your WSL disk | ❌ | ✅ |

## Features

🫧 **The graph.** Folders and entities are bubbles, sized by GB, colored by type, connected like a mind-map. Click to expand, and the camera glides onto the cluster you opened. A green halo means *safe to delete*; red means *danger*.

🔍 **Semantic detection.** Recognizes conda envs, venvs, `node_modules`, pip/npm/yarn/HuggingFace/PyTorch caches, model weights (`.safetensors`, `.gguf`, `.ckpt`…), WSL/Hyper-V `.vhdx` disks, training outputs (`wandb`, `mlruns`, `checkpoints`), git projects, Rust toolchains, and more — each with a plain-language explanation and a 0–100 safe score.

📄 **Files are nodes too.** Every file appears as a document-shaped glyph, colored by kind (code / text / image / media / archive / doc). Click one and it opens in the **built-in viewer**: text and code with live search highlighting, images rendered inline, binaries as a clean hex dump. Read-only, capped, instant — even on a 50 GB file.

✨ **Reclaim** *(Pro)*. One green button measures every guaranteed-safe location — temp files, Windows Update leftovers, thumbnail cache, crash dumps, browser page caches, the Recycle Bin itself — shows an itemized checkbox review, and frees it all in one click. Then watches your drive-free counter climb in real time. Locked files are skipped, never crashed on. Nothing you created is ever in the bucket.

Δ **What grew?** *(Pro)*. Every scan is snapshotted. Scan again next week and bubbles wear growth badges (▲ +2.1 GB), new items are tagged NEW, and a ranked growers list answers the eternal question: *"why is my disk suddenly full?"* — naming the cause, not the container.

🛡 **Safety is the architecture, not a feature.**
- Deletes go to the **Recycle Bin** by default (real `SHFileOperationW` call — undoable)
- Items scoring under 80 get an explicit warning on *both* recycle and permanent delete
- `.vhdx` virtual disks require a third confirmation (deleting one destroys the Linux system inside)
- Grouped "+N items" bubbles are view-only and can never be deleted
- Windows junctions/reparse points are skipped — no double counting, no loops
- Sizes propagate up after every delete, so the map never lies

## Install

**Run from source** (Python 3.10+, zero dependencies — stdlib only):

```bash
python diskatlas_app.py
```

**Build a portable .exe** (Windows): double-click `build_exe.bat`. Your single-file app appears at `dist\DiskAtlas.exe` — target PCs don't need Python.

> ⚠️ **SmartScreen note:** the exe is currently unsigned, so Windows may show "Windows protected your PC" → *More info* → *Run anyway*. Code signing is on the roadmap. If that makes you uncomfortable (it should — healthy instinct for a deletion tool!), run from source instead: it's one readable Python file, audit it yourself.

## Controls

| Action | Result |
|---|---|
| Click a folder bubble | expand / collapse (camera auto-focuses) |
| Click a file 📄 | open it in the built-in viewer |
| Drag | move a node / pan the map |
| Mouse wheel | zoom around the cursor |
| Right-click | actions (recycle, delete, copy command, reveal, open) |
| `Del` | send selected item to Recycle Bin |
| `Esc` (in viewer) | close viewer |

## Roadmap

- [ ] Code signing / winget distribution
- [ ] NTFS transparent compression ("shrink your games 40% without deleting them")
- [ ] Cleanup basket (multi-select → one review → execute)
- [ ] MFT fast scan (WizTree-speed scanning)
- [ ] macOS & Linux polish (the code already runs there)
- [ ] Community detector rules (YAML)

## Honest limitations

It's a young tool (v0.7). The scanner walks the filesystem (minutes on a big drive, with live progress — MFT fast-scan is planned). It's tkinter, so it's functional-pretty, not Figma-pretty. Test it on data you can afford to lose first — that's good advice for *any* deletion tool, including this one.

## License

MIT. Use it, fork it, learn from it. If it saved you 50 GB, a ⭐ is the best thank-you.

---

*Every destructive action in DiskAtlas is a real, documented Windows shell call — `SHFileOperationW` with `FOF_ALLOWUNDO`, `SHEmptyRecycleBinW`, `GetDiskFreeSpaceExW` — not a `del` command behind a curtain. Read the source: it's one file.*
