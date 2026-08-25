"""
take_five/models.py

Single source of truth for which Claude model backs each call site.

Before this file existed, model IDs were hardcoded separately in images.py,
messages.py, and summaries.py. They drifted: messages.py's tool-use path
was deliberately upgraded to claude-sonnet-5, but images.py's VISION_MODEL
was left on a fully deprecated snapshot (claude-opus-4-20250514) and
started 404ing — silently breaking both medication-label detection and
document OCR until it was caught manually during testing on 2026-08-25.
Update the model for a given role here, not at the call site, so a future
upgrade can't miss a spot the way this one did.

Each constant can be overridden via an env var of the same name — e.g.
from the Render dashboard — without a code change or redeploy-from-git.
Useful for a fast swap if a model gets deprecated mid-incident. The value
below is the checked-in default; the env var, if set, wins.
"""

import os


def _model(env_var: str, default: str) -> str:
    return os.getenv(env_var, default)


# Vision analysis — medication label reads, document OCR (images.py)
VISION_MODEL = _model("VISION_MODEL", "claude-opus-4-8")

# ask_with_tools() question-answering + tool-use flow (messages.py)
TOOL_USE_MODEL = _model("TOOL_USE_MODEL", "claude-sonnet-5")

# Prep packet generation (messages.py generate_prep_packet)
PREP_PACKET_MODEL = _model("PREP_PACKET_MODEL", "claude-sonnet-4-6")

# Short structured-JSON parsing — prep request doctor/date extraction
# (messages.py parse_prep_request)
PARSE_MODEL = _model("PARSE_MODEL", "claude-haiku-4-5-20251001")

# Weekly digest generation (summaries.py)
DIGEST_MODEL = _model("DIGEST_MODEL", "claude-sonnet-4-6")

# Message chunk context summaries, for embedding (memory.py) — was already
# env-var-overridable before this file existed; moved here for consistency
# with everything else rather than keeping its own separate os.getenv call.
SUMMARY_MODEL = _model("SUMMARY_MODEL", "claude-haiku-4-5-20251001")

# Clinical signal detection from message text (signals.py)
SIGNAL_DETECTION_MODEL = _model("SIGNAL_DETECTION_MODEL", "claude-sonnet-4-6")

# Life Log topic extraction — recent-thread and durable-detail passes
# (engagement/life_log.py)
LIFE_LOG_EXTRACTION_MODEL = _model("LIFE_LOG_EXTRACTION_MODEL", "claude-sonnet-4-6")

# Post-visit "already reported organically" check (engagement/post_visit.py)
ALREADY_REPORTED_MODEL = _model("ALREADY_REPORTED_MODEL", "claude-sonnet-4-6")
