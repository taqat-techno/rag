"""Tray-icon rendering.

Design
------
Uses the RAGTools brand logo as the base and overlays a small round
status badge in the bottom-right corner (Slack / Discord / Zoom style).
This keeps the tray icon recognisable as "our app" at a glance while
still surfacing health state through colour.

If the logo file is missing (unusual — it ships in ``service/static/``)
we fall back to a plain coloured circle so the tray still works.

Palette
-------
Modern, saturated colours tuned to look clean on both dark and light
Windows taskbars. Aligned with Tailwind CSS 500-level stops so the
colours match anything else we might style in the admin panel.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Optional, Tuple

logger = logging.getLogger("ragtools.tray")


# Tailwind-500 palette — keeps colours consistent with the admin panel CSS.
_PALETTE: dict[str, Tuple[int, int, int]] = {
    "healthy":     (34, 197, 94),    # emerald-500 — clearly "good"
    "starting":    (234, 179, 8),    # amber-500 — attention without alarm
    "down":        (239, 68, 68),    # red-500 — "do something"
    "unreachable": (239, 68, 68),    # same red — urgency, different cause
    "unknown":     (148, 163, 184),  # slate-400 — neutral / pre-first-probe
}


def color_for(state: str) -> Tuple[int, int, int]:
    """RGB colour for the named state. Unknown kinds default to slate."""
    return _PALETTE.get(state, _PALETTE["unknown"])


# ---------------------------------------------------------------------------
# Logo loading — cached at module level so repeated icon renders are cheap
# ---------------------------------------------------------------------------


_logo_cache: dict[int, Optional["object"]] = {}


def _load_logo(size: int):
    """Load the brand logo resized to fit a ``size x size`` canvas.

    Returns a PIL Image in RGBA mode, centred on a transparent square
    canvas. Returns None if the file is missing or Pillow isn't available.
    Result is cached per requested size.
    """
    if size in _logo_cache:
        return _logo_cache[size]

    try:
        from PIL import Image  # lazy — Pillow is in the [tray] extra
    except ImportError:
        _logo_cache[size] = None
        return None

    try:
        logo_path = Path(__file__).parent / "service" / "static" / "logo.png"
        if not logo_path.is_file():
            logger.debug("Tray logo not found at %s", logo_path)
            _logo_cache[size] = None
            return None

        src = Image.open(logo_path).convert("RGBA")
        # Preserve aspect ratio; fit inside `size x size` bounding box.
        src.thumbnail((size, size), Image.Resampling.LANCZOS)

        if src.size == (size, size):
            result = src
        else:
            # Centre on a transparent canvas so the status badge always
            # lands at the same absolute corner regardless of logo shape.
            canvas = Image.new("RGBA", (size, size), (0, 0, 0, 0))
            x = (size - src.width) // 2
            y = (size - src.height) // 2
            canvas.paste(src, (x, y), src)
            result = canvas

        _logo_cache[size] = result
        return result
    except Exception as e:
        logger.debug("Could not load tray logo: %s", e)
        _logo_cache[size] = None
        return None


def _render_status_badge(draw, canvas_size: int, state: str) -> None:
    """Paint a small coloured dot with a thin white ring in the bottom-right
    corner of the canvas. Proportions tuned to be legible at 16×16 when
    Windows downsamples the 64×64 source.
    """
    # Badge roughly 38% of the icon size — big enough to be visible at
    # 16×16 but not swallowing the logo.
    badge = int(canvas_size * 0.38)
    margin = max(1, canvas_size // 32)

    # Outer ring — white, fully opaque. Gives separation from the logo
    # whatever its dominant colour is.
    outer = (
        canvas_size - badge - margin,
        canvas_size - badge - margin,
        canvas_size - margin,
        canvas_size - margin,
    )
    draw.ellipse(outer, fill=(255, 255, 255, 255))

    # Inner fill — the status colour.
    ring = max(1, canvas_size // 32)
    inner = (outer[0] + ring, outer[1] + ring, outer[2] - ring, outer[3] - ring)
    draw.ellipse(inner, fill=color_for(state) + (255,))


def _render_fallback(state: str, size: int):
    """Plain coloured circle used when the logo is unavailable."""
    from PIL import Image, ImageDraw

    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    pad = max(2, size // 10)
    bbox = (pad, pad, size - pad, size - pad)
    draw.ellipse(
        bbox,
        fill=color_for(state) + (255,),
        outline=(255, 255, 255, 255),
        width=2,
    )
    return img


def generate_icon(state: str, size: int = 64):
    """Render the tray icon for the given state. Returns a PIL Image (RGBA).

    Layered render: brand logo on a transparent canvas, with a small
    coloured dot overlay in the bottom-right corner to communicate state.

    Falls back to a plain coloured circle when the logo file is missing —
    keeps the tray functional on partial installs or tests that stub out
    static resources.

    Raises ``ImportError`` if Pillow is not installed.
    """
    from PIL import ImageDraw

    logo = _load_logo(size)
    if logo is None:
        return _render_fallback(state, size)

    # Composite on a fresh copy so the cached logo isn't mutated.
    img = logo.copy()
    draw = ImageDraw.Draw(img)
    _render_status_badge(draw, size, state)
    return img
