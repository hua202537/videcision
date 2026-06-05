"""运行时配置读写：持久化到 .env，供网页/API 修改 MQTT 与端口参数。"""

import os
import re
from pathlib import Path
from typing import Dict, List, Optional, Tuple

CONFIG_KEYS = (
    "HOST",
    "PORT",
    "HOST_PORT",
    "SOURCE_BROKER",
    "SOURCE_PORT",
    "SOURCE_USERNAME",
    "SOURCE_PASSWORD",
)

DB_CONFIG_KEYS = (
    "DB_HOST",
    "DB_PORT",
    "DB_USER",
    "DB_PASSWORD",
    "DB_NAME",
)

DEFAULTS: Dict[str, str] = {
    "HOST": "0.0.0.0",
    "PORT": "17686",
    "HOST_PORT": "30517",
    "SOURCE_BROKER": "172.16.10.13",
    "SOURCE_PORT": "30502",
    "SOURCE_USERNAME": "test",
    "SOURCE_PASSWORD": "test",
}

DB_DEFAULTS: Dict[str, str] = {
    "DB_HOST": "172.16.10.8",
    "DB_PORT": "19030",
    "DB_USER": "root",
    "DB_PASSWORD": "123456",
    "DB_NAME": "fusion",
}

_ENV_LINE = re.compile(r"^([A-Za-z_][A-Za-z0-9_]*)=(.*)$")


def get_config_path() -> Path:
    return Path(os.getenv("CONFIG_FILE", ".env"))


def _parse_env_lines(text: str) -> Tuple[List[str], Dict[str, str]]:
    """解析 .env 文本，保留原始行顺序与注释。"""
    lines: List[str] = []
    values: Dict[str, str] = {}
    for raw_line in text.splitlines():
        line = raw_line.rstrip("\n")
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            lines.append(line)
            continue
        match = _ENV_LINE.match(stripped)
        if match:
            key, value = match.group(1), match.group(2)
            values[key] = value
            lines.append(line)
        else:
            lines.append(line)
    return lines, values


def load_config() -> Dict[str, str]:
    """从配置文件、环境变量、默认值合并读取。"""
    config = dict(DEFAULTS)
    path = get_config_path()
    if path.is_file():
        try:
            file_values = _parse_env_lines(path.read_text(encoding="utf-8"))[1]
            config.update({k: v for k, v in file_values.items() if k in CONFIG_KEYS})
        except OSError:
            pass
    for key in CONFIG_KEYS:
        env_val = os.getenv(key)
        if env_val is not None and str(env_val).strip() != "":
            config[key] = str(env_val).strip()
    return config


def load_db_config() -> Dict[str, str]:
    """读取 fusion 数据库连接配置（独立于 MQTT/端口配置）。"""
    config = dict(DB_DEFAULTS)
    path = get_config_path()
    if path.is_file():
        try:
            file_values = _parse_env_lines(path.read_text(encoding="utf-8"))[1]
            config.update({k: v for k, v in file_values.items() if k in DB_CONFIG_KEYS})
        except OSError:
            pass
    for key in DB_CONFIG_KEYS:
        env_val = os.getenv(key)
        if env_val is not None and str(env_val).strip() != "":
            config[key] = str(env_val).strip()
    return config


def apply_config_to_environ(config: Dict[str, str]) -> None:
    for key in CONFIG_KEYS:
        if key in config:
            os.environ[key] = str(config[key])


def save_config(updates: Dict[str, str]) -> Dict[str, str]:
    """合并更新并写回 .env，返回完整配置。"""
    path = get_config_path()
    current = load_config()
    for key, value in updates.items():
        if key in CONFIG_KEYS and value is not None:
            current[key] = str(value).strip()
    if path.is_file():
        existing_lines, _ = _parse_env_lines(path.read_text(encoding="utf-8"))
    else:
        existing_lines = []

    written_keys = set()
    new_lines: List[str] = []
    for line in existing_lines:
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            new_lines.append(line)
            continue
        match = _ENV_LINE.match(stripped)
        if match and match.group(1) in CONFIG_KEYS:
            key = match.group(1)
            new_lines.append(f"{key}={current[key]}")
            written_keys.add(key)
        else:
            new_lines.append(line)

    for key in CONFIG_KEYS:
        if key not in written_keys:
            new_lines.append(f"{key}={current[key]}")

    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(new_lines) + "\n", encoding="utf-8")
    apply_config_to_environ(current)
    return current


def validate_config(data: Dict[str, str]) -> Optional[str]:
    """校验配置项，失败返回错误信息。"""
    for port_key in ("PORT", "HOST_PORT", "SOURCE_PORT"):
        if port_key in data:
            try:
                port = int(data[port_key])
                if port < 1 or port > 65535:
                    return f"{port_key} 必须在 1-65535 之间"
            except (TypeError, ValueError):
                return f"{port_key} 必须是有效端口号"

    if "SOURCE_BROKER" in data and not str(data["SOURCE_BROKER"]).strip():
        return "SOURCE_BROKER 不能为空"

    if "HOST" in data and not str(data["HOST"]).strip():
        return "HOST 不能为空"

    return None


def public_config(config: Dict[str, str]) -> Dict[str, str]:
    """对外展示的配置（明文，便于管理页查看与编辑）。"""
    return {k: config.get(k, DEFAULTS.get(k, "")) for k in CONFIG_KEYS}
