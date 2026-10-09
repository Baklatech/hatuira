"""First run and `setup`: ask for the server and a long-lived access token."""

import getpass
from urllib.parse import urlsplit

from . import config
from .hass import ApiError, Hass

ACCENT, DIM, RED, GREEN, OFF = "\x1b[1;33m", "\x1b[2m", "\x1b[31m", "\x1b[32m", "\x1b[0m"


def normalise(url):
    """'ha.local:8123/lovelace' -> 'http://ha.local:8123'."""
    if "://" not in url:
        url = "http://" + url
    parts = urlsplit(url)
    return f"{parts.scheme}://{parts.netloc}"


def setup(cfg):
    """Prompt for the server and token, verify them, store them. Returns success."""
    print(f"\n  {ACCENT}● hatuira{OFF}  connect Home Assistant\n")
    default = cfg["url"] or config.DEFAULT_URL
    url = normalise(input(f"  url [{default}]: ").strip() or default)
    cfg["url"] = url  # a retry after a wrong token offers the same address again
    print(f"  {DIM}token: Home Assistant → your profile → Security → "
          f"Long-lived access tokens{OFF}")
    token = getpass.getpass("  token (hidden): ").strip()
    if not token:
        print(f"  {RED}no token given{OFF}")
        return False
    try:
        Hass(url, token, insecure=cfg.get("insecure", False)).ping()
    except ApiError as e:
        print(f"  {RED}✗ could not connect to {url}: {e}{OFF}")
        return False
    cfg["token"] = token
    cfg.pop("token_cmd", None)
    config.save(cfg)
    print(f"  {GREEN}✓{OFF} connected — saved in {config.path()}")
    return True


def first_run(cfg):
    while not setup(cfg):
        if input("  try again? [Y/n]: ").strip().lower() == "n":
            return False
    return True
