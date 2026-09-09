#!/usr/bin/env python3
r"""
hfcli.py — terminal test harness for the Hugging Face model layer.

Proves out: browse, search, inspect, download, list, eject.
Everything the frontend will later call, exposed as CLI commands first.

Setup:
    pip install huggingface_hub psutil

Usage:
    .venv\Scripts\python.exe hfcli.py browse                     # trending GGUF models
    .venv\Scripts\python.exe hfcli.py search qwen vision         # keyword search
    .venv\Scripts\python.exe hfcli.py info Qwen/Qwen3-VL-8B-GGUF # card + files + quants
    .venv\Scripts\python.exe hfcli.py pull Qwen/Qwen3-VL-8B-GGUF # download (auto-picks quant)
    .venv\Scripts\python.exe hfcli.py pull <repo> --quant Q4_K_M # force a quant
    .venv\Scripts\python.exe hfcli.py list                       # what's on disk
    .venv\Scripts\python.exe hfcli.py eject <repo>               # delete it
"""

import argparse
import json
import re
import shutil
import sys
from pathlib import Path

try:
    import psutil
except ImportError:
    psutil = None

from huggingface_hub import HfApi, snapshot_download, hf_hub_download
from huggingface_hub.utils import EntryNotFoundError, GatedRepoError, HfHubHTTPError

# ---------------------------------------------------------------- config

# Everything lives inside the project directory, next to this script.
APP_DIR = Path(__file__).resolve().parent
MODELS_DIR = APP_DIR / "models"
MANIFEST_PATH = APP_DIR / "manifest.json"

# Quant preference, best-to-worst. We walk this list and take the first
# one that fits comfortably in available RAM.
QUANT_PREFERENCE = ["Q8_0", "Q6_K", "Q5_K_M", "Q4_K_M", "Q4_0", "Q3_K_M", "Q2_K"]

api = HfApi()

# ---------------------------------------------------------------- helpers

def available_ram_gb() -> float:
    """Rough budget for model weights. Leaves headroom for OS + KV cache."""
    if psutil is None:
        return 8.0  # conservative default if psutil isn't installed
    total = psutil.virtual_memory().total / (1024 ** 3)
    # Reserve ~6GB for OS and apps, then keep 30% of the rest for KV cache.
    return max(2.0, (total - 6.0) * 0.7)

def human(n_bytes) -> str:
    if n_bytes is None:
        return "?"
    for unit in ("B", "KB", "MB", "GB"):
        if n_bytes < 1024:
            return f"{n_bytes:.1f}{unit}"
        n_bytes /= 1024
    return f"{n_bytes:.1f}TB"

def slug(repo_id: str) -> str:
    """Qwen/Qwen3-VL-8B-GGUF -> qwen__qwen3-vl-8b-gguf (flat, predictable)."""
    return repo_id.replace("/", "__").lower()

def detect_quant(filename: str) -> str | None:
    m = re.search(r"(Q\d+_[A-Z0-9_]+|Q\d+_\d+|F16|BF16|F32)", filename, re.I)
    return m.group(1).upper() if m else None

def is_mmproj(filename: str) -> bool:
    return "mmproj" in filename.lower()

# ---------------------------------------------------------------- commands

def cmd_browse(args):
    """List trending GGUF models — the default view of the model browser."""
    models = api.list_models(
        filter="gguf",
        sort=args.sort,

        limit=args.limit,
    )
    _print_model_list(models)

def cmd_search(args):
    """Keyword search across GGUF models."""
    query = " ".join(args.terms)
    models = api.list_models(
        search=query,
        filter="gguf",
        sort=args.sort,

        limit=args.limit,
    )
    _print_model_list(models)

def _print_model_list(models):
    rows = list(models)
    if not rows:
        print("No models found.")
        return
    print(f"\n{'REPO':<55} {'DOWNLOADS':>10} {'LIKES':>7}")
    print("-" * 75)
    for m in rows:
        dl = getattr(m, "downloads", 0) or 0
        likes = getattr(m, "likes", 0) or 0
        print(f"{m.id:<55} {dl:>10,} {likes:>7}")
    print(f"\n{len(rows)} results\n")

