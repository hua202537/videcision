#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""大小写不敏感字段读取、项目/批次记忆、紧凑时间戳格式化。"""

from __future__ import annotations

import threading
from datetime import datetime, timedelta
from typing import Any, Dict, Optional, Tuple

DEFAULT_PROJECT = "default"
DEFAULT_BATCH = "default"

_project_batch_registry: Dict[str, str] = {}
_registry_lock = threading.Lock()

_MESSAGE_TYPE_CANONICAL = {
    "generate": "generate",
    "regenerate": "reGenerate",
    "jammerstart": "jammerStart",
    "simdata": "simData",
    "fusion_end": "fusion_end",
    "fusionend": "fusion_end",
    "debug_finalize": "debug_finalize",
}


def ci_get(data: Any, *logical_keys: str, default: Any = None) -> Any:
    """从 dict 中按逻辑名大小写不敏感取值。"""
    if not isinstance(data, dict):
        return default
    key_map = {str(k).lower(): k for k in data.keys()}
    for lk in logical_keys:
        real = key_map.get(lk.lower())
        if real is None:
            continue
        val = data[real]
        if val is not None and val != "":
            return val
    return default


def ci_get_nested(data: Any, *path: str, default: Any = None) -> Any:
    """path 交替为容器键与子字段逻辑名，如 ('scene', 'projectname')。"""
    cur = data
    i = 0
    while i < len(path):
        if not isinstance(cur, dict):
            return default
        key = path[i]
        if i + 1 < len(path) and path[i + 1].lower() in (
            "projectname", "batchname", "batch", "projectname", "range", "rangetype",
        ):
            cur = ci_get(cur, key, default=None)
            i += 1
            continue
        # 单段：取子 dict
        cur = ci_get(cur, key, default=None)
        i += 1
    return cur if cur is not None else default


def normalize_message_type(raw: Any) -> str:
    if raw is None:
        return ""
    s = str(raw).strip()
    if not s:
        return ""
    return _MESSAGE_TYPE_CANONICAL.get(s.lower(), s)


def extract_project_name(data: Any, default: str = DEFAULT_PROJECT) -> str:
    proj = ci_get(data, "projectname", "ProjectName")
    if not proj:
        scene = ci_get(data, "scene", "Scene")
        if isinstance(scene, dict):
            proj = ci_get(scene, "projectname", "ProjectName")
    if not proj:
        return default
    text = str(proj).strip()
    return text if text else default


def extract_batch_name(data: Any, default: Optional[str] = DEFAULT_BATCH) -> Optional[str]:
    """从报文提取批次；无字段时返回 default（可传 None 表示未提供）。"""
    batch = ci_get(data, "batchname", "batchName", "batch")
    if not batch:
        scene = ci_get(data, "scene", "Scene")
        if isinstance(scene, dict):
            batch = ci_get(scene, "batchname", "batchName", "batch")
    if batch is None or batch == "":
        return default
    text = str(batch).strip()
    return text if text else default


def remember_project_batch(project: str, batch: Optional[str]) -> str:
    """同一 project 共享 batch；新消息带 batch 则更新，否则用已记忆或 default。"""
    project = (project or DEFAULT_PROJECT).strip() or DEFAULT_PROJECT
    with _registry_lock:
        if batch:
            _project_batch_registry[project] = batch
            return batch
        return _project_batch_registry.get(project, DEFAULT_BATCH)


def sync_project_batch(data: Any) -> Tuple[str, str]:
    """从报文解析 project/batch 并更新记忆表。"""
    project = extract_project_name(data, DEFAULT_PROJECT)
    batch_in_msg = extract_batch_name(data, default=None)
    batch = remember_project_batch(project, batch_in_msg)
    return project, batch


def get_batch_for_project(project: str) -> str:
    project = (project or DEFAULT_PROJECT).strip() or DEFAULT_PROJECT
    with _registry_lock:
        return _project_batch_registry.get(project, DEFAULT_BATCH)


def clear_project_batch_registry(project: Optional[str] = None) -> None:
    with _registry_lock:
        if project is None:
            _project_batch_registry.clear()
        else:
            _project_batch_registry.pop(project, None)


def ensure_scene_metadata(config: dict, project_name: str, batch_name: str) -> dict:
    scene = dict(config.get("scene") or config.get("Scene") or {})
    scene["projectname"] = project_name or DEFAULT_PROJECT
    scene["batchname"] = batch_name or DEFAULT_BATCH
    config["scene"] = scene
    config["projectname"] = project_name or DEFAULT_PROJECT
    config["batchname"] = batch_name or DEFAULT_BATCH
    return config


def format_compact_timestamp(dt: datetime) -> str:
    """格式：YYYYMMDDHHMMSSmmm，例如 20251001120001200。"""
    ms = dt.microsecond // 1000
    return dt.strftime("%Y%m%d%H%M%S") + f"{ms:03d}"


def format_compact_time_at_offset(sim_start: Optional[datetime], t_rel: float) -> str:
    if sim_start is not None:
        return format_compact_timestamp(sim_start + timedelta(seconds=t_rel))
    sec_ms = int(round(t_rel * 1000))
    sec, ms = divmod(sec_ms, 1000)
    h, rem = divmod(sec, 3600)
    m, s = divmod(rem, 60)
    return f"00000000{h:02d}{m:02d}{s:02d}{ms:03d}"
