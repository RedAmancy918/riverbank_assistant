#!/usr/bin/env python3
"""Regression checks for the read-only RiverBank task report catalogue."""

from __future__ import annotations

import os
import tempfile
from pathlib import Path

from report_library import ReportLibrary


def main() -> None:
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        older = root / "2026-08-27_old.md"
        newer_dir = root / "news"
        newer_dir.mkdir()
        newer = newer_dir / "2026-08-28_robotics.md"
        hidden = root / ".hidden.md"
        ignored = root / "notes.txt"
        older.write_text("# 旧报告\n\n较早内容。\n", encoding="utf-8")
        newer.write_text(
            "---\nkind: report\n---\n# 机器人新闻热点\n\n"
            "这是报告摘要，包含 [来源](https://example.com)。\n\n## 详情\n更多内容。\n",
            encoding="utf-8",
        )
        hidden.write_text("# 不公开\n", encoding="utf-8")
        ignored.write_text("not a report\n", encoding="utf-8")
        os.utime(older, (100, 100))
        os.utime(newer, (200, 200))

        library = ReportLibrary(root)
        reports = library.list_reports()
        assert len(reports) == 2
        assert reports[0]["title"] == "机器人新闻热点"
        assert reports[0]["summary"] == "这是报告摘要，包含 来源。"
        assert reports[0]["relative_path"] == "news/2026-08-28_robotics.md"
        path, content = library.read_report(reports[0]["id"])
        assert path == newer.resolve()
        assert "## 详情" in content
        assert ReportLibrary.decode_id(reports[0]["id"]) == reports[0]["relative_path"]

        outside = root.parent / "outside.md"
        outside.write_text("# outside\n", encoding="utf-8")
        try:
            library.resolve(ReportLibrary.encode_id("../outside.md"))
        except ValueError:
            pass
        else:
            raise AssertionError("path traversal must be rejected")
        outside.unlink()

    print("report library: regression checks passed")


if __name__ == "__main__":
    main()
