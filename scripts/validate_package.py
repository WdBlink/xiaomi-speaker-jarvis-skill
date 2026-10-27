#!/usr/bin/env python3
"""Check the distributable skill without credentials, network, or hardware."""
from pathlib import Path
import ast
import json
import plistlib
import re
import sys
import tomllib
from urllib.parse import unquote, urlsplit


def validate(root: Path) -> list[str]:
    errors = []
    skill = root / "skills/xiaomi-speaker-jarvis"
    required = ["README.md", "README.zh-CN.md", "LICENSE", "UPSTREAM.md", "VALIDATION.md",
                "skills/xiaomi-speaker-jarvis/SKILL.md",
                "skills/xiaomi-speaker-jarvis/agents/openai.yaml",
                "skills/xiaomi-speaker-jarvis/assets/runtime/pyproject.toml"]
    for name in required:
        if not (root / name).is_file():
            errors.append(f"Missing {name}")
    entry = skill / "SKILL.md"
    if entry.exists():
        text = entry.read_text()
        front = re.match(r"\A---\n(.*?)\n---\n", text, re.S)
        if (not front or not re.search(r"^name: xiaomi-speaker-jarvis$", front[1], re.M)
                or not re.search(r"^description: \S.+$", front[1], re.M)):
            errors.append("Skill entry needs a matching name and a nonempty description")
    ignored = {".git", ".venv", "__pycache__", ".pytest_cache", "build", "dist"}
    files = [p for p in root.rglob("*") if p.is_file()
             and not any(part in ignored or part.endswith(".egg-info") for part in p.relative_to(root).parts)]
    for path in files:
        rel = path.relative_to(root)
        if path.is_symlink():
            errors.append(f"Distribution contains symlink: {rel}")
            continue
        if path.suffix in {".db", ".sqlite3", ".sqlite", ".log", ".pem", ".key"} or path.name.endswith("-qr.png"):
            errors.append(f"Private/runtime artifact included: {rel}")
            continue
        try:
            if path.suffix == ".py":
                ast.parse(path.read_text(), filename=str(rel))
            elif path.suffix == ".json":
                json.loads(path.read_text())
            elif path.suffix == ".toml":
                tomllib.loads(path.read_text())
            elif path.suffix == ".plist":
                plistlib.loads(path.read_bytes())
            if path.suffix != ".md":
                continue
            text = path.read_text()
            if sum(line.startswith("```") for line in text.splitlines()) % 2:
                errors.append(f"Unclosed Markdown fence: {rel}")
            prose = re.sub(r"^```[^\n]*\n.*?^```\s*$", "", text, flags=re.M | re.S)
            for target in re.findall(r"\]\(([^)]+)\)", prose):
                target = target.strip().strip("<>")
                parsed = urlsplit(target)
                if parsed.scheme or not parsed.path:
                    continue
                resolved = (path.parent / unquote(parsed.path)).resolve()
                if not resolved.is_relative_to(root) or not resolved.exists():
                    errors.append(f"Broken/outside local link in {rel}: {target}")
        except (ValueError, SyntaxError, UnicodeError, plistlib.InvalidFileException) as error:
            errors.append(f"Invalid {rel}: {type(error).__name__}")
    return errors


if __name__ == "__main__":
    root = Path(__file__).resolve().parents[1]
    errors = validate(root)
    if errors:
        print("\n".join(errors), file=sys.stderr)
        raise SystemExit(1)
    print("Skill structure, local links, and Python/JSON/TOML/plist syntax passed.")
