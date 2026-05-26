"""Charge point identity resolution and Zelos trace-path sanitization.

OCPP charge points identify themselves with an operator-set string carried in
the WebSocket URL path (`ws://csms/<cp_id>`). Real-world values include
vendor serial numbers, MAC addresses, hostnames, IP addresses, or arbitrary
operator-chosen tokens — many of which contain characters that are invalid
or hostile in Zelos catalog paths (notably '.', which is the catalog
separator, and ':' / '/' / whitespace).

This module converts a raw `cp_id` into a stable, sanitized trace-path
segment, with optional operator-supplied aliases for hierarchical fleet
naming (e.g. `"AABBCCDDEEFF" -> "depot_north/bay_3"`).
"""

from __future__ import annotations

import json
import logging
import re
from pathlib import Path

logger = logging.getLogger(__name__)

MAX_SEGMENT_LEN = 64
_UNSAFE = re.compile(r"[^A-Za-z0-9_-]+")


def sanitize_segment(s: str) -> str:
    """Sanitize a single trace-path segment.

    Replaces any character outside `[A-Za-z0-9_-]` with `_`, collapses runs,
    strips leading/trailing `_`, and truncates to `MAX_SEGMENT_LEN`.
    Returns "" if nothing survives.
    """
    if not s:
        return ""
    cleaned = _UNSAFE.sub("_", s).strip("_")
    if not cleaned:
        return ""
    if len(cleaned) > MAX_SEGMENT_LEN:
        cleaned = cleaned[:MAX_SEGMENT_LEN].rstrip("_")
    return cleaned


def sanitize_path(s: str) -> str:
    """Sanitize a multi-segment alias (segments separated by `/`).

    Each segment is sanitized independently; empty segments are dropped.
    Returns "" if no segment survives.
    """
    parts = [sanitize_segment(p) for p in s.split("/")]
    parts = [p for p in parts if p]
    return "/".join(parts)


class ChargerPathResolver:
    """Map OCPP charge point IDs to Zelos trace-path segments.

    Resolution order, per `cp_id`:
      1. Explicit user alias (sanitized; may include `/` for hierarchy).
      2. Sanitized form of the raw `cp_id`.
      3. Monotonic fallback (`cp_0`, `cp_1`, ...) for IDs that sanitize away.

    Mappings are memoized so a given `cp_id` resolves to the same path for
    the lifetime of the resolver, even across reconnects.
    """

    def __init__(self, aliases: dict[str, str] | None = None) -> None:
        self._aliases: dict[str, str] = {}
        if aliases:
            for raw, alias in aliases.items():
                cleaned = sanitize_path(alias)
                if cleaned:
                    self._aliases[raw] = cleaned
                else:
                    logger.warning(f"Charger alias for {raw!r} sanitized to empty; ignoring")
        self._resolved: dict[str, str] = {}
        self._fallback_seq: int = 0

    def resolve(self, cp_id: str) -> str:
        if cp_id in self._resolved:
            return self._resolved[cp_id]

        if cp_id in self._aliases:
            path = self._aliases[cp_id]
        else:
            cleaned = sanitize_segment(cp_id)
            if cleaned:
                path = cleaned
            else:
                path = f"cp_{self._fallback_seq}"
                self._fallback_seq += 1
                logger.warning(
                    f"Charge point {cp_id!r} has no usable identifier after sanitization; "
                    f"assigned fallback path {path!r}"
                )

        self._resolved[cp_id] = path
        return path

    @property
    def mappings(self) -> dict[str, str]:
        """Snapshot of all `cp_id -> path` mappings seen so far."""
        return dict(self._resolved)

    @classmethod
    def from_file(cls, path: str | Path | None) -> ChargerPathResolver:
        if not path:
            return cls()
        p = Path(path)
        if not p.exists():
            logger.warning(f"Charger aliases file not found: {p}")
            return cls()
        try:
            with p.open() as f:
                data = json.load(f)
        except (OSError, json.JSONDecodeError) as e:
            logger.warning(f"Failed to load charger aliases from {p}: {e}")
            return cls()
        if not isinstance(data, dict):
            logger.warning(
                f"Charger aliases file {p} must contain a JSON object; got {type(data).__name__}"
            )
            return cls()
        return cls({str(k): str(v) for k, v in data.items()})
