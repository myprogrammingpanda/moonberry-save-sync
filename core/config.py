"""Config loading. Game-agnostic: just JSON plus recursive %ENV% expansion
for any string value (so path-like config keys work without core needing to
know which keys are paths -- each game module's own keys get expanded the
same way)."""

import json
import os
import sys
from pathlib import Path


def _expand_env(value):
    if isinstance(value, str):
        return os.path.expandvars(value)
    if isinstance(value, dict):
        return {k: _expand_env(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_expand_env(v) for v in value]
    return value


def load_config(config_path: Path) -> dict:
    if not config_path.exists():
        print(
            f"{config_path.name} not found. Copy config.example.json to "
            f"{config_path.name} and fill in your values.",
            file=sys.stderr,
        )
        sys.exit(1)
    with open(config_path, "r", encoding="utf-8") as f:
        cfg = json.load(f)
    return _expand_env(cfg)
