"""Read-only report snapshot supplied only to already-authorized planning."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
from uuid import uuid4


def planning_context(reports: list[dict[str, object]], state_dir: Path) -> str:
    if not reports:
        return ""
    content = json.dumps({"version": 1, "reports": reports}, ensure_ascii=False,
                         sort_keys=True, indent=2).encode("utf-8")
    identity = hashlib.sha256(content).hexdigest()
    directory = state_dir / "common-defect-evidence"
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / (identity + ".json")
    temporary = path.with_name("." + identity + "-" + uuid4().hex)
    try:
        temporary.write_bytes(content)
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)
    return ("\n\n공통 결함의 미확정 보고 snapshot: " + str(path)
            + "\nSHA-256: " + identity + "\n보고 수: " + str(len(reports))
            + "\n현재 catalog·명세와 함께 공통 결함 보고 지침에 따라 검토하라.\n")
