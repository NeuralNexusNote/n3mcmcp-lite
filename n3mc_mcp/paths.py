import os
from pathlib import Path

from platformdirs import user_data_dir

APP_NAME = "n3mc-workingmemory"

# Spec §7 (v1.7.0): the config lives in the user's home directory, NOT in the
# platform app-data directory. The Microsoft Store (MSIX) Claude desktop app
# redirects its MCP servers' reads/writes under %LOCALAPPDATA% to a
# package-private location, which gave one PC two config.json files and two
# owner_ids (desktop-app launches vs terminal launches). The home directory
# is not redirected.
HOME_DIR_NAME = ".n3mc-workingmemory"


def get_data_dir() -> Path:
    override = os.environ.get("N3MC_DATA_DIR")
    if override:
        return Path(override)
    return Path.home() / HOME_DIR_NAME


def get_config_path() -> Path:
    return get_data_dir() / "config.json"


def get_legacy_config_path() -> Path:
    """v1.6.x location (platformdirs user-data dir), used only for migration."""
    return Path(user_data_dir(APP_NAME)) / "config.json"
