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
    js = (FRONTEND / "app.js").read_text(encoding="utf-8")

    # Mobile viewport and the full-screen office shell are non-negotiable.
    assert 'name="viewport"' in html
    assert "width=device-width" in html
    assert 'class="stage-shell"' in html
    assert "width:100vw" in css
    assert "height:100dvh" in css
    assert "object-fit:cover" in css
    assert "overflow:hidden" in css

    # Cover crops the source image differently per viewport. Hotspots therefore
    # must be re-projected from the stable 1080x832 source coordinate system.
    assert "OFFICE_SOURCE_WIDTH=1080" in js
    assert "OFFICE_SOURCE_HEIGHT=832" in js
    assert "Math.max(width/OFFICE_SOURCE_WIDTH,height/OFFICE_SOURCE_HEIGHT)" in js
    assert "offsetX=(width-renderedWidth)/2" in js
    assert "offsetY=(height-renderedHeight)/2" in js

    # Person buttons remain wired to the real governed Staff workspace API.
    assert "/ui/api/staff/${encodeURIComponent(member.staff_id)}/workspace" in js

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
