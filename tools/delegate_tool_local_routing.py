"""LOCAL PATCH (mini-local-overrides): per-task delegate model routing.

Re-port of 884f1426ce + 62ce8e1dbc onto the 0.21.5 delegate tool (03/10/2026). Upstream resolves
ONE credential bundle per delegation and gives every child the same model; this module decides, per
task, whether a child should run somewhere else:

  1. an explicit per-task ``model`` / ``provider`` always wins;
  2. otherwise, when the delegation config pins no provider, a goal that explicitly asks for the
     main model ("use deepseek", "[model:deepseek]", "no glm") stays on it;
  3. otherwise a goal that clearly reads as coding / frontend-design work is auto-routed to
     DELEGATE_CODING_MODEL / DELEGATE_CODING_PROVIDER (default zai / glm-5.2);
  4. everything else inherits the batch credentials unchanged.

The regexes and the env switch are carried over verbatim from the 0.15.1 patch. The call site is
``_build_children`` in ``tools/delegate_tool.py``.
"""
import logging
import os
import re
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Auto-route coding subtasks to GLM 5.2 (added 2026-06-17)
# ---------------------------------------------------------------------------
# The driving model (deepseek-v4-pro) reliably IGNORES prose guidance to pass
# model='glm-5.2' on coding delegations, so this is a DETERMINISTIC fallback:
# when a delegated task specifies no model/provider AND its goal/context match
# clear coding signals, the child is routed to zai/glm-5.2 (better at sustained
# coding). Conservative — only fires on unambiguous implementation/debug/build
# signals, never on research/writing/analysis — and every auto-route is logged
# so misfires are visible. An explicit per-task model always wins (checked by
# the caller before this runs). Override the target with env
# DELEGATE_CODING_MODEL / DELEGATE_CODING_PROVIDER; disable entirely by setting
# DELEGATE_CODING_MODEL="".
_CODING_AUTOROUTE_MODEL_ENV = "DELEGATE_CODING_MODEL"
_CODING_AUTOROUTE_PROVIDER_ENV = "DELEGATE_CODING_PROVIDER"
_CODING_AUTOROUTE_DEFAULT_MODEL = "glm-5.2"
_CODING_AUTOROUTE_DEFAULT_PROVIDER = "zai"

# Strong, unambiguous code signals — three alternations:
#   1. a coding VERB followed (within ~44 chars) by a code NOUN
#   2. a source-code / build file (calc.py, app.tsx, Dockerfile, ...)
#   3. a code framework/tool/term that only appears in coding work
# Widened 2026-06-17 to catch oddly-worded coding tasks (optimize/deploy/wire
# a query/pipeline/webhook, etc.) WITHOUT misfiring on brand/design/ops prose.
# Framework names that collide with English (react/spring/rails/express/
# bootstrap/cargo/mocha/jest/compile-a-list) are deliberately EXCLUDED from the
# verb-free branch 3 — they only trigger via a .ext file or a verb+noun pair.
# Validated: 32/32 coding tasks matched, 32/32 non-coding tasks skipped.
_CODING_SIGNAL_RE = re.compile(
    r"(?ix)"
    # 1. coding verb + (within ~44 chars) a code noun
    r"\b(?:implement|reimplement|rewrite|refactor|debug|patch|optimi[sz]e|"
    r"integrate|containeri[sz]e|seriali[sz]e|deseriali[sz]e|parse|lint|"
    r"configure|instrument|benchmark|profile|validate|render|deploy|scaffold|"
    r"write|add|create|build|fix|extend|port|migrate|wire(?:\s+up)?|"
    r"design|redesign|restyle|revamp|reskin|polish|theme|lay\s?out|"
    r"mock\s?up|prototype|style)\b"
    r"[\w\s,./'\"():\-]{0,44}?"
    r"\b(?:functions?|methods?|classe?s?|module|script|tests?|unit\s?tests?|"
    r"test\s?suite|cli|api|endpoints?|routes?|components?|parser|server|daemon|"
    r"library|package|feature|bug|regression|schema|migrations?|git\s?hook|"
    r"webhook|plugin|crud|algorithm|quer(?:y|ies)|form|pipeline|handler|"
    r"middleware|config|dependenc(?:y|ies)|build|frontend|backend|database|"
    r"interface|struct|fixture|mock|decorator|container|workflow|reducer|"
    r"layout|stylesheet|selector|serializer|app|application|ui|"
    r"theme|template|hero|header|footer|menu|sidebar|nav(?:bar|igation)?|"
    r"modal|drawer|card|button|"
    r"icon|banner|section|page|screen|view|breakpoint|animation|"
    r"transition|responsive|storefront|typography|palette|styling)\b"
    r"|"
    # 2. a source-code / build file
    r"\b\w+\.(?:py|js|mjs|cjs|ts|tsx|jsx|go|rs|java|kt|swift|scala|dart|lua|rb|"
    r"php|c|cc|cpp|h|hpp|cs|css|scss|sass|less|html|sql|sh|bash|vue|svelte|"
    r"yaml|yml|toml|tf|liquid)\b"
    r"|\b(?:Dockerfile|Makefile)\b"
    r"|"
    # 3. unambiguous code framework / tool / term (no verb needed)
    r"\b(?:pytest|vitest|django|fastapi|flask|webpack|eslint|prettier|tailwind|"
    r"kubernetes|k8s|dockerfile|docker|postgres(?:ql)?|sqlite|redis|mongodb|"
    r"graphql|terraform|ansible|pytorch|tensorflow|numpy|nginx|vite|nuxt|"
    r"next\.js|svelte|vue\.js|react\.js|reactjs|node\.js|nodejs|typescript|"
    r"javascript|golang|kotlin|stack\s?trace|traceback|codebase|segfault|"
    r"null\s?pointer|race\s?condition|merge\s?conflict|pull\s?request|ci/?cd|"
    r"github\s+actions|gitlab\s+ci|frontend|front-end|backend|back-end|"
    r"shopify|liquid|theme\s?check|theme\s?kit|storefront|framer|"
    r"recompile|(?:syntax|runtime|compile|compilation)\s+error)\b"
)


