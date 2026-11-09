#!/usr/bin/env python3
"""Read-only pre-publish check. Run from anywhere: python scripts/check_public.py

Fails (exit 1) when the checkout contains real company identifiers, credentials,
runtime databases or development-machine absolute paths.
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TEXT_EXT = {
    ".py", ".js", ".css", ".html", ".json", ".md", ".yml", ".yaml",
    ".txt", ".bat", ".svg", ".cfg", ".ini", ".mjs",
}
# Patterns are assembled from fragments so this checker never contains the
# literal identifiers it looks for, and the checker skips its own file.
WORDS = [
    "北京" + "埃诺登", "北京" + "卓美", "资阳" + "卓美", "广州" + "景安宏悦",
    "景安" + "宏悦", "埃" + "诺登", "卓" + "美",
    "zar" + "wdw", "w7s" + "uta", "zyzm." + "ik3cloud.com",
    "fQm0" + "PPACPbh2F9IsNQgX",
]
PATTERNS = [("真实公司/内部标识", re.compile(re.escape(w))) for w in WORDS]
PATTERNS += [
    ("开发机绝对路径", re.compile(r"[A-Za-z]:[\\/](?:python|Users[\\/]Administrator)[\\/]", re.I)),
    ("疑似令牌", re.compile(r"(?:ghp_|github_pat_|sk-[A-Za-z0-9]{20,}|AKIA[0-9A-Z]{16})")),
]
BANNED_NAMES = re.compile(r"^(state\.db.*|state\.key|.*\.db-shm$|.*\.db-wal$|portal_audit\.log|.*\.log$)", re.I)
BANNED_DIRS = {".git", ".portal-state-dsh", "artifacts", "__pycache__", ".pytest_cache", "node_modules", ".venv"}
SKIP_FILES = {"LICENSE", "vendor/kingdee-data-exporter-LICENSE.md", "scripts/check_public.py"}

findings: list[str] = []
text_files = 0

for path in sorted(ROOT.rglob("*")):
    rel = path.relative_to(ROOT).as_posix()
    if rel in SKIP_FILES or rel.startswith("vendor/kingdee-data-exporter-LICENSE.md"):
        continue
    if path.is_dir():
        continue
    if any(part in BANNED_DIRS for part in path.relative_to(ROOT).parts):
        continue
    if BANNED_NAMES.match(path.name):
        findings.append(f"禁止入库的运行/日志文件: {rel}")
        continue
    if path.suffix.lower() not in TEXT_EXT:
        continue
    try:
        text = path.read_text(encoding="utf-8")
    except (UnicodeDecodeError, OSError):
        continue
    text_files += 1
    for lineno, line in enumerate(text.splitlines(), 1):
        for label, pattern in PATTERNS:
            if pattern.search(line):
                findings.append(f"{label}: {rel}:{lineno}: {line.strip()[:120]}")

print(f"扫描文本文件 {text_files} 个，根目录 {ROOT}")
if findings:
    print(f"发现 {len(findings)} 处问题：")
    for item in findings:
        print("  -", item)
    sys.exit(1)
print("PASS：未发现真实公司名、凭据、运行数据或开发机绝对路径")
