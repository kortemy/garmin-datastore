"""M5 live check: run one write tool against the real account from this machine.

    uv run python scripts/m5_live_test.py <tool> '<json kwargs>'

Same code path as the connector (gds.writes → broker client → write_log → resync), minus
OAuth. Use clearly labelled test items and undo them afterwards (undo_write).
"""

import json
import sys
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from gds.cli import load_env_into_process  # noqa: E402

load_env_into_process()

from gds import writes  # noqa: E402


def main() -> None:
    tool, kwargs = sys.argv[1], json.loads(sys.argv[2]) if len(sys.argv) > 2 else {}
    fn = getattr(writes, tool)
    if tool == "recent_writes":
        out = fn(**kwargs)
    else:
        kwargs.setdefault("key", str(uuid.uuid4()))
        kwargs.setdefault("dry_run", False)
        out = fn(**kwargs)
    print(json.dumps(out, indent=2, default=str, ensure_ascii=False))


if __name__ == "__main__":
    main()
