from __future__ import annotations

from pathlib import Path


FRONTEND = Path("src/nemotron/staff/control_plane/frontend")


def test_sherman_training_workspace_contract_is_packaged() -> None:
    index = (FRONTEND / "index.html").read_text(encoding="utf-8")
    sherman = (FRONTEND / "sherman.js").read_text(encoding="utf-8")

    assert "/ui/sherman.css" in index
    assert "/ui/sherman.js" in index
    assert "staff-sherman-trainer" in sherman
    assert "sherman-drop-zone" in sherman
    assert "skillUpload" in sherman
    assert ".zip,.rar,.7z,.tar,.tar.gz,.tgz" in sherman
    assert "تدريب المقترحين" in sherman
    assert "تدريب الجميع" in sherman
    assert "تدريب المحددين" in sherman
    for state in (
        "Uploading",
        "Scanning",
        "Validated",
        "Awaiting Approval",
        "Active",
        "Rejected",
        "Unsupported",
        "Malformed",
        "Unsafe",
        "Duplicate",
    ):
        assert state in sherman
