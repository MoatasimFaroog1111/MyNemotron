# Responsive Contract

The official MyNemotron Staff OS frontend is responsive by contract, not by visual convention.

- The canonical hotspot coordinate system is always 1080 × 832.
- The team image may be web-optimized, but it is rendered inside that canonical aspect ratio.
- Hotspots use percentage coordinates so they remain aligned across viewport sizes.
- Desktop, tablet, mobile, narrow-mobile, and landscape breakpoints are explicit.
- The layout must never introduce horizontal page overflow.
- Safe-area insets are respected on mobile devices.
- Motion is reduced when the operating system requests `prefers-reduced-motion`.
- Sensitive actions remain outside the browser UI until explicit human identity, authorization, CSRF, and audit controls are proven.
