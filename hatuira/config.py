"""Server, token and choices in ~/.config/hatuira/config.json (mode 0600)."""

import json
import os
import subprocess
from pathlib import Path

DEFAULT_URL = "http://homeassistant.local:8123"
DEFAULTS = {
    "interval": 5,       # seconds between polls while the list is open
    "step": 10,          # percent per ←/→ press
    "mode": "single",    # lights that follow the theme: a "single" colour, the two or three
                         # "main" colours of the theme or a "mix" of all its hues
    "color": "accent",   # colors.toml key they all get in "single" mode
    "transition": 1,     # seconds a light takes to fade to a new theme colour
}


def path():
    base = os.environ.get("XDG_CONFIG_HOME") or os.path.expanduser("~/.config")
    return Path(base) / "hatuira" / "config.json"


def load():
    try:
        cfg = json.loads(path().read_text())
    except FileNotFoundError:
        cfg = {}
    cfg.setdefault("url", "")
    cfg.setdefault("hidden", [])  # entity ids left off the list
    cfg.setdefault("shown", [])   # switches listed although they do not look like lighting
    cfg.setdefault("sync", [])    # lights whose colour follows the omarchy theme
    cfg["settings"] = {**DEFAULTS, **cfg.get("settings", {})}
    return cfg


def save(cfg):
    p = path()
    p.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    body = dict(cfg)
    body["settings"] = {k: v for k, v in cfg["settings"].items() if DEFAULTS.get(k) != v}
    tmp = p.with_suffix(".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write(json.dumps(body, indent=2, ensure_ascii=False) + "\n")
    os.replace(tmp, p)


def token(cfg):
    """The config stores either the token itself or a command that prints it."""
    cmd = cfg.get("token_cmd")
    if not cmd:
        return cfg.get("token", "")
    out = subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=20)
    if out.returncode != 0 or not out.stdout.strip():
        raise RuntimeError(f"token_cmd failed: {cmd}")
    return out.stdout.strip()