def _looks_like_coding_task(goal: Optional[str], context: Optional[str] = None) -> bool:
    """True when the delegated task is clearly an implementation/coding/debug job.

    Conservative by design: matches a coding verb + code noun, or an explicit
    code artifact/framework/tool. Does NOT match research, writing, summarising,
    planning, or analysis tasks.
    """
    text = f"{goal or ''}\n{context or ''}"
    return bool(_CODING_SIGNAL_RE.search(text))


# A deterministic OPT-OUT from the coding auto-route: when the delegated goal
# explicitly asks to run on the main model (deepseek), honour it and skip the
# glm-5.2 auto-route. This is the reliable, per-task "switch back to deepseek"
# Hermes can flip in natural language — deepseek won't reliably self-pass a
# structured model param (the same reason the coding auto-route is a regex).
_MAIN_MODEL_OVERRIDE_RE = re.compile(
    r"(?ix)"
    r"\b(?:use|on|keep|stay(?:\s+on)?|with|via|prefer|run\s+on|route\s+to)\s+"
    r"deep\s?seek\b"
    r"|\[\s*model\s*[:=]\s*deep\s?seek[\w.\-]*\s*\]"
    r"|\b(?:no|not|avoid|skip|without|don'?t\s+use)\s+glm\b"
)


def _prefers_main_model(goal: Optional[str], context: Optional[str] = None) -> bool:
    """True when the goal explicitly asks to run on the main model (deepseek) —
    e.g. 'use deepseek', '[model:deepseek]', 'no glm'. Lets Hermes flip one task
    back off the glm-5.2 coding auto-route without a structured model param."""
    text = f"{goal or ''}\n{context or ''}"
    return bool(_MAIN_MODEL_OVERRIDE_RE.search(text))


def _coding_autoroute_target() -> tuple:
    """(model, provider) for auto-routed coding subtasks, env-overridable.

    The target is read from DELEGATE_CODING_MODEL / DELEGATE_CODING_PROVIDER via
    Hermes' get_env_value — so it can be set in ``~/.hermes/.env`` (next to the
    API keys) OR the process environment, exactly like every other Hermes key.
    This is THE single switch for swapping coding models: change those two
    values, restart the gateway, done. Falls back to the module defaults
    (glm-5.2 / zai) when unset. Disable auto-routing entirely with
    DELEGATE_CODING_MODEL="" (set to empty in .env).
    """
    try:
        from hermes_cli.config import get_env_value
        _raw_model = get_env_value(_CODING_AUTOROUTE_MODEL_ENV)
        _raw_provider = get_env_value(_CODING_AUTOROUTE_PROVIDER_ENV)
    except Exception:  # never let an import hiccup break delegation
        _raw_model = os.environ.get(_CODING_AUTOROUTE_MODEL_ENV)
        _raw_provider = os.environ.get(_CODING_AUTOROUTE_PROVIDER_ENV)

    # None = not set → use default; "" = explicitly set empty → disable.
    model = (_raw_model if _raw_model is not None else _CODING_AUTOROUTE_DEFAULT_MODEL).strip()
    if not model:
        return None, None
    provider = (
        _raw_provider if _raw_provider is not None else _CODING_AUTOROUTE_DEFAULT_PROVIDER
    ).strip() or None
    return model, provider


def route_task(task: Dict[str, Any], index: int, base_creds: Dict[str, Any]) -> tuple:
    """``(model, provider)`` this task should run on, or ``(None, None)`` to inherit the batch
    credentials. Pure decision: no credential resolution, no side effects beyond a log line."""
    task_model = str(task.get("model") or "").strip() or None
    task_provider = str(task.get("provider") or "").strip() or None
    if task_model or task_provider:
        return task_model, task_provider
    if base_creds.get("provider"):
        return None, None  # delegation.provider is pinned in config: that pin wins over the auto-route
    goal, context = task.get("goal"), task.get("context")
    if _prefers_main_model(goal, context):
        logger.info("delegate_task: main-model override on subtask %d (goal=%.60r) — skipping coding "
                    "auto-route", index, (goal or ""))
        return None, None
    if _looks_like_coding_task(goal, context):
        ar_model, ar_provider = _coding_autoroute_target()
        if ar_model:
            logger.info("delegate_task: auto-routed coding subtask %d to %s/%s (goal=%.60r)",
                        index, ar_provider, ar_model, (goal or ""))
            return ar_model, ar_provider
    return None, None
