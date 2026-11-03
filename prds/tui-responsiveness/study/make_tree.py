"""Create a synthetic project of about N files, shaped like a repo with vendored deps.

Usage: python make_tree.py <root> <files>
"""

from __future__ import annotations

import pathlib
import sys


def main(root: pathlib.Path, limit: int) -> int:
    root.mkdir(parents=True, exist_ok=True)
    (root / ".gitignore").write_text("node_modules/\n")
    made = 0
    for top in ("src", "node_modules", "tests", "assets"):
        for a in range(30):
            for b in range(30):
                directory = root / top / f"pkg{a}" / f"mod{b}"
                directory.mkdir(parents=True, exist_ok=True)
                for c in range(10):
                    text = f"# {top} {a} {b} {c}\nVALUE = {c}\n" * 20
                    (directory / f"file{c}.py").write_text(text)
                    made += 1
                    if made >= limit:
                        return made
    return made


if __name__ == "__main__":
    print(main(pathlib.Path(sys.argv[1]), int(sys.argv[2])))
