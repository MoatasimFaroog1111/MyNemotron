# Official Staff OS responsive contract

The official browser UI is a Presentation/BFF surface over the Control Plane. It must remain usable on desktop, tablet, mobile, narrow mobile, and short landscape viewports without changing the 1080x832 image coordinate system used by percentage-based staff hotspots.

Acceptance requirements are enforced in `tests/staff_core/test_frontend_responsive_contract.py`:

- mobile viewport metadata
- stable `1080/832` stage aspect ratio
- no page-level horizontal overflow
- safe-area support
- explicit tablet/mobile/narrow-mobile/landscape breakpoints
- reduced-motion accessibility

The browser remains read-only for sensitive actions and must never receive provider credentials or the long-lived Control Plane bearer token after session establishment.
