"""
Router.

The planner names a model. The router turns that name into a port,
starting the model if it isn't running. That is the whole job — no
task reasoning happens here.

The fallback path exists because a planner running on a small model
will occasionally name a model that isn't installed. Rather than
failing the whole plan, we substitute a model with the right
capability and record the substitution in the audit log.
"""

from . import manifest, supervisor
from .audit import log_event


class RouteError(Exception):
    pass


def resolve_port(model_name: str) -> tuple[str, int]:
    """Returns (actual_model_used, port)."""
    entry = manifest.resolve(model_name)
    rec = supervisor.start(entry["repo_id"])
    return entry["repo_id"], rec["port"]


def route(model_name: str | None, capability: str) -> tuple[str, int]:
    """
    Preferred path: the planner named a model, we use it.
    Fallback: pick any installed model with the required capability.
    """
    if model_name:
        try:
            model, port = resolve_port(model_name)
            return model, port
        except Exception as e:
            log_event("route.fallback", requested=model_name,
                      capability=capability, reason=str(e))

    candidates = manifest.by_capability(capability)
    if not candidates:
        raise RouteError(
            f"no installed model provides '{capability}'. "
            f"Download one, then run enrich."
        )

    # Prefer something already running — avoids a cold load mid-plan.
    running = {s["model"] for s in supervisor.status() if s["healthy"]}
    for c in candidates:
        if c in running:
            return resolve_port(c)

    return resolve_port(candidates[0])
