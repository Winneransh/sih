#!/usr/bin/env python3
r"""
engine.py — llama-server supervisor + manifest enrichment.

This is the runtime layer. It spawns llama-server processes for downloaded
models, assigns ports, health-checks them, and proves they answer.

Setup:
    Download a llama.cpp release build and put llama-server.exe on PATH.
    .venv\Scripts\python.exe -m pip install httpx pyyaml

Usage:
    .venv\Scripts\python.exe engine.py enrich                      # fill manifest from README frontmatter
    .venv\Scripts\python.exe engine.py test <repo>                 # spawn, ask a question, kill
    .venv\Scripts\python.exe engine.py test <repo> --image pic.png # vision test
    .venv\Scripts\python.exe engine.py serve <repo>                # spawn and stay up (Ctrl-C to stop)
    .venv\Scripts\python.exe engine.py serve <repo> --port 8081
    .venv\Scripts\python.exe engine.py ps                          # what's running
"""

import argparse
import json
import os
import signal
import socket
import subprocess
import sys
import time
import base64
import re
from pathlib import Path

import httpx
import yaml

APP_DIR = Path(__file__).resolve().parent
MODELS_DIR = APP_DIR / "models"
MANIFEST_PATH = APP_DIR / "manifest.json"
RUNTIME_PATH = APP_DIR / "runtime.json"   # which model is on which port

LLAMA_SERVER = os.environ.get("LLAMA_SERVER", "llama-server.exe")
DEFAULT_CTX = 8192
HEALTH_TIMEOUT = 300      # big models take a while to load
HEALTH_POLL = 1.0


# ---------------------------------------------------------------- manifest

def load_manifest() -> dict:
    if not MANIFEST_PATH.exists():
        return {}
    return json.loads(MANIFEST_PATH.read_text())


def save_manifest(m: dict):
    MANIFEST_PATH.write_text(json.dumps(m, indent=2))


def parse_frontmatter(readme_path: Path) -> dict:
    """
    Pull the YAML block between the leading --- markers.
    This is where the hard facts live: pipeline_tag, license, base_model, tags.
    No LLM needed — it's structured already.
    """
    if not readme_path.exists():
        return {}
    text = readme_path.read_text(encoding="utf-8", errors="replace")
    m = re.match(r"^---\s*\n(.*?)\n---\s*\n", text, re.S)
    if not m:
        return {}
    try:
        return yaml.safe_load(m.group(1)) or {}
    except yaml.YAMLError:
        return {}


# pipeline_tag -> capability list
PIPELINE_CAPS = {
    "text-generation": ["text"],
    "image-text-to-text": ["text", "vision"],
    "visual-question-answering": ["text", "vision"],
    "feature-extraction": ["embedding"],
    "sentence-similarity": ["embedding"],
    "text-ranking": ["reranker"],
    "automatic-speech-recognition": ["audio"],
}


def infer_capabilities(fm: dict, entry: dict) -> list:
    caps = []

    tag = fm.get("pipeline_tag")
    if tag in PIPELINE_CAPS:
        caps.extend(PIPELINE_CAPS[tag])

    # An mmproj file on disk is hard evidence of vision, regardless of tags.
    if entry.get("mmproj_file") and "vision" not in caps:
        caps.append("vision")

    # Tags and repo name are weaker signals, used only to add specialisations.
    haystack = " ".join(str(t) for t in (fm.get("tags") or []))
    haystack += " " + entry.get("repo_id", "")
    low = haystack.lower()

    if "coder" in low or "code" in low:
        caps.append("coding")
    if "embed" in low and "embedding" not in caps:
        caps.append("embedding")
    if "rerank" in low and "reranker" not in caps:
        caps.append("reranker")

    if not caps:
        caps = ["text"]

    # dedupe, keep order
    seen, out = set(), []
    for c in caps:
        if c not in seen:
            seen.add(c)
            out.append(c)
    return out


def cmd_enrich(args):
    """Fill capabilities/license/base_model in the manifest from README frontmatter."""
    manifest = load_manifest()
    if not manifest:
        print("No models in manifest. Pull one first.")
        return

    for repo, entry in manifest.items():
        readme = Path(entry["path"]) / "README.md"
        fm = parse_frontmatter(readme)

        entry["capabilities"] = infer_capabilities(fm, entry)
        entry["pipeline_tag"] = fm.get("pipeline_tag")
        entry["license"] = fm.get("license")
        base = fm.get("base_model")
        entry["base_model"] = base[0] if isinstance(base, list) and base else base
        entry["hf_tags"] = [str(t) for t in (fm.get("tags") or [])][:15]
        entry["enriched"] = True

        print(f"{repo}")
        print(f"  capabilities : {', '.join(entry['capabilities'])}")
        print(f"  pipeline_tag : {entry['pipeline_tag']}")
        print(f"  license      : {entry['license']}")
        print(f"  base_model   : {entry['base_model']}")
        print()

    save_manifest(manifest)
    print(f"[ok] manifest updated -> {MANIFEST_PATH}")


