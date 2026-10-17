"""Print the key structure of captured payloads: path, type, and a short sample."""

import json
import sys
from pathlib import Path


def walk(obj, path="", depth=0, out=None, max_depth=4):
    out = [] if out is None else out
    if isinstance(obj, dict):
        for k, v in obj.items():
            p = f"{path}.{k}" if path else k
            if isinstance(v, (dict, list)) and depth < max_depth:
                out.append((p, type(v).__name__, len(v)))
                walk(v, p, depth + 1, out, max_depth)
            else:
                out.append((p, type(v).__name__, repr(v)[:40]))
    elif isinstance(obj, list) and obj:
        first = obj[0]
        if isinstance(first, (dict, list)):
            walk(first, path + "[0]", depth + 1, out, max_depth)
        else:
            out.append((path + "[0]", type(first).__name__, repr(first)[:40]))
    return out


def main() -> None:
    folder, date = Path(sys.argv[1]), sys.argv[2]
    only = set(sys.argv[3].split(",")) if len(sys.argv) > 3 else None
    for f in sorted(folder.glob(f"*__{date}.json")):
        name = f.name.split("__")[0]
        if only and name not in only:
            continue
        data = json.loads(f.read_text(encoding="utf-8"))
        print(f"\n### {name}  ({type(data).__name__})")
        for p, t, s in walk(data):
            print(f"  {p}  <{t}> {s}")


if __name__ == "__main__":
    main()
