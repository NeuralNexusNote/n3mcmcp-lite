import os
from pathlib import Path

from platformdirs import user_data_dir

APP_NAME = "n3mc-workingmemory"


def get_data_dir() -> Path:
    override = os.environ.get("N3MC_DATA_DIR")
    if override:
        return Path(override)
    return Path(user_data_dir(APP_NAME))


def get_config_path() -> Path:
    return get_data_dir() / "config.json"
