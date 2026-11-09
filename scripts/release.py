"""Advance one decimal release: 1.5.9 -> 1.6.0; 1.9.9 -> 2.0.0."""
import json
import re
from pathlib import Path


def next_version(version):
    if not re.fullmatch(r"[0-9]\.[0-9]\.[0-9]", version):
        raise ValueError("Version must contain three single decimal digits")
    value = int(version.replace(".", "")) + 1
    if value > 999:
        raise ValueError("9.9.9 is the final version under the three-digit rule")
    return ".".join(f"{value:03d}")


if __name__ == "__main__":
    path = Path(__file__).resolve().parents[1] / "portal_config.json"
    config = json.loads(path.read_text(encoding="utf-8"))
    old = config["version"]
    config["version"] = next_version(old)
    path.write_text(json.dumps(config, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"{old} -> {config['version']}")