def cmd_info(args):
    """Show the model card and available quantizations for one repo."""
    repo = args.repo
    try:
        info = api.model_info(repo, files_metadata=True)
    except GatedRepoError:
        print(f"[gated] {repo} requires accepting a license on huggingface.co first.")
        return
    except HfHubHTTPError as e:
        print(f"[error] {e}")
        return

    print(f"\n=== {repo} ===")
    print(f"downloads: {getattr(info, 'downloads', 0):,}   likes: {getattr(info, 'likes', 0)}")
    print(f"tags: {', '.join(info.tags[:12])}")

    ggufs = [f for f in info.siblings if f.rfilename.lower().endswith(".gguf")]
    if ggufs:
        print(f"\n--- GGUF files ({len(ggufs)}) ---")
        for f in sorted(ggufs, key=lambda x: x.rfilename):
            q = detect_quant(f.rfilename) or ("mmproj" if is_mmproj(f.rfilename) else "-")
            print(f"  {f.rfilename:<50} {q:<10} {human(f.size)}")
    else:
        print("\n[warn] no .gguf files in this repo — not runnable by llama.cpp")

    # Model card — this is what we later feed to the LLM for the manifest.
    try:
        card_path = hf_hub_download(repo, "README.md", repo_type="model")
        card = Path(card_path).read_text(encoding="utf-8", errors="replace")
        print(f"\n--- MODEL CARD ({len(card)} chars) ---")
        print(card[: args.card_chars])
        if len(card) > args.card_chars:
            print(f"\n... [{len(card) - args.card_chars} more chars]")
    except EntryNotFoundError:
        print("\n[warn] no README.md in this repo")
    print()

def _choose_quant(ggufs, forced=None):
    """Pick which GGUF to pull. Returns (filename, size, reason)."""
    weights = [f for f in ggufs if not is_mmproj(f.rfilename)]
    if not weights:
        return None, None, "no weight files"

    by_quant = {}
    for f in weights:
        q = detect_quant(f.rfilename)
        if q:
            by_quant.setdefault(q, f)

    if forced:
        f = by_quant.get(forced.upper())
        if not f:
            return None, None, f"quant {forced} not in repo ({', '.join(sorted(by_quant))})"
        return f.rfilename, f.size, f"forced {forced}"

    budget_bytes = available_ram_gb() * (1024 ** 3)
    for q in QUANT_PREFERENCE:
        f = by_quant.get(q)
        if f and f.size and f.size < budget_bytes:
            return f.rfilename, f.size, f"best fit under {available_ram_gb():.1f}GB budget"

    # Nothing fits — take the smallest and warn.
    smallest = min(weights, key=lambda x: x.size or float("inf"))
    return smallest.rfilename, smallest.size, "WARNING: nothing fits budget, taking smallest"

def cmd_pull(args):
    """Download a model: weights + mmproj (if vision) + README."""
    repo = args.repo
    try:
        info = api.model_info(repo, files_metadata=True)
    except GatedRepoError:
        print(f"[gated] {repo} — accept the license on huggingface.co, then set HF_TOKEN.")
        return
    except HfHubHTTPError as e:
        print(f"[error] {e}")
        return

    ggufs = [f for f in info.siblings if f.rfilename.lower().endswith(".gguf")]
    if not ggufs:
        print("[error] no .gguf files in this repo")
        return

    chosen, size, reason = _choose_quant(ggufs, args.quant)
    if not chosen:
        print(f"[error] {reason}")
        return

    patterns = [chosen, "README.md"]
    mmprojs = [f.rfilename for f in ggufs if is_mmproj(f.rfilename)]
    if mmprojs:
        patterns.append(mmprojs[0])  # vision projector — required for VLMs

    dest = MODELS_DIR / slug(repo)
    print(f"\nrepo    : {repo}")
    print(f"weights : {chosen}  ({human(size)})")
    print(f"reason  : {reason}")
    if mmprojs:
        print(f"mmproj  : {mmprojs[0]}   <- vision model")
    print(f"dest    : {dest}\n")

    snapshot_download(
        repo_id=repo,
        local_dir=str(dest),
        allow_patterns=patterns,
    )

    _write_manifest_stub(repo, dest, chosen, mmprojs[0] if mmprojs else None, size)
    print(f"\n[ok] downloaded -> {dest}")
    print("[ok] manifest stub written (LLM enrichment happens later)")