# ---------------------------------------------------------------- runtime

def free_port(start=8080) -> int:
    for p in range(start, start + 200):
        with socket.socket() as s:
            if s.connect_ex(("127.0.0.1", p)) != 0:
                return p
    raise RuntimeError("no free port")


def resolve(repo: str) -> dict:
    manifest = load_manifest()
    entry = manifest.get(repo)
    if not entry:
        # allow partial match so you don't have to type the full repo id
        matches = [k for k in manifest if repo.lower() in k.lower()]
        if len(matches) == 1:
            entry = manifest[matches[0]]
        elif len(matches) > 1:
            raise SystemExit(f"ambiguous: {', '.join(matches)}")
        else:
            raise SystemExit(f"not installed: {repo}")
    return entry


def build_cmd(entry: dict, port: int, ctx: int) -> list:
    model_dir = Path(entry["path"])
    cmd = [
        LLAMA_SERVER,
        "--model", str(model_dir / entry["weight_file"]),
        "--port", str(port),
        "--host", "127.0.0.1",
        "--ctx-size", str(ctx),
    ]
    if entry.get("mmproj_file"):
        cmd += ["--mmproj", str(model_dir / entry["mmproj_file"])]

    # Endpoint-enabling flags. /v1/embeddings and /v1/rerank do not exist
    # unless the server is started with these.
    caps = entry.get("capabilities") or []
    if "embedding" in caps:
        cmd += ["--embedding"]
    if "reranker" in caps:
        cmd += ["--reranking"]
    return cmd


def wait_healthy(port: int, proc, timeout=HEALTH_TIMEOUT) -> bool:
    """Poll /health until the model finishes loading."""
    url = f"http://127.0.0.1:{port}/health"
    deadline = time.time() + timeout
    while time.time() < deadline:
        if proc.poll() is not None:
            return False          # process died
        try:
            r = httpx.get(url, timeout=2.0)
            if r.status_code == 200:
                return True
        except httpx.HTTPError:
            pass
        time.sleep(HEALTH_POLL)
    return False


def spawn(entry: dict, port: int, ctx: int, quiet=True, detached=False, log_path=None):
    """
    detached=True puts the server in its own process group so it survives
    this script exiting — that is what makes a persistent pool possible.
    """
    cmd = build_cmd(entry, port, ctx)
    print(f"[spawn] {' '.join(cmd)}\n")

    if log_path:
        log_path.parent.mkdir(parents=True, exist_ok=True)
        out = open(log_path, "ab")
    else:
        out = subprocess.DEVNULL if quiet else None

    flags = 0
    if detached:
        flags = subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.DETACHED_PROCESS

    proc = subprocess.Popen(
        cmd,
        stdout=out,
        stderr=out,
        creationflags=flags,
    )
    return proc


def record_runtime(repo, port, pid):
    rt = json.loads(RUNTIME_PATH.read_text()) if RUNTIME_PATH.exists() else {}
    rt[repo] = {"port": port, "pid": pid}
    RUNTIME_PATH.write_text(json.dumps(rt, indent=2))


def clear_runtime(repo):
    if not RUNTIME_PATH.exists():
        return
    rt = json.loads(RUNTIME_PATH.read_text())
    rt.pop(repo, None)
    RUNTIME_PATH.write_text(json.dumps(rt, indent=2))


# ---------------------------------------------------------------- commands

def cmd_serve(args):
    """Spawn and hold. Ctrl-C to stop."""
    entry = resolve(args.repo)
    port = args.port or free_port()
    proc = spawn(entry, port, args.ctx, quiet=False)

    print(f"[wait] loading {entry['repo_id']} ...")
    if not wait_healthy(port, proc):
        print("[fail] never became healthy")
        proc.terminate()
        return

    record_runtime(entry["repo_id"], port, proc.pid)
    print(f"\n[ok] serving on http://127.0.0.1:{port}")
    print(f"[ok] try: curl http://127.0.0.1:{port}/v1/models\n")
    try:
        proc.wait()
    except KeyboardInterrupt:
        print("\n[stop] shutting down")
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
        clear_runtime(entry["repo_id"])


