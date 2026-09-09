"""Paths and constants. Everything lives under the project directory."""

from pathlib import Path
import os

# In development this is the project directory. Once frozen, the executable
# lives under Program Files, which is read-only, so the packaged
# entry point sets WORKBENCH_DATA_DIR to a per-user location instead.
APP_DIR = Path(os.environ.get("WORKBENCH_DATA_DIR")
               or Path(__file__).resolve().parent.parent)

MODELS_DIR = APP_DIR / "models"
MANIFEST_PATH = APP_DIR / "manifest.json"
RUNTIME_PATH = APP_DIR / "runtime.json"
LOGS_DIR = APP_DIR / "logs"
SESSIONS_DIR = APP_DIR / "sessions"      # per-session working + output files
KB_DIR = APP_DIR / "kb"                  # vector index + ingested docs
AUDIT_PATH = APP_DIR / "audit.jsonl"
TEMPLATES_DIR = APP_DIR / "templates"

for d in (MODELS_DIR, LOGS_DIR, SESSIONS_DIR, KB_DIR, TEMPLATES_DIR):
    d.mkdir(parents=True, exist_ok=True)

LLAMA_SERVER = os.environ.get("LLAMA_SERVER", "llama-server.exe")

DEFAULT_CTX = 8192
HEALTH_TIMEOUT = 300
REQUEST_TIMEOUT = 600.0

# Hard ceiling on what an agent may hand back to the planner. Bulk content
# goes to disk and travels as a path. This single rule is what keeps the
# planner's context small no matter how much data is being processed.
MAX_AGENT_RESULT_CHARS = 4000

# Safety rails on plan execution.
MAX_PLAN_STEPS = 12
MAX_FANOUT = 50
DEFAULT_MAX_PARALLEL = 3
MAX_REPLANS = 1

# Capabilities a model can declare in the manifest.
CAPABILITIES = ["text", "vision", "coding", "embedding", "reranker", "audio"]

# What each agent needs from a model. The planner picks the actual model.
AGENT_CAPABILITY = {
    "vision": "vision",
    "kb": "text",
    "code": "coding",
    "calc": "text",
    "docx": "text",
    "pptx": "text",
    "xlsx": "text",
}