def _write_manifest_stub(repo, dest, weight_file, mmproj_file, size):
    """
    Minimal manifest entry. The LLM card-extraction step fills in
    capabilities and the 'suited for' line later — this is just the
    structural record so the supervisor knows the model exists.
    """
    MANIFEST_PATH.parent.mkdir(parents=True, exist_ok=True)
    manifest = {}
    if MANIFEST_PATH.exists():
        manifest = json.loads(MANIFEST_PATH.read_text())

    manifest[repo] = {
        "repo_id": repo,
        "path": str(dest),
        "weight_file": weight_file,
        "mmproj_file": mmproj_file,
        "size_bytes": size,
        "quant": detect_quant(weight_file),
        "backend": "llama.cpp",
        "vision_capable": mmproj_file is not None,
        "capabilities": [],       # filled by LLM card extraction
        "suited_for": None,       # filled by LLM card extraction
        "enriched": False,
    }
    MANIFEST_PATH.write_text(json.dumps(manifest, indent=2))

def cmd_list(args):
    """Show what's downloaded."""
    if not MANIFEST_PATH.exists():
        print("No models downloaded yet.")
        return
    manifest = json.loads(MANIFEST_PATH.read_text())
    if not manifest:
        print("No models downloaded yet.")
        return

    print(f"\n{'REPO':<45} {'QUANT':<9} {'SIZE':>9} {'VISION':>7} {'ENRICHED':>9}")
    print("-" * 82)
    for repo, e in manifest.items():
        print(
            f"{repo:<45} {str(e.get('quant')):<9} {human(e.get('size_bytes')):>9} "
            f"{'yes' if e.get('vision_capable') else '-':>7} "
            f"{'yes' if e.get('enriched') else 'no':>9}"
        )
    print(f"\nmodels dir: {MODELS_DIR}\n")

def cmd_eject(args):
    """Delete a model from disk and drop it from the manifest."""
    repo = args.repo
    manifest = {}
    if MANIFEST_PATH.exists():
        manifest = json.loads(MANIFEST_PATH.read_text())

    entry = manifest.get(repo)
    dest = Path(entry["path"]) if entry else MODELS_DIR / slug(repo)

    if not dest.exists() and repo not in manifest:
        print(f"[error] {repo} is not installed")
        return

    if not args.yes:
        resp = input(f"Delete {dest} ? [y/N] ").strip().lower()
        if resp != "y":
            print("cancelled")
            return

    if dest.exists():
        shutil.rmtree(dest)
    manifest.pop(repo, None)
    MANIFEST_PATH.write_text(json.dumps(manifest, indent=2))
    print(f"[ok] ejected {repo}")

# ---------------------------------------------------------------- main

def main():
    p = argparse.ArgumentParser(description="HF model layer test harness")
    sub = p.add_subparsers(dest="cmd", required=True)

    b = sub.add_parser("browse", help="list trending GGUF models")
    b.add_argument("--limit", type=int, default=30)
    b.add_argument("--sort", default="downloads", choices=["downloads", "likes", "lastModified"])
    b.set_defaults(func=cmd_browse)

    s = sub.add_parser("search", help="keyword search")
    s.add_argument("terms", nargs="+")
    s.add_argument("--limit", type=int, default=30)
    s.add_argument("--sort", default="downloads", choices=["downloads", "likes", "lastModified"])
    s.set_defaults(func=cmd_search)

    i = sub.add_parser("info", help="show card + quants for one repo")
    i.add_argument("repo")
    i.add_argument("--card-chars", type=int, default=2000)
    i.set_defaults(func=cmd_info)

    d = sub.add_parser("pull", help="download a model")
    d.add_argument("repo")
    d.add_argument("--quant", help="force a quantization, e.g. Q4_K_M")
    d.set_defaults(func=cmd_pull)

    l = sub.add_parser("list", help="list downloaded models")
    l.set_defaults(func=cmd_list)

    e = sub.add_parser("eject", help="delete a downloaded model")
    e.add_argument("repo")
    e.add_argument("-y", "--yes", action="store_true", help="skip confirmation")
    e.set_defaults(func=cmd_eject)

    args = p.parse_args()
    try:
        args.func(args)
    except KeyboardInterrupt:
        print("\ninterrupted")
        sys.exit(130)

if __name__ == "__main__":
    main()