def cmd_test(args):
    """Spawn, send one request, print the answer, kill. The full proof."""
    entry = resolve(args.repo)
    port = args.port or free_port()
    proc = spawn(entry, port, args.ctx)

    t0 = time.time()
    print(f"[wait] loading {entry['repo_id']} ...")
    if not wait_healthy(port, proc):
        print("[fail] server never became healthy — rerun with `serve` to see its logs")
        proc.terminate()
        return
    print(f"[ok] healthy in {time.time() - t0:.1f}s\n")

    # Build the message — text, or text+image for vision models.
    if args.image:
        img = Path(args.image)
        if not img.exists():
            print(f"[fail] no such image: {img}")
            proc.terminate()
            return
        b64 = base64.b64encode(img.read_bytes()).decode()
        ext = img.suffix.lstrip(".").lower() or "png"
        content = [
            {"type": "text", "text": args.prompt},
            {"type": "image_url",
             "image_url": {"url": f"data:image/{ext};base64,{b64}"}},
        ]
    else:
        content = args.prompt

    print(f"> {args.prompt}\n")
    t1 = time.time()
    try:
        r = httpx.post(
            f"http://127.0.0.1:{port}/v1/chat/completions",
            json={
                "messages": [{"role": "user", "content": content}],
                "max_tokens": args.max_tokens,
                "temperature": 0.3,
            },
            timeout=300.0,
        )
        r.raise_for_status()
        data = r.json()
        print(data["choices"][0]["message"]["content"])
        usage = data.get("usage", {})
        elapsed = time.time() - t1
        print(f"\n[stats] {elapsed:.1f}s  "
              f"prompt={usage.get('prompt_tokens', '?')} "
              f"completion={usage.get('completion_tokens', '?')}")
    except Exception as e:
        print(f"[fail] request error: {e}")
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
        print("\n[ok] server stopped")


# ---------------------------------------------------------------- pool

def load_runtime() -> dict:
    if not RUNTIME_PATH.exists():
        return {}
    return json.loads(RUNTIME_PATH.read_text())


def is_alive(port: int) -> bool:
    try:
        r = httpx.get(f"http://127.0.0.1:{port}/health", timeout=1.5)
        return r.status_code == 200
    except httpx.HTTPError:
        return False


