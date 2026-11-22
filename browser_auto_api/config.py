"""配置读写：端口等设置保存在程序目录的 config.json。"""

import json
import os

APP_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CONFIG_PATH = os.path.join(APP_DIR, "config.json")

DEFAULT_CONFIG = {
    "port": 8765,
    "last_url": "",
    "pointer_pos": True,
    "pointer_trail": False,
}


def load_config() -> dict:
    cfg = dict(DEFAULT_CONFIG)
    try:
        with open(CONFIG_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, dict):
            for key in DEFAULT_CONFIG:
                if key in data:
                    cfg[key] = data[key]
            # keep keys owned by other components (e.g. app ui settings)
            for key, value in data.items():
                if key not in cfg:
                    cfg[key] = value
    except (OSError, ValueError):
        pass
    if not isinstance(cfg.get("port"), int) or not (1 <= cfg["port"] <= 65535):
        cfg["port"] = DEFAULT_CONFIG["port"]
    return cfg


def save_config(cfg: dict) -> None:
    try:
        merged = {}
        try:
            with open(CONFIG_PATH, "r", encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data, dict):
                merged = data
        except (OSError, ValueError):
            pass
        merged.update(cfg)
        with open(CONFIG_PATH, "w", encoding="utf-8") as f:
            json.dump(merged, f, ensure_ascii=False, indent=2)
    except OSError:
        pass
