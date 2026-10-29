"""``python -m videodub`` 等价于 ``videodub``。"""

from __future__ import annotations

from .cli import main

if __name__ == "__main__":
    raise SystemExit(main())