def pid_running(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        return True
    except (OSError, ProcessLookupError):
        return False


def reap_dead():
    """Drop runtime entries whose process is gone. Keeps the file honest."""
    rt = load_runtime()
    live = {r: i for r, i in rt.items() if pid_running(i["pid"])}
    if len(live) != len(rt):
        RUNTIME_PATH.write_text(json.dumps(live, indent=2))
    return live


def port_for(repo_id: str):
    """Router primitive: model name in, port out."""
    rt = reap_dead()
    info = rt.get(repo_id)
    return info["port"] if info else None


def cmd_up(args):
    """Start one or more models and leave them running."""
    rt = reap_dead()
    started = []

    for name in args.repos:
        entry = resolve(name)
        repo_id = entry["repo_id"]

        if repo_id in rt and is_alive(rt[repo_id]["port"]):
            print(f"[skip] {repo_id} already up on :{rt[repo_id]['port']}\n")
            continue

        used = {i["port"] for i in rt.values()}
        port = free_port()
        while port in used:
            port = free_port(port + 1)

        log = APP_DIR / "logs" / f"{slugify(repo_id)}.log"
        proc = spawn(entry, port, args.ctx, detached=True, log_path=log)
        print(f"[wait] loading {repo_id} ...")

        if not wait_healthy(port, proc):
            print(f"[fail] {repo_id} never became healthy — see {log}\n")
            proc.terminate()
            continue

        rt[repo_id] = {"port": port, "pid": proc.pid,
                       "caps": entry.get("capabilities", [])}
        RUNTIME_PATH.write_text(json.dumps(rt, indent=2))
        started.append((repo_id, port))
        print(f"[ok] {repo_id} -> :{port}  (pid {proc.pid})\n")

    if started:
        print("Started:")
        for repo, port in started:
            print(f"  {repo}  ->  http://127.0.0.1:{port}")
        print()


def cmd_down(args):
    """Stop one model, or all of them."""
    rt = load_runtime()
    if not rt:
        print("Nothing running.")
        return

    targets = list(rt.keys())
    if args.repo:
        targets = [r for r in rt if args.repo.lower() in r.lower()]
        if not targets:
            print(f"[error] no running model matching '{args.repo}'")
            return

    for repo in targets:
        pid = rt[repo]["pid"]
        try:
            os.kill(pid, signal.SIGTERM)
            print(f"[stop] {repo} (pid {pid})")
        except ProcessLookupError:
            print(f"[gone] {repo} (pid {pid} already dead)")
        rt.pop(repo)

    RUNTIME_PATH.write_text(json.dumps(rt, indent=2))


def cmd_ps(args):
    """Show what is running, on which port, and whether it answers."""
    rt = reap_dead()
    if not rt:
        print("Nothing running.")
        return

    print(f"\n{'REPO':<45} {'PORT':>6} {'PID':>8} {'HEALTH':>8}  CAPABILITIES")
    print("-" * 92)
    for repo, info in rt.items():
        health = "ok" if is_alive(info["port"]) else "DEAD"
        caps = ", ".join(info.get("caps", []))
        print(f"{repo:<45} {info['port']:>6} {info['pid']:>8} {health:>8}  {caps}")

    if psutil:
        mem = psutil.virtual_memory()
        print(f"\nRAM: {(mem.total - mem.available) / 1024**3:.1f}GB used / "
              f"{mem.total / 1024**3:.1f}GB total")
    print()


def cmd_ask(args):
    """Send a prompt to an already-running model. Proves the pool is usable."""
    rt = reap_dead()
    matches = [r for r in rt if args.repo.lower() in r.lower()]
    if not matches:
        print(f"[error] '{args.repo}' is not running. Start it with: engine.py up {args.repo}")
        return
    if len(matches) > 1:
        print(f"[error] ambiguous: {', '.join(matches)}")
        return

    repo = matches[0]
    port = rt[repo]["port"]

    if args.image:
        img = Path(args.image)
        if not img.exists():
            print(f"[fail] no such image: {img}")
            return
        b64 = base64.b64encode(img.read_bytes()).decode()
        ext = img.suffix.lstrip(".").lower() or "png"
        content = [
            {"type": "text", "text": args.prompt},
            {"type": "image_url", "image_url": {"url": f"data:image/{ext};base64,{b64}"}},
        ]
    else:
        content = args.prompt

    print(f"[route] {repo} -> :{port}\n> {args.prompt}\n")
    t0 = time.time()
    try:
        r = httpx.post(
            f"http://127.0.0.1:{port}/v1/chat/completions",
            json={"messages": [{"role": "user", "content": content}],
                  "max_tokens": args.max_tokens, "temperature": 0.3},
            timeout=300.0,
        )
        r.raise_for_status()
        data = r.json()
        print(data["choices"][0]["message"]["content"])
        u = data.get("usage", {})
        print(f"\n[stats] {time.time() - t0:.1f}s  "
              f"prompt={u.get('prompt_tokens', '?')} completion={u.get('completion_tokens', '?')}")
    except Exception as e:
        print(f"[fail] {e}")


def slugify(repo_id: str) -> str:
    return repo_id.replace("/", "__").lower()


def main():
    p = argparse.ArgumentParser(description="llama-server supervisor")
    sub = p.add_subparsers(dest="cmd", required=True)

    e = sub.add_parser("enrich", help="fill manifest from README frontmatter")
    e.set_defaults(func=cmd_enrich)

    t = sub.add_parser("test", help="spawn, ask one question, kill")
    t.add_argument("repo")
    t.add_argument("--prompt", default="In one sentence, what are you?")
    t.add_argument("--image", help="path to an image (vision models)")
    t.add_argument("--port", type=int)
    t.add_argument("--ctx", type=int, default=DEFAULT_CTX)
    t.add_argument("--max-tokens", type=int, default=256)
    t.set_defaults(func=cmd_test)

    s = sub.add_parser("serve", help="spawn and stay up")
    s.add_argument("repo")
    s.add_argument("--port", type=int)
    s.add_argument("--ctx", type=int, default=DEFAULT_CTX)
    s.set_defaults(func=cmd_serve)

    ps = sub.add_parser("ps", help="list running servers")
    ps.set_defaults(func=cmd_ps)

    up = sub.add_parser("up", help="start one or more models and leave them running")
    up.add_argument("repos", nargs="+")
    up.add_argument("--ctx", type=int, default=DEFAULT_CTX)
    up.set_defaults(func=cmd_up)

    dn = sub.add_parser("down", help="stop a model (or all if no name given)")
    dn.add_argument("repo", nargs="?")
    dn.set_defaults(func=cmd_down)

    a = sub.add_parser("ask", help="send a prompt to an already-running model")
    a.add_argument("repo")
    a.add_argument("prompt")
    a.add_argument("--image")
    a.add_argument("--max-tokens", type=int, default=256)
    a.set_defaults(func=cmd_ask)

    args = p.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
