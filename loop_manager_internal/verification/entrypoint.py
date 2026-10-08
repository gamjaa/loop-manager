"""Absolute runtime entry point; project interpreters need no installed package."""
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

if __name__ == "__main__":
    from loop_manager_internal.verification.runner import main
    try:
        raise SystemExit(main())
    except Exception:
        import traceback
        traceback.print_exc()
        raise SystemExit(2)
