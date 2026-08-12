import json

from onebrief.development_change_tracking import (
    development_change_fingerprint,
    discover_rejected_change_fingerprints,
    write_rejected_change_fingerprints,
)


def _change(summary: str = "repair") -> dict[str, object]:
    return {
        "summary": summary,
        "changes": [{
            "path": "Assets/UI/Lobby.cs",
            "base_sha256": None,
            "content": "class Lobby { }\n",
            "reason": summary,
        }],
    }


def test_change_fingerprint_ignores_narrative_metadata() -> None:
    assert development_change_fingerprint(_change("first")) == development_change_fingerprint(
        _change("second")
    )


def test_failed_round_delta_is_discovered_and_persisted(tmp_path) -> None:
    delta = tmp_path / "code_change_set_delta_r2.json"
    delta.write_text(json.dumps(_change()), encoding="utf-8")
    (tmp_path / "development_verification_failure_r2.txt").write_text(
        "visual failure", encoding="utf-8"
    )

    discovered = discover_rejected_change_fingerprints(tmp_path)
    assert discovered == {development_change_fingerprint(_change())}

    delta.unlink()
    (tmp_path / "development_verification_failure_r2.txt").unlink()
    write_rejected_change_fingerprints(tmp_path, discovered)
    assert discover_rejected_change_fingerprints(tmp_path) == discovered
