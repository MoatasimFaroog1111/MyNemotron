from __future__ import annotations

from pathlib import Path


FRONTEND = (
    Path(__file__).resolve().parents[2]
    / "src"
    / "nemotron"
    / "staff"
    / "control_plane"
    / "frontend"
)


def test_official_frontend_has_responsive_contract() -> None:
    html = (FRONTEND / "index.html").read_text(encoding="utf-8")
    css = (FRONTEND / "app.css").read_text(encoding="utf-8")

    # Mobile viewport and a scroll-safe stage shell are non-negotiable.
    assert 'name="viewport"' in html
    assert "width=device-width" in html
    assert 'class="stage-shell"' in html

    # The image coordinate system must remain stable so percentage hotspots
    # continue to align with the 1080x832 team scene at every viewport size.
    assert "aspect-ratio:1080/832" in css
    assert "overflow-x:hidden" in css

    # Safe-area support keeps controls usable on notched mobile devices.
    assert "safe-area-inset-top" in css
    assert "safe-area-inset-bottom" in css

    # Explicit desktop/tablet/mobile/narrow-mobile/landscape contracts.
    assert "@media(max-width:1024px)" in css
    assert "@media(max-width:780px)" in css
    assert "@media(max-width:480px)" in css
    assert "@media(max-width:360px)" in css
    assert "@media(orientation:landscape)" in css

    # Accessibility preference must not be overridden by animation.
    assert "@media(prefers-reduced-motion:reduce)" in css
