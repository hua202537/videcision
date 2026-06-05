import os
import json
import time
import hashlib
import math
import random
import logging
import builtins
import sys
import io
from pathlib import Path
from typing import Dict, List, Optional, Tuple, Union

from pyproj import Transformer, CRS
from logging.handlers import RotatingFileHandler
from queue import Queue

from flask import Flask, jsonify, request, g, render_template
from flask_mqtt import Mqtt
from flask_cors import CORS

from config_manager import (
    load_config,
    save_config,
    validate_config,
    public_config,
    apply_config_to_environ,
)
import threading
import concurrent.futures
import matplotlib

matplotlib.use('Agg')

os.environ['PYTHONIOENCODING'] = 'utf-8'
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8')
sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding='utf-8')

# 部署模块
from communication_random_deployment import JammerDeployment as CommRandomDeployment
from radar_random_deployment import JammerDeployment as RadarRandomDeployment
from communication_youhua_deployment import JammerOptimization as CommOptimization
from radar_youhua_deployment import RadarJammerOptimization as RadarOptimization

# 决策模块（流式版本）
import communication_decision
import radar_decision
from field_utils import (
    DEFAULT_BATCH,
    DEFAULT_PROJECT,
    ci_get,
    clear_project_batch_registry,
    ensure_scene_metadata,
    extract_batch_name,
    extract_project_name,
    get_batch_for_project,
    normalize_message_type,
    sync_project_batch,
)
from shujuku import query_decision_result_project_batch_list, query_decision_result_simulation

# ---------- 日志重定向 ----------
_original_print = builtins.print

def _logging_print(*args, **kwargs):
    if sys.exc_info() != (None, None, None):
        _original_print(*args, **kwargs)
    else:
        message = ' '.join(str(arg) for arg in args)
        logger.info(message)

builtins.print = _logging_print

# ---------- Flask 初始化 ----------
app = Flask(__name__)
CORS(app, resources={r"/api/*": {"origins": "*"}})

log_dir = "logs"
os.makedirs(log_dir, exist_ok=True)
log_file = os.path.join(log_dir, "decision_system.log")

for handler in logging.root.handlers[:]:
    logging.root.removeHandler(handler)

file_handler = RotatingFileHandler(log_file, maxBytes=50*1024*1024, backupCount=5, encoding='utf-8')
file_handler.setLevel(logging.DEBUG)
file_handler.setFormatter(logging.Formatter('%(asctime)s - %(levelname)s - [%(threadName)s] %(message)s'))

console_handler = logging.StreamHandler(sys.stderr)
console_handler.setLevel(logging.DEBUG)
console_handler.setFormatter(logging.Formatter('%(asctime)s - %(levelname)s - [%(threadName)s] %(message)s'))

logging.root.addHandler(file_handler)
logging.root.addHandler(console_handler)
logging.root.setLevel(logging.DEBUG)

logger = logging.getLogger(__name__)

def _safe_json_dumps(data, limit: int = 500) -> str:
    try:
        text = json.dumps(data, ensure_ascii=False, default=str)
    except Exception:
        text = str(data)
    if len(text) > limit:
        return text[:limit] + "...<truncated>"
    return text

def _get_client_ip() -> str:
    forwarded_for = request.headers.get("X-Forwarded-For", "").strip()
    if forwarded_for:
        return forwarded_for.split(",")[0].strip()
    real_ip = request.headers.get("X-Real-IP", "").strip()
    if real_ip:
        return real_ip
    return request.remote_addr or "unknown"

def _get_request_payload_preview() -> str:
    try:
        payload = request.get_json(silent=True)
        if payload is not None:
            return _safe_json_dumps(payload)
        form_dict = request.form.to_dict(flat=True)
        if form_dict:
            return _safe_json_dumps(form_dict)
        raw_data = request.get_data(cache=True, as_text=True) or ""
        raw_data = raw_data.strip()
        if raw_data:
            if len(raw_data) > 500:
                return raw_data[:500] + "...<truncated>"
            return raw_data
    except Exception as e:
        return f"<payload parse failed: {e}>"
    return ""

@app.before_request
def _log_http_request():
    g.request_start_time = time.time()
    g.request_client_ip = _get_client_ip()
    payload_preview = _get_request_payload_preview()
    logger.info(
        "HTTP请求开始: ip=%s method=%s path=%s query=%s payload=%s",
        g.request_client_ip,
        request.method,
        request.path,
        request.query_string.decode("utf-8", errors="ignore"),
        payload_preview or "<empty>"
    )

@app.after_request
def _log_http_response(response):
    started = getattr(g, "request_start_time", time.time())
    cost_ms = int((time.time() - started) * 1000)
    client_ip = getattr(g, "request_client_ip", _get_client_ip())
    logger.info(
        "HTTP请求结束: ip=%s method=%s path=%s status=%s cost_ms=%s content_length=%s",
        client_ip,
        request.method,
        request.path,
        response.status_code,
        cost_ms,
        response.calculate_content_length()
    )
    return response

@app.errorhandler(Exception)
def _handle_unexpected_http_error(e):
    client_ip = getattr(g, "request_client_ip", "unknown")
    logger.error(
        "HTTP请求异常: ip=%s method=%s path=%s error=%s",
        client_ip,
        request.method,
        request.path,
        e,
        exc_info=True
    )
    return jsonify({
        "success": False,
        "message": f"服务内部异常: {str(e)}"
    }), 500

# ---------- 运行时配置（.env 持久化，支持网页修改） ----------
_runtime_config = load_config()
apply_config_to_environ(_runtime_config)


def _apply_mqtt_config_from_env():
    app.config['MQTT_BROKER_URL'] = os.getenv("SOURCE_BROKER", "172.16.10.13")
    app.config['MQTT_BROKER_PORT'] = int(os.getenv("SOURCE_PORT", 30502))
    app.config['MQTT_USERNAME'] = os.getenv("SOURCE_USERNAME", "test1")
    app.config['MQTT_PASSWORD'] = os.getenv("SOURCE_PASSWORD", "test1")
    app.config['MQTT_KEEPALIVE'] = 120
    app.config['MQTT_TLS_ENABLED'] = False


# ---------- MQTT 配置 ----------
_apply_mqtt_config_from_env()
app.config['MQTT_CLIENT_ID'] = os.getenv('MQTT_CLIENT_ID', f"decision_{os.getpid()}")

# 注意：Mqtt(app) 构造函数内已调用 init_app，切勿再次 init_app，否则会重复 connect/loop_start
mqtt_receiver = Mqtt(app)

# ---------- 订阅主题 (修改：仅订阅指令与目标数据，步数据统一发到 vi_decision_res，服务端不再订阅以避免自消费) ----------
SUBSCRIBE_TOPICS = [
    'vi_decision/#',
    'pg_data_processor_finish/#',
]
for topic in SUBSCRIBE_TOPICS:
    mqtt_receiver.subscribe(topic)
    logger.info(f"注册订阅主题: {topic}")

_mqtt_connected = False
_mqtt_lock = threading.Lock()
_mqtt_dedup_lock = threading.Lock()
_mqtt_dedup_cache: Dict[str, Union[float, str]] = {}
_MQTT_DEDUP_TTL = 2.0
_MQTT_CLAIM_PROCESSING = "__processing__"
_mqtt_dispatch_thread_id: Optional[int] = None


def _mqtt_loop_thread_names() -> List[str]:
    return [
        t.name for t in threading.enumerate()
        if t.name.startswith("paho-mqtt") or t.name == "Thread-1"
    ]


def _stop_mqtt_network_loop(client) -> None:
    """安全停止 paho 网络线程，避免 reconnect 后存在多个 loop 线程。"""
    try:
        client.loop_stop()
    except Exception as e:
        logger.debug(f"MQTT loop_stop: {e}")
    thread = getattr(client, "_thread", None)
    if thread is not None and thread.is_alive():
        try:
            thread.join(timeout=3.0)
        except Exception as e:
            logger.debug(f"MQTT loop thread join: {e}")
    client._thread = None


def _start_mqtt_network_loop(client) -> None:
    """仅启动一个 MQTT 网络循环线程。"""
    if getattr(client, "_thread", None) is not None:
        logger.warning("MQTT loop 线程仍存在，先停止再启动")
        _stop_mqtt_network_loop(client)
        time.sleep(0.05)
    rc = client.loop_start()
    if rc not in (None, 0):
        logger.error("MQTT loop_start 失败，返回码=%s", rc)


def _ensure_single_mqtt_network_loop() -> None:
    """flask-mqtt 初始化后强制只保留一个 loop，消除 Thread-1 与 paho 双线程重复收消息。"""
    global _mqtt_dispatch_thread_id
    client = mqtt_receiver.client
    before = _mqtt_loop_thread_names()
    if before:
        logger.warning("检测到多个 MQTT 网络线程，正在重置: %s", before)
    _stop_mqtt_network_loop(client)
    time.sleep(0.1)
    _start_mqtt_network_loop(client)
    _mqtt_dispatch_thread_id = None
    after = _mqtt_loop_thread_names()
    logger.info("MQTT 网络循环已重置，当前线程: %s", after or "(无)")


_ensure_single_mqtt_network_loop()


def _mqtt_dedup_key(topic: str, payload: bytes) -> str:
    return f"{topic}:{hashlib.md5(payload).hexdigest()}"


def _try_claim_mqtt_message(topic: str, payload: bytes) -> Optional[str]:
    """抢占处理权；返回 key 表示本线程应处理，返回 None 表示跳过重复消息。"""
    key = _mqtt_dedup_key(topic, payload)
    now = time.time()
    with _mqtt_dedup_lock:
        entry = _mqtt_dedup_cache.get(key)
        if entry == _MQTT_CLAIM_PROCESSING:
            return None
        if isinstance(entry, float) and now - entry < _MQTT_DEDUP_TTL:
            return None
        _mqtt_dedup_cache[key] = _MQTT_CLAIM_PROCESSING
        if len(_mqtt_dedup_cache) > 2000:
            cutoff = now - _MQTT_DEDUP_TTL
            expired = [k for k, ts in _mqtt_dedup_cache.items() if isinstance(ts, float) and ts < cutoff]
            for k in expired:
                _mqtt_dedup_cache.pop(k, None)
    return key


def _release_mqtt_claim(key: Optional[str]) -> None:
    if not key:
        return
    with _mqtt_dedup_lock:
        _mqtt_dedup_cache[key] = time.time()


def _bind_mqtt_dispatch_thread() -> bool:
    """仅允许 client._thread 对应的网络线程向业务层派发消息。"""
    global _mqtt_dispatch_thread_id
    tid = threading.get_ident()
    name = threading.current_thread().name
    loop_thread = getattr(mqtt_receiver.client, "_thread", None)
    if loop_thread is not None and threading.current_thread() is not loop_thread:
        logger.info(
            "跳过非主 MQTT loop 线程 %s（主循环=%s）",
            name,
            loop_thread.name,
        )
        return False
    if _mqtt_dispatch_thread_id is None:
        _mqtt_dispatch_thread_id = tid
        logger.info("MQTT 消息派发绑定线程: %s", name)
        return True
    if tid == _mqtt_dispatch_thread_id:
        return True
    logger.info("跳过非绑定 MQTT 线程的重复回调: %s (绑定线程 id=%s)", name, _mqtt_dispatch_thread_id)
    return False


def reconnect_mqtt() -> bool:
    """断开并按当前环境变量中的 MQTT 配置重新连接。"""
    global _mqtt_connected
    _apply_mqtt_config_from_env()
    broker = app.config['MQTT_BROKER_URL']
    port = int(app.config['MQTT_BROKER_PORT'])
    username = app.config.get('MQTT_USERNAME') or ''
    password = app.config.get('MQTT_PASSWORD') or ''
    keepalive = int(app.config.get('MQTT_KEEPALIVE', 120))

    with _mqtt_lock:
        _mqtt_connected = False
        client = mqtt_receiver.client
        _stop_mqtt_network_loop(client)
        try:
            client.disconnect()
        except Exception as e:
            logger.debug(f"MQTT disconnect: {e}")
        time.sleep(0.3)

        if username:
            client.username_pw_set(username, password)
        else:
            client.username_pw_set(None, None)
        try:
            global _mqtt_dispatch_thread_id
            _mqtt_dispatch_thread_id = None
            client.connect(broker, port, keepalive=keepalive)
            _start_mqtt_network_loop(client)
            for topic in SUBSCRIBE_TOPICS:
                client.subscribe(topic)
            logger.info(
                "MQTT 已重连至 %s:%s（client_id=%s, pid=%s）",
                broker,
                port,
                app.config.get('MQTT_CLIENT_ID'),
                os.getpid(),
            )
            return True
        except Exception as e:
            logger.error(f"MQTT 重连失败: {e}", exc_info=True)
            return False

# ---------- MQTT 发送队列 ----------
mqtt_send_queue = Queue()

def _parse_publish_result(result):
    """兼容 flask-mqtt / paho 不同 publish 返回类型。"""
    rc = getattr(result, 'rc', None)
    mid = getattr(result, 'mid', None)
    if rc is not None:
        return rc, mid
    if isinstance(result, tuple):
        if len(result) >= 2:
            return result[0], result[1]
        if len(result) == 1:
            return result[0], None
    return None, None

def mqtt_sender_worker():
    while True:
        try:
            topic, payload = mqtt_send_queue.get()
            if topic is None:
                break
            max_wait = 10.0
            waited = 0.0
            while True:
                with _mqtt_lock:
                    connected = _mqtt_connected
                if connected:
                    break
                if waited >= max_wait:
                    logger.error(f"等待 MQTT 连接超时 ({max_wait}s)，丢弃消息，主题: {topic}")
                    break
                time.sleep(0.5)
                waited += 0.5
            if not connected:
                continue
            result = mqtt_receiver.publish(topic, payload, qos=1)
            rc, mid = _parse_publish_result(result)
            if rc is not None:
                if rc == 0:
                    logger.info(f"队列发送成功: 主题={topic}, 消息ID={mid}, 大小={len(payload)} 字节")
                else:
                    logger.error(f"队列发送失败: 返回码={rc}, 主题={topic}")
            else:
                logger.warning(f"无法获取 MQTT 返回码，result 类型: {type(result)}")
        except Exception as e:
            logger.error(f"队列发送异常: {e}", exc_info=True)

threading.Thread(target=mqtt_sender_worker, daemon=True, name="MQTTSender").start()

# ---------- 流式状态管理 ----------
stream_active = False
stream_project_name: Optional[str] = None
stream_batch_name: Optional[str] = None
stream_comm_sim_rand: Optional[communication_decision.CommunicationJammerSimulation] = None
stream_comm_sim_opt: Optional[communication_decision.CommunicationJammerSimulation] = None
stream_radar_sim_rand: Optional[radar_decision.RadarJammerSimulation] = None
stream_radar_sim_opt: Optional[radar_decision.RadarJammerSimulation] = None
stream_lock = threading.RLock()
stream_t0_abs: Optional[float] = None
stream_last_t_rel: Optional[float] = None

finalizing = False
finalizing_lock = threading.Lock()
_mqtt_dispatch_lock = threading.Lock()

_IDLE_TIMEOUT = 300.0
_idle_timer: Optional[threading.Timer] = None
_timer_lock = threading.Lock()
stream_frame_count = 0

# ---------- 流程阶段管理（部署 -> 决策开始 -> 融合数据） ----------
_flow_stage_lock = threading.Lock()
_project_flow_stage: Dict[str, Dict[str, bool]] = {}
_generate_done_global = False
_pending_random_deployment: Optional[Dict[str, object]] = None

def _get_message_type(data: dict) -> str:
    raw = ci_get(data, "type", "Type", default="")
    return normalize_message_type(raw)

_DEPLOYMENT_MSG_TYPES = frozenset({"generate", "reGenerate", "jammerStart"})

def _is_deployment_message(msg_type: str) -> bool:
    return msg_type in _DEPLOYMENT_MSG_TYPES

def _log_flow_stage_snapshot(context: str) -> None:
    with _flow_stage_lock:
        snapshot = dict(_project_flow_stage)
    logger.info("流程阶段快照 [%s]: %s", context, snapshot or "(空)")

def _extract_project_name(data: dict, default: str = DEFAULT_PROJECT) -> str:
    return extract_project_name(data, default)

def _ensure_project_in_config(config: dict, project_name: str, batch_name: str = DEFAULT_BATCH) -> None:
    ensure_scene_metadata(config, project_name or DEFAULT_PROJECT, batch_name or DEFAULT_BATCH)

def _mark_stage(project: str, stage_key: str, value: bool = True):
    with _flow_stage_lock:
        stage = _project_flow_stage.setdefault(project, {
            "generate_done": False,
            "regenerate_done": False
        })
        stage[stage_key] = value
    _touch_system_activity()
    logger.info(f"流程阶段更新: project={project}, {stage_key}={value}")

def _can_start_decision(project: str) -> bool:
    with _flow_stage_lock:
        stage = _project_flow_stage.get(project, {})
        return bool(stage.get("generate_done")) and bool(stage.get("regenerate_done"))

def _on_generate_completed() -> None:
    global _generate_done_global, _pending_random_deployment
    with _flow_stage_lock:
        _generate_done_global = True
    _touch_system_activity()
    logger.info("流程阶段更新: generate 已完成（未绑定项目，等待 reGenerate 绑定）")

def _on_regenerate_completed(project: str) -> None:
    global _generate_done_global
    with _flow_stage_lock:
        stage = _project_flow_stage.setdefault(project, {
            "generate_done": False,
            "regenerate_done": False,
        })
        stage["generate_done"] = True
        stage["regenerate_done"] = True
        _generate_done_global = False
    _touch_system_activity()
    logger.info(
        "流程阶段更新: project=%s, generate_done=True, regenerate_done=True",
        project,
    )

def _store_pending_random_deployment(comm_result: List[dict], radar_result: List[dict]) -> None:
    global _pending_random_deployment
    _pending_random_deployment = {
        "comm": [j.copy() for j in comm_result],
        "radar": [j.copy() for j in radar_result],
        "timestamp": time.strftime('%Y-%m-%d %H:%M:%S'),
    }
    logger.info(
        "随机部署结果已缓存在内存（通信 %d / 雷达 %d），不落盘",
        len(comm_result),
        len(radar_result),
    )

def _load_pending_random_as_original(
    comm_original: List[dict],
    radar_original: List[dict],
) -> tuple:
    global _pending_random_deployment
    if not _pending_random_deployment:
        return comm_original, radar_original
    if not comm_original and _pending_random_deployment.get("comm"):
        comm_original = [j.copy() for j in _pending_random_deployment["comm"]]
        logger.info("reGenerate 未携带通信随机位置，使用内存中 generate 结果")
    if not radar_original and _pending_random_deployment.get("radar"):
        radar_original = [j.copy() for j in _pending_random_deployment["radar"]]
        logger.info("reGenerate 未携带雷达随机位置，使用内存中 generate 结果")
    return comm_original, radar_original

def _clear_stage(project: Optional[str] = None):
    global _generate_done_global, _pending_random_deployment
    clear_project_batch_registry(project)
    with _flow_stage_lock:
        if project is None:
            _project_flow_stage.clear()
            _generate_done_global = False
            _pending_random_deployment = None
            _touch_system_activity()
            logger.info("已清空所有项目流程阶段、批次记忆与随机部署缓存")
            return
        if project in _project_flow_stage:
            _project_flow_stage.pop(project, None)
            _touch_system_activity()
            logger.info(f"已清空项目流程阶段: {project}")

def _end_fusion_stream(data: dict, *, topic: str = ""):
    """收到 fusion_end（pg_data_processor_finish 主题）时立即结束融合流。"""
    topic_is_pg_finish = (
        topic == "pg_data_processor_finish"
        or topic.startswith("pg_data_processor_finish/")
    )
    if not topic_is_pg_finish:
        logger.warning(
            "收到 fusion_end 但 topic 非 pg_data_processor_finish，忽略。topic=%s",
            topic,
        )
        return

    # 先取消空闲定时器，避免 fusion_end 后仍等待 300 秒超时
    _cancel_idle_timer()

    proj = _extract_project_name(data, default="")
    if not proj:
        proj = str(ci_get(data, "simID", "simid", default="") or "").strip()

    with stream_lock:
        active = stream_active
        has_sims = any(
            sim is not None
            for sim in (
                stream_comm_sim_rand,
                stream_comm_sim_opt,
                stream_radar_sim_rand,
                stream_radar_sim_opt,
            )
        )
        if not proj:
            proj = stream_project_name or ""

    with finalizing_lock:
        if finalizing:
            logger.warning("收到 fusion_end 但最终化已在进行，已取消空闲定时器")
            return

    if not active and not has_sims:
        with _status_lock:
            status = _system_status
        logger.warning(
            "收到 fusion_end 但无活跃流/模拟器（stream_active=%s, status=%s），"
            "已取消空闲定时器，跳过最终化",
            active,
            status,
        )
        return

    logger.info(
        "收到 fusion_end，立即触发最终化（stream_active=%s, has_sims=%s, project=%s）",
        active,
        has_sims,
        proj or "(未知)",
    )
    _finalize_stream(idle_reason="normal_finished")
    if proj:
        _clear_stage(proj)

# ---------- 系统状态管理 ----------
_system_status = "idle"  # idle / running / error
_system_status_project: Optional[str] = None
_system_status_error: Optional[str] = None
_system_status_reason = "initial"
_system_last_update_time = time.strftime('%Y-%m-%d %H:%M:%S')
_status_lock = threading.Lock()

def _touch_system_activity():
    global _system_last_update_time
    with _status_lock:
        _system_last_update_time = time.strftime('%Y-%m-%d %H:%M:%S')

def _get_current_flow_stage():
    with _flow_stage_lock:
        if _system_status_project and _system_status_project in _project_flow_stage:
            return {
                "project": _system_status_project,
                **_project_flow_stage[_system_status_project]
            }
        if stream_project_name and stream_project_name in _project_flow_stage:
            return {
                "project": stream_project_name,
                **_project_flow_stage[stream_project_name]
            }
        if len(_project_flow_stage) == 1:
            project, stage = next(iter(_project_flow_stage.items()))
            return {
                "project": project,
                **stage
            }
    return None

def _set_system_status(
    status: str,
    project: Optional[str] = None,
    error: Optional[str] = None,
    reason: str = "unknown"
):
    global _system_status, _system_status_project, _system_status_error, _system_status_reason, _system_last_update_time
    with _status_lock:
        _system_status = status
        _system_status_project = project
        _system_status_error = error
        _system_status_reason = reason
        _system_last_update_time = time.strftime('%Y-%m-%d %H:%M:%S')
    logger.info(f"系统状态变更: status={status}, reason={reason}, project={project}, error={error}")

def _cancel_idle_timer():
    global _idle_timer
    with _timer_lock:
        if _idle_timer is not None:
            logger.debug("取消空闲定时器")
            _idle_timer.cancel()
            _idle_timer = None

def _reset_stream_state(reason: str = "stream_reset", update_status: bool = True):
    global stream_active, stream_project_name, stream_batch_name
    global stream_comm_sim_rand, stream_comm_sim_opt
    global stream_radar_sim_rand, stream_radar_sim_opt
    global stream_t0_abs, stream_last_t_rel, stream_frame_count
    with stream_lock:
        logger.debug("重置流式状态")
        stream_active = False
        stream_project_name = None
        stream_batch_name = None
        stream_comm_sim_rand = None
        stream_comm_sim_opt = None
        stream_radar_sim_rand = None
        stream_radar_sim_opt = None
        stream_t0_abs = None
        stream_last_t_rel = None
        stream_frame_count = 0
    _cancel_idle_timer()
    if update_status:
        _set_system_status("idle", None, None, reason=reason)

def _on_idle_timeout():
    with stream_lock:
        if not stream_active:
            logger.debug("空闲超时但流已非活跃，忽略")
            return
    with finalizing_lock:
        if finalizing:
            logger.debug("空闲超时但已有最终化进程，忽略")
            return
    logger.info(f"空闲超时 {_IDLE_TIMEOUT} 秒，触发数据流结束")
    _finalize_stream(idle_reason="timeout_finished")

def _reset_idle_timer():
    global _idle_timer
    with _timer_lock:
        if _idle_timer is not None:
            _idle_timer.cancel()
        _idle_timer = threading.Timer(_IDLE_TIMEOUT, _on_idle_timeout)
        _idle_timer.daemon = True
        _idle_timer.start()
        logger.debug(f"空闲定时器已重置，{_IDLE_TIMEOUT} 秒后触发")

# ---------- 紧急发送：不丢步数据，仅优先直接发送 ----------
def send_urgent_final(data: dict, topic: str = "vi_decision_res"):
    """直接尝试发送最终结果，若失败则放入队列（不清空已有消息）"""
    payload = json.dumps(data, ensure_ascii=False)
    with _mqtt_lock:
        connected = _mqtt_connected
    if connected:
        try:
            result = mqtt_receiver.publish(topic, payload, qos=1)
            rc, mid = _parse_publish_result(result)
            if rc is not None and rc == 0:
                logger.info(f"紧急发送成功（直接）: 主题={topic}, 消息ID={mid}, 大小={len(payload)} 字节")
                _original_print(f"[URGENT] 直接发送成功，消息ID={mid}")
                return
            else:
                logger.error(f"紧急发送直接发送失败，返回码={rc}，将放入队列重试")
                _original_print(f"[URGENT] 直接发送失败，放入队列")
        except Exception as e:
            logger.error(f"紧急发送直接发送异常: {e}", exc_info=True)
            _original_print(f"[URGENT] 直接发送异常，放入队列: {e}")

    # 后备：放入队列
    mqtt_send_queue.put((topic, payload))
    logger.info(f"紧急发送：最终结果已放入队列，主题={topic}，大小={len(payload)} 字节")
    _original_print("[URGENT] 最终结果已加入发送队列（将按序发送）")

def send_to_target(data: dict, topic: str = "vi_decision_res") -> bool:
    try:
        payload = json.dumps(data, ensure_ascii=False)
        mqtt_send_queue.put((topic, payload))
        logger.info(f"消息已加入发送队列，主题: {topic}，大小: {len(payload)} 字节")
        return True
    except Exception as e:
        logger.error(f"消息入队失败: {e}", exc_info=True)
        return False

# ---------- 最终化流程 ----------
def _finalize_stream(idle_reason: str = "normal_finished"):
    global finalizing
    with finalizing_lock:
        if finalizing:
            logger.warning("已有最终化进程正在执行，跳过本次触发")
            return
        finalizing = True

    finalize_success = False
    try:
        global stream_active, stream_project_name, stream_batch_name
        global stream_comm_sim_rand, stream_comm_sim_opt
        global stream_radar_sim_rand, stream_radar_sim_opt
        global stream_last_t_rel

        logger.info("========== 开始最终化数据流 ==========")
        proj = None
        batch = DEFAULT_BATCH
        with stream_lock:
            has_sims = any(
                sim is not None
                for sim in (
                    stream_comm_sim_rand,
                    stream_comm_sim_opt,
                    stream_radar_sim_rand,
                    stream_radar_sim_opt,
                )
            )
            if not stream_active and not has_sims:
                logger.warning("流式状态未激活且无模拟器，跳过最终化")
                return
            comm_rand = stream_comm_sim_rand
            comm_opt = stream_comm_sim_opt
            radar_rand = stream_radar_sim_rand
            radar_opt = stream_radar_sim_opt
            proj = stream_project_name
            batch = stream_batch_name or get_batch_for_project(proj or DEFAULT_PROJECT)
            stream_active = False
            stream_project_name = None
            stream_batch_name = None
            stream_comm_sim_rand = None
            stream_comm_sim_opt = None
            stream_radar_sim_rand = None
            stream_radar_sim_opt = None
            last_t_rel = stream_last_t_rel
            stream_last_t_rel = None
        _cancel_idle_timer()

        active_sims = [
            (comm_rand, "通信随机"),
            (comm_opt, "通信优化"),
            (radar_rand, "雷达随机"),
            (radar_opt, "雷达优化"),
        ]
        sims_to_finalize = [(sim, name) for sim, name in active_sims if sim is not None]
        if not sims_to_finalize:
            logger.warning("没有可最终化的流式模拟器，跳过最终化")
            return

        def get_metrics(sim, name):
            try:
                logger.debug(f"开始获取 {name} 最终指标...")
                metrics = sim.finalize()
                logger.debug(f"{name} 最终指标获取成功")
                return metrics
            except Exception as e:
                logger.error(f"{name} 决策最终化失败: {e}", exc_info=True)
                return None

        metrics_by_name: Dict[str, Optional[dict]] = {}
        with concurrent.futures.ThreadPoolExecutor(max_workers=max(len(sims_to_finalize), 1)) as executor:
            future_map = {
                executor.submit(get_metrics, sim, name): name
                for sim, name in sims_to_finalize
            }
            for future in concurrent.futures.as_completed(future_map):
                name = future_map[future]
                metrics_by_name[name] = future.result(timeout=30)

        comm_rand_metrics = metrics_by_name.get("通信随机")
        comm_opt_metrics = metrics_by_name.get("通信优化")
        radar_rand_metrics = metrics_by_name.get("雷达随机")
        radar_opt_metrics = metrics_by_name.get("雷达优化")

        if last_t_rel is not None:
            logger.info(f"数据流实际处理的最大相对时间: {last_t_rel:.2f}s")
            _original_print(f"[FINALIZE] 实际最大帧时间 = {last_t_rel:.2f}s")
            for label, metrics in [("通信随机", comm_rand_metrics), ("通信优化", comm_opt_metrics),
                                   ("雷达随机", radar_rand_metrics), ("雷达优化", radar_opt_metrics)]:
                if metrics:
                    total_time = metrics.get('overall', {}).get('total_time')
                    if total_time is not None and abs(total_time - last_t_rel) > 5:
                        logger.warning(f"{label}: 模拟总时间={total_time}s 与最后帧时间={last_t_rel}s 差异较大，可能存在数据丢失")
                    elif total_time is not None:
                        logger.debug(f"{label}: 模拟总时间={total_time}s，匹配")
        else:
            logger.warning("未记录到任何有效帧时间，数据流可能为空")

        def percent_change(orig, opt):
            if orig == 0:
                return 0.0 if opt == 0 else float('inf')
            return (opt - orig) / orig * 100.0

        if comm_rand_metrics and comm_opt_metrics:
            overall_rand = comm_rand_metrics.get('overall', {})
            overall_opt = comm_opt_metrics.get('overall', {})
            comm_comparison = {
                'jam_duration_improvement_percent': percent_change(
                    overall_rand.get('jam_duration', 0),
                    overall_opt.get('jam_duration', 0)
                ),
                'effective_jamming_length_improvement_percent': percent_change(
                    overall_rand.get('effective_jamming_length', 0),
                    overall_opt.get('effective_jamming_length', 0)
                ),
                'effective_coverage_area_improvement_percent': percent_change(
                    overall_rand.get('effective_coverage_area', 0),
                    overall_opt.get('effective_coverage_area', 0)
                )
            }
            comm_metrics_full = {
                'random_deployment': comm_rand_metrics,
                'optimized_deployment': comm_opt_metrics,
                'comparison': comm_comparison
            }
        else:
            comm_metrics_full = None

        if radar_rand_metrics and radar_opt_metrics:
            overall_rand = radar_rand_metrics.get('overall', {})
            overall_opt = radar_opt_metrics.get('overall', {})
            radar_comparison = {
                'jam_duration_improvement_percent': percent_change(
                    overall_rand.get('jam_duration', 0),
                    overall_opt.get('jam_duration', 0)
                ),
                'effective_jamming_length_improvement_percent': percent_change(
                    overall_rand.get('effective_jamming_length', 0),
                    overall_opt.get('effective_jamming_length', 0)
                ),
                'effective_coverage_area_improvement_percent': percent_change(
                    overall_rand.get('effective_coverage_area', 0),
                    overall_opt.get('effective_coverage_area', 0)
                )
            }
            radar_metrics_full = {
                'random_deployment': radar_rand_metrics,
                'optimized_deployment': radar_opt_metrics,
                'comparison': radar_comparison
            }
        else:
            radar_metrics_full = None

        proj_dir = Path(f"data/{proj or 'default'}")
        comm_comp_file = proj_dir / "communication_data" / "comparison_metrics.json"
        radar_comp_file = proj_dir / "radar_data" / "comparison_metrics.json"
        comm_comp_file.parent.mkdir(parents=True, exist_ok=True)
        radar_comp_file.parent.mkdir(parents=True, exist_ok=True)

        if comm_metrics_full:
            try:
                with open(comm_comp_file, 'w', encoding='utf-8') as f:
                    json.dump(comm_metrics_full, f, ensure_ascii=False, indent=2)
                logger.info(f"通信对比结果已保存至: {comm_comp_file}")
            except Exception as e:
                logger.error(f"保存通信对比文件失败: {e}")

        if radar_metrics_full:
            try:
                with open(radar_comp_file, 'w', encoding='utf-8') as f:
                    json.dump(radar_metrics_full, f, ensure_ascii=False, indent=2)
                logger.info(f"雷达对比结果已保存至: {radar_comp_file}")
            except Exception as e:
                logger.error(f"保存雷达对比文件失败: {e}")

        proj_out = proj or DEFAULT_PROJECT
        batch_out = batch or DEFAULT_BATCH
        result = {
            "Status": True,
            "Message": "应对决策处理完成",
            "Timestamp": time.strftime('%Y-%m-%d %H:%M:%S'),
            "projectname": proj_out,
            "batchname": batch_out,
            "type": "decision_result",
            "CommunicationMetrics": comm_metrics_full,
            "RadarMetrics": radar_metrics_full,
            "CommunicationMetricsFile": str(comm_comp_file) if comm_metrics_full else None,
            "RadarMetricsFile": str(radar_comp_file) if radar_metrics_full else None
        }

        logger.info(f"最终结果已构建，大小约 {len(json.dumps(result, ensure_ascii=False))} 字节")
        _original_print("[FINALIZE] 即将发送最终结果（优先直接，不清空步数据）")
        send_urgent_final(result, "vi_decision_res")
        finalize_success = True

    except Exception as e:
        logger.error(f"最终化流程异常: {e}", exc_info=True)
        _set_system_status("error", proj, str(e), reason="finalize_exception")
    finally:
        if proj:
            _clear_stage(proj)
        if finalize_success:
            _set_system_status("idle", None, None, reason=idle_reason)
        with finalizing_lock:
            finalizing = False

# ---------- 辅助函数 ----------
def calc_improvement(orig_val, opt_val):
    diff = opt_val - orig_val
    if abs(diff) < 1e-6:
        return 0.0
    return round(diff, 4)

def save_jammer_positions_to_files(config, comm_original, comm_optimized, radar_original, radar_optimized):
    project_name, batch_name = sync_project_batch(config)
    ensure_scene_metadata(config, project_name, batch_name)
    project_dir = Path(f"data/{project_name}")
    project_dir.mkdir(parents=True, exist_ok=True)

    def convert_name_to_showName(jammer_list):
        new_list = []
        for j in jammer_list:
            new_j = j.copy()
            if 'name' in new_j:
                new_j['showName'] = new_j.pop('name')
            new_list.append(new_j)
        return new_list

    comm_original_show = convert_name_to_showName(comm_original or [])
    comm_optimized_show = convert_name_to_showName(comm_optimized or [])
    radar_original_show = convert_name_to_showName(radar_original or [])
    radar_optimized_show = convert_name_to_showName(radar_optimized or [])

    comm_file = project_dir / "communication_jammer_positions.json"
    comm_data = {
        "scene": config.get("scene", {}),
        "guardpoints": config.get("guardPoints", []),
        "originalJammers": comm_original_show,
        "optimizedJammers": comm_optimized_show
    }
    with open(comm_file, 'w', encoding='utf-8') as f:
        json.dump(comm_data, f, ensure_ascii=False, indent=2)

    radar_file = project_dir / "radar_jammer_positions.json"
    radar_data = {
        "scene": config.get("scene", {}),
        "guardpoints": config.get("guardPoints", []),
        "originaljammers": radar_original_show,
        "optimizedjammers": radar_optimized_show
    }
    with open(radar_file, 'w', encoding='utf-8') as f:
        json.dump(radar_data, f, ensure_ascii=False, indent=2)

    logger.info(
        "干扰机位置已保存至 %s projectname=%s batchname=%s（随机→original*，优化→optimized*；通信 %d/%d，雷达 %d/%d）",
        project_dir,
        project_name,
        batch_name,
        len(comm_original_show),
        len(comm_optimized_show),
        len(radar_original_show),
        len(radar_optimized_show),
    )

def _jammer_entries_from_json(data: dict, *keys: str) -> List[dict]:
    for key in keys:
        entries = data.get(key)
        if isinstance(entries, list) and entries:
            return entries
    return []

def _project_has_jammer_type(project_name: str, jammer_type: str) -> bool:
    project_dir = Path(f"data/{project_name}")
    if jammer_type == "comm":
        path = project_dir / "communication_jammer_positions.json"
        original_keys = ("originalJammers", "originaljammers")
        optimized_keys = ("optimizedJammers", "optimizedjammers")
    elif jammer_type == "radar":
        path = project_dir / "radar_jammer_positions.json"
        original_keys = ("originaljammers", "originalJammers")
        optimized_keys = ("optimizedjammers", "optimizedJammers")
    else:
        return False
    if not path.exists():
        return False
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, json.JSONDecodeError) as e:
        logger.warning("读取干扰机配置失败 path=%s error=%s", path, e)
        return False
    return bool(
        _jammer_entries_from_json(data, *original_keys)
        or _jammer_entries_from_json(data, *optimized_keys)
    )

def _get_project_jammer_availability(project_name: str) -> Tuple[bool, bool]:
    has_comm = _project_has_jammer_type(project_name, "comm")
    has_radar = _project_has_jammer_type(project_name, "radar")
    return has_comm, has_radar

def _create_scene_projection(center_lon: float, center_lat: float, radius_m: float) -> Transformer:
    wgs84 = CRS.from_epsg(4326)
    if radius_m <= 50000:
        utm_zone = int((center_lon + 180) / 6) + 1
        hemisphere = "north" if center_lat >= 0 else "south"
        proj_crs = CRS.from_string(
            f"+proj=utm +zone={utm_zone} +{hemisphere} +ellps=WGS84 +datum=WGS84 +units=m +no_defs"
        )
    elif radius_m <= 150000:
        proj_crs = CRS.from_string(
            f"+proj=lcc +lat_1={center_lat - 1} +lat_2={center_lat + 1} "
            f"+lat_0={center_lat} +lon_0={center_lon} +x_0=0 +y_0=0 "
            "+ellps=WGS84 +datum=WGS84 +units=m +no_defs"
        )
    else:
        proj_crs = CRS.from_string(
            f"+proj=merc +lat_0={center_lat} +lon_0={center_lon} "
            "+k=1.0 +x_0=0 +y_0=0 +ellps=WGS84 +datum=WGS84 +units=m +no_defs"
        )
    return Transformer.from_crs(wgs84, proj_crs, always_xy=True)

def _generate_random_positions_in_scene_range(
    center_lon: float,
    center_lat: float,
    center_alt: float,
    radius_m: float,
    count: int,
    range_type: str = "circle",
) -> List[dict]:
    if count <= 0:
        return []
    range_type_norm = (range_type or "circle").lower()
    if range_type_norm not in ("circle", "cir", "circular"):
        logger.warning("区域形状 %s 暂不支持，按 circle 处理", range_type)
    transformer = _create_scene_projection(center_lon, center_lat, radius_m)
    center_x, center_y = transformer.transform(center_lon, center_lat)
    positions = []
    for _ in range(count):
        r = radius_m * 0.7 * math.sqrt(random.random())
        theta = random.random() * 2 * math.pi
        proj_x = center_x + r * math.cos(theta)
        proj_y = center_y + r * math.sin(theta)
        lon, lat = transformer.transform(proj_x, proj_y, direction="INVERSE")
        positions.append({
            "longitude": round(lon, 6),
            "latitude": round(lat, 6),
            "altitude": center_alt,
        })
    return positions

def _parse_jammer_start_jammers(data: dict) -> Tuple[List[dict], List[dict]]:
    jammers_dict = data.get("Jammers") or data.get("jammers") or {}
    comm_optimized: List[dict] = []
    radar_optimized: List[dict] = []
    for jammer_data in jammers_dict.values():
        if not isinstance(jammer_data, dict):
            continue
        sensor = jammer_data.get("sensorInfo") or {}
        lon = float(sensor.get("longitude", 0))
        lat = float(sensor.get("latitude", 0))
        alt = float(sensor.get("altitude", 0))
        entry = {
            "uuid": sensor.get("uuid", ""),
            "showName": sensor.get("showName", ""),
            "longitude": round(lon, 6),
            "latitude": round(lat, 6),
            "altitude": alt,
        }
        if "communicationsJammerInfo" in jammer_data:
            info = jammer_data["communicationsJammerInfo"]
            if info.get("jammerType") == 8:
                comm_optimized.append(entry)
        elif "radarJammerInfo" in jammer_data:
            info = jammer_data["radarJammerInfo"]
            if info.get("jammerType") == 7:
                radar_optimized.append(entry)
    return comm_optimized, radar_optimized

def _build_random_from_optimized(
    optimized_list: List[dict],
    random_coords: List[dict],
) -> List[dict]:
    result = []
    for opt, coord in zip(optimized_list, random_coords):
        result.append({
            "uuid": opt["uuid"],
            "showName": opt["showName"],
            "longitude": coord["longitude"],
            "latitude": coord["latitude"],
            "altitude": opt.get("altitude", coord.get("altitude", 0.0)),
        })
    return result

def run_jammer_start_deployment(data: dict) -> dict:
    logger.info("执行 jammerStart（跳过 generate/reGenerate，直接准备决策）")
    project_name, batch_name = sync_project_batch(data)
    scene_raw = ci_get(data, "scene", "Scene") or {}
    if not isinstance(scene_raw, dict):
        scene_raw = {}
    range_info = ci_get(scene_raw, "range", "Range") or {}
    if not isinstance(range_info, dict):
        range_info = {}

    center_lon = float(ci_get(range_info, "longitude", default=0) or 0)
    center_lat = float(ci_get(range_info, "latitude", default=0) or 0)
    center_alt = float(ci_get(range_info, "altitude", default=0) or 0)
    radius_m = float(ci_get(range_info, "radius", default=3000) or 3000)
    range_type = ci_get(range_info, "rangeType", "rangetype", default="circle") or "circle"

    comm_optimized, radar_optimized = _parse_jammer_start_jammers(data)
    logger.info(
        "jammerStart: project=%s, 通信=%d, 雷达=%d, 中心=(%.6f,%.6f), 半径=%sm, 形状=%s",
        project_name,
        len(comm_optimized),
        len(radar_optimized),
        center_lat,
        center_lon,
        radius_m,
        range_type,
    )

    comm_random_coords = _generate_random_positions_in_scene_range(
        center_lon, center_lat, center_alt, radius_m, len(comm_optimized), range_type
    )
    radar_random_coords = _generate_random_positions_in_scene_range(
        center_lon, center_lat, center_alt, radius_m, len(radar_optimized), range_type
    )
    comm_random = _build_random_from_optimized(comm_optimized, comm_random_coords)
    radar_random = _build_random_from_optimized(radar_optimized, radar_random_coords)

    config = {
        "projectname": project_name,
        "batchname": batch_name,
        "scene": {
            "latitude": str(ci_get(range_info, "latitude", default=center_lat)),
            "longitude": str(ci_get(range_info, "longitude", default=center_lon)),
            "altitude": str(ci_get(range_info, "altitude", default=center_alt)),
            "radius": radius_m,
            "projectname": project_name,
            "batchname": batch_name,
        },
        "guardPoints": ci_get(data, "guardPoints", "GuardPoints") or [],
    }

    save_jammer_positions_to_files(
        config=config,
        comm_original=comm_random,
        comm_optimized=comm_optimized,
        radar_original=radar_random,
        radar_optimized=radar_optimized,
    )
    global _pending_random_deployment
    _pending_random_deployment = None
    _on_regenerate_completed(project_name)

    return {
        "Status": True,
        "Message": "jammerStart 部署完成，可接收融合数据",
        "Timestamp": time.strftime('%Y-%m-%d %H:%M:%S'),
        "projectname": project_name,
        "batchname": batch_name,
        "Type": "jammerStart_return",
        "RadarJammerPositions": radar_optimized,
        "CommunicationJammerPositions": comm_optimized,
        "RadarPerformanceMetrics": {},
        "CommunicationPerformanceMetrics": {},
    }

# ---------- 随机部署 ----------
def run_random_deployment(config: dict) -> dict:
    logger.info("执行随机部署 (generate)")
    project_name, batch_name = sync_project_batch(config)
    ensure_scene_metadata(config, project_name, batch_name)
    comm_deployer = CommRandomDeployment(config)
    comm_positions, _ = comm_deployer.generate_deployment()
    comm_result = []
    for pos in comm_positions:
        jam_id = pos['id']
        comm_result.append({
            "uuid": f"comm_{jam_id}",
            "showName": f"通信导航干扰机_{jam_id}",
            "longitude": pos['longitude'],
            "latitude": pos['latitude'],
            "altitude": 0.0
        })
    radar_deployer = RadarRandomDeployment(config)
    radar_positions, _ = radar_deployer.generate_deployment()
    radar_result = []
    for pos in radar_positions:
        jam_id = pos['id']
        radar_result.append({
            "uuid": f"radar_{jam_id}",
            "showName": f"雷达干扰机_{jam_id}",
            "longitude": pos['longitude'],
            "latitude": pos['latitude'],
            "altitude": 0.0
        })

    _store_pending_random_deployment(comm_result, radar_result)
    logger.info("随机部署仅通过 MQTT 返回，不写入磁盘")

    return {
        "Status": True,
        "Message": "随机部署成功",
        "Timestamp": time.strftime('%Y-%m-%d %H:%M:%S'),
        "RadarJammerPositions": radar_result,
        "CommunicationJammerPositions": comm_result,
        "RadarPerformanceMetrics": {},
        "CommunicationPerformanceMetrics": {},
        "projectname": project_name,
        "batchname": batch_name,
        "Type": "deployment_random_return"
    }

# ---------- 优化部署 ----------
def run_optimization_deployment(config: dict) -> dict:
    logger.info("执行优化部署 (reGenerate)")
    project_name, batch_name = sync_project_batch(config)
    ensure_scene_metadata(config, project_name, batch_name)

    comm_original = []
    radar_original = []
    jammer_dict = config.get('jammer', {})
    comm_name_map = {}
    radar_name_map = {}
    for jammer_key, jammer_data in jammer_dict.items():
        sensor = jammer_data.get('sensorInfo', {})
        lon = float(sensor.get('longitude', 0))
        lat = float(sensor.get('latitude', 0))
        alt = float(sensor.get('altitude', 0))
        uuid = sensor.get('uuid', '')
        name = sensor.get('showName', '')
        if 'communicationsJammerInfo' in jammer_data:
            if jammer_data['communicationsJammerInfo'].get('jammerType') == 8:
                comm_original.append({
                    "uuid": uuid,
                    "showName": name,
                    "longitude": round(lon, 6),
                    "latitude": round(lat, 6),
                    "altitude": alt
                })
                comm_name_map[uuid] = name
        elif 'radarJammerInfo' in jammer_data:
            if jammer_data['radarJammerInfo'].get('jammerType') == 7:
                radar_original.append({
                    "uuid": uuid,
                    "showName": name,
                    "longitude": round(lon, 6),
                    "latitude": round(lat, 6),
                    "altitude": alt
                })
                radar_name_map[uuid] = name

    comm_original, radar_original = _load_pending_random_as_original(comm_original, radar_original)
    if not comm_original and not radar_original:
        logger.warning("reGenerate 未解析到随机部署位置（报文 jammer 与内存缓存均为空）")

    if comm_original or radar_original:
        save_jammer_positions_to_files(
            config=config,
            comm_original=comm_original,
            comm_optimized=[j.copy() for j in comm_original],
            radar_original=radar_original,
            radar_optimized=[j.copy() for j in radar_original],
        )
        _on_regenerate_completed(project_name)
        logger.info(
            "reGenerate 原始位置已落盘，部署阶段已就绪 project=%s（优化计算继续进行中）",
            project_name,
        )

    comm_optimized_positions = [j.copy() for j in comm_original]
    radar_optimized_positions = [j.copy() for j in radar_original]
    communication_metrics = {}
    radar_metrics = {}
    opt_success = False

    try:
        logger.info("开始通信干扰机优化...")
        comm_opt = CommOptimization(config=config)
        optimized_comm_xy = comm_opt.optimize_jammer_positions()
        if optimized_comm_xy:
            new_comm_positions = []
            for jammer_xy in optimized_comm_xy:
                uuid = jammer_xy['uuid']
                lon, lat = comm_opt.xy_to_latlon(jammer_xy['x'], jammer_xy['y'])
                show_name = comm_name_map.get(uuid, f"通信导航干扰机_{uuid}")
                new_comm_positions.append({
                    "uuid": uuid,
                    "showName": show_name,
                    "longitude": round(lon, 6),
                    "latitude": round(lat, 6),
                    "altitude": 0.0
                })
            if new_comm_positions:
                comm_optimized_positions = new_comm_positions
                opt_success = True

        orig_guard_cov = comm_opt.calculate_guard_coverage(comm_opt.original_jammers_xy)
        orig_overlap = comm_opt.calculate_jammer_overlap(comm_opt.original_jammers_xy)
        orig_jammer_cov = comm_opt.calculate_jammer_coverage(comm_opt.original_jammers_xy)
        jammers_for_metrics = []
        for pos in comm_optimized_positions:
            x, y = comm_opt.latlon_to_xy(pos['longitude'], pos['latitude'])
            jammers_for_metrics.append({'x': x, 'y': y})
        if jammers_for_metrics:
            opt_guard_cov = comm_opt.calculate_guard_coverage(jammers_for_metrics)
            opt_overlap = comm_opt.calculate_jammer_overlap(jammers_for_metrics)
            opt_jammer_cov = comm_opt.calculate_jammer_coverage(jammers_for_metrics)
        else:
            opt_guard_cov = orig_guard_cov
            opt_overlap = orig_overlap
            opt_jammer_cov = orig_jammer_cov

        communication_metrics = {
            "original": {"guardCoverage": round(orig_guard_cov, 4), "jammerOverlap": round(orig_overlap, 4), "jammerCoverage": round(orig_jammer_cov, 4)},
            "optimized": {"guardCoverage": round(opt_guard_cov, 4), "jammerOverlap": round(opt_overlap, 4), "jammerCoverage": round(opt_jammer_cov, 4)},
            "improvement": {
                "guardCoverage": calc_improvement(orig_guard_cov, opt_guard_cov),
                "jammerOverlap": calc_improvement(orig_overlap, opt_overlap),
                "jammerCoverage": calc_improvement(orig_jammer_cov, opt_jammer_cov)
            }
        }

        logger.info("开始雷达干扰机优化...")
        radar_opt = RadarOptimization(config=config)
        optimized_radar_xy = radar_opt.optimize_jammer_positions()
        if optimized_radar_xy:
            new_radar_positions = []
            for jammer_xy in optimized_radar_xy:
                uuid = jammer_xy['uuid']
                lon, lat = radar_opt.xy_to_latlon(jammer_xy['x'], jammer_xy['y'])
                show_name = radar_name_map.get(uuid, f"雷达干扰机_{uuid}")
                new_radar_positions.append({
                    "uuid": uuid,
                    "showName": show_name,
                    "longitude": round(lon, 6),
                    "latitude": round(lat, 6),
                    "altitude": 0.0
                })
            if new_radar_positions:
                radar_optimized_positions = new_radar_positions
                opt_success = True

        r_orig_guard = radar_opt.calculate_guard_coverage(radar_opt.original_jammers_xy)
        r_orig_area = radar_opt.calculate_area_coverage(radar_opt.original_jammers_xy)
        r_orig_overlap = radar_opt.calculate_jammer_overlap(radar_opt.original_jammers_xy)
        radar_jammers_for_metrics = []
        for pos in radar_optimized_positions:
            x, y = radar_opt.latlon_to_xy(pos['longitude'], pos['latitude'])
            radar_jammers_for_metrics.append({'x': x, 'y': y})
        if radar_jammers_for_metrics:
            r_opt_guard = radar_opt.calculate_guard_coverage(radar_jammers_for_metrics)
            r_opt_area = radar_opt.calculate_area_coverage(radar_jammers_for_metrics)
            r_opt_overlap = radar_opt.calculate_jammer_overlap(radar_jammers_for_metrics)
        else:
            r_opt_guard = r_orig_guard
            r_opt_area = r_orig_area
            r_opt_overlap = r_orig_overlap

        radar_metrics = {
            "original": {"guardCoverage": round(r_orig_guard, 4), "jammerOverlap": round(r_orig_overlap, 4), "jammerCoverage": round(r_orig_area, 4)},
            "optimized": {"guardCoverage": round(r_opt_guard, 4), "jammerOverlap": round(r_opt_overlap, 4), "jammerCoverage": round(r_opt_area, 4)},
            "improvement": {
                "guardCoverage": calc_improvement(r_orig_guard, r_opt_guard),
                "jammerOverlap": calc_improvement(r_orig_overlap, r_opt_overlap),
                "jammerCoverage": calc_improvement(r_orig_area, r_opt_area)
            }
        }

        logger.info("优化计算成功完成")
    except Exception as e:
        logger.error(f"优化过程中发生异常: {e}", exc_info=True)
        logger.warning("将使用原始位置作为优化结果")
        comm_optimized_positions = [j.copy() for j in comm_original]
        radar_optimized_positions = [j.copy() for j in radar_original]

    save_jammer_positions_to_files(
        config=config,
        comm_original=comm_original,
        comm_optimized=comm_optimized_positions,
        radar_original=radar_original,
        radar_optimized=radar_optimized_positions,
    )
    global _pending_random_deployment
    _pending_random_deployment = None

    if opt_success and (comm_optimized_positions != comm_original or radar_optimized_positions != radar_original):
        msg = "优化部署成功"
    else:
        msg = "优化部署完成（未获得更优位置，使用原始位置）"

    return {
        "Status": True,
        "Message": msg,
        "Timestamp": time.strftime('%Y-%m-%d %H:%M:%S'),
        "RadarJammerPositions": radar_optimized_positions,
        "CommunicationJammerPositions": comm_optimized_positions,
        "RadarPerformanceMetrics": radar_metrics if radar_metrics else {},
        "CommunicationPerformanceMetrics": communication_metrics if communication_metrics else {},
        "projectname": project_name,
        "batchname": batch_name,
        "Type": "deployment_return"
    }

# ---------- MQTT 消息处理 ----------
@mqtt_receiver.on_connect()
def on_connect(client, userdata, flags, rc):
    global _mqtt_connected
    if rc == 0:
        with _mqtt_lock:
            if not _mqtt_connected:
                loop_threads = _mqtt_loop_thread_names()
                if len(loop_threads) > 1:
                    logger.warning("MQTT 连接时发现多个 loop 线程，执行重置: %s", loop_threads)
                    _ensure_single_mqtt_network_loop()
                    loop_threads = _mqtt_loop_thread_names()
                logger.info(
                    "MQTT 连接成功（client_id=%s, pid=%s, loop_threads=%s）",
                    app.config.get('MQTT_CLIENT_ID'),
                    os.getpid(),
                    loop_threads or "(无)",
                )
                _mqtt_connected = True
    else:
        logger.error(f"连接失败，返回码: {rc}")

@mqtt_receiver.on_disconnect()
def on_disconnect():
    global _mqtt_connected
    with _mqtt_lock:
        _mqtt_connected = False
    logger.warning("MQTT 连接已断开")

@mqtt_receiver.on_message()
def on_message(client, userdata, message):
    claim_key = _try_claim_mqtt_message(message.topic, message.payload)
    if claim_key is None:
        logger.info(
            "跳过重复 MQTT 消息 topic=%s size=%s thread=%s",
            message.topic,
            len(message.payload),
            threading.current_thread().name,
        )
        return

    try:
        if not _bind_mqtt_dispatch_thread():
            return

        payload = message.payload.decode()
        raw_data = json.loads(payload)
        logger.info(f"收到消息 [{message.topic}]: {payload[:300]}...")

        if isinstance(raw_data, list):
            for item in raw_data:
                logger.info(
                    "MQTT消息摘要: topic=%s type=%s project=%s sensor_type=%s payload_size=%s",
                    message.topic,
                    _get_message_type(item),
                    _extract_project_name(item, default=""),
                    ci_get(item, "sensor_type", "Sensor_Type"),
                    len(message.payload)
                )
                _process_single_message(message.topic, item)
        else:
            logger.info(
                "MQTT消息摘要: topic=%s type=%s project=%s sensor_type=%s payload_size=%s",
                message.topic,
                _get_message_type(raw_data),
                _extract_project_name(raw_data, default=""),
                ci_get(raw_data, "sensor_type", "Sensor_Type"),
                len(message.payload)
            )
            _process_single_message(message.topic, raw_data)
    except Exception as e:
        logger.error(f"消息处理出错: {e}", exc_info=True)
    finally:
        _release_mqtt_claim(claim_key)

def _process_single_message(topic, data):
    with _mqtt_dispatch_lock:
        _process_single_message_unsafe(topic, data)

def _process_single_message_unsafe(topic, data):
    global stream_active, stream_project_name, stream_batch_name
    global stream_comm_sim_rand, stream_comm_sim_opt
    global stream_radar_sim_rand, stream_radar_sim_opt
    global stream_t0_abs, stream_last_t_rel, stream_frame_count
    global finalizing

    msg_type = _get_message_type(data)
    topic_is_vi_decision = topic == "vi_decision" or topic.startswith("vi_decision/")
    topic_is_pg_finish = topic == "pg_data_processor_finish" or topic.startswith("pg_data_processor_finish/")

    # fusion_end 优先处理：立即取消空闲定时器并触发最终化
    if msg_type == "fusion_end":
        _end_fusion_stream(data, topic=topic)
        return

    # 部署消息处理
    if _is_deployment_message(msg_type):
        if not topic_is_vi_decision:
            logger.warning(
                "部署消息 topic=%s type=%s project=%s（非 vi_decision，仍按部署流程处理）",
                topic,
                msg_type,
                _extract_project_name(data),
            )
        if msg_type == "generate":
            logger.info(
                "收到部署消息 topic=%s type=%s project=%s",
                topic, msg_type, _extract_project_name(data),
            )
            result = run_random_deployment(data)
            send_to_target(result)
            _on_generate_completed()
            _log_flow_stage_snapshot("generate 完成")
            return
        elif msg_type == "reGenerate":
            proj = _extract_project_name(data)
            logger.info("收到部署消息 topic=%s type=%s project=%s", topic, msg_type, proj)
            try:
                result = run_optimization_deployment(data)
                send_to_target(result)
            except Exception:
                logger.error("reGenerate 处理异常 project=%s", proj, exc_info=True)
                raise
            finally:
                _on_regenerate_completed(proj)
            _log_flow_stage_snapshot("reGenerate 完成")
            return
        elif msg_type == "jammerStart":
            proj = _extract_project_name(data)
            logger.info("收到部署消息 topic=%s type=%s project=%s", topic, msg_type, proj)
            result = None
            try:
                result = run_jammer_start_deployment(data)
                send_to_target(result)
            except Exception:
                logger.error("jammerStart 处理异常 project=%s", proj, exc_info=True)
                raise
            finally:
                _on_regenerate_completed((result or {}).get("projectname") or proj)
            _log_flow_stage_snapshot("jammerStart 完成")
            return

    # 手动调试触发最终化
    if msg_type == "debug_finalize":
        logger.info("收到调试指令，手动触发最终化")
        _finalize_stream(idle_reason="debug_finalize")
        return

    # 融合数据流处理
    is_simdata = False
    if topic_is_pg_finish or topic_is_vi_decision:
        sensor_type = str(ci_get(data, "sensor_type", "Sensor_Type", default="") or "").upper()
        if msg_type == "simData" and sensor_type == "AA00":
            is_simdata = True

    if is_simdata:
        with finalizing_lock:
            if finalizing:
                logger.debug("正在最终化，忽略新到达的 simData 帧")
                return

        proj, batch = sync_project_batch(data)
        if not _can_start_decision(proj):
            with _flow_stage_lock:
                stage = _project_flow_stage.get(proj, {})
                logger.warning(
                    "融合数据到达但部署阶段未完成，忽略。topic=%s project=%s，"
                    "generate_done=%s，regenerate_done=%s（需先完成 generate+reGenerate 或 jammerStart）；"
                    "当前内存中已登记项目=%s",
                    topic,
                    proj,
                    stage.get("generate_done", False),
                    stage.get("regenerate_done", False),
                    list(_project_flow_stage.keys()) or "(无)",
                )
            return
        logger.debug(f"处理 simData，项目: {proj}")

        frame_time = str(ci_get(data, "time", "Time", default="") or "")
        t_abs = communication_decision.parse_timestamp_to_seconds(frame_time)
        if t_abs == 0.0:
            t_abs = time.time()
            logger.warning(f"时间解析失败，使用当前时间: {t_abs}，原始时间串: {frame_time}")

        with stream_lock:
            if not stream_active or stream_project_name != proj:
                logger.info(
                    "开始新数据流，项目: %s batch: %s (原项目: %s)",
                    proj, batch, stream_project_name,
                )
                _reset_stream_state(reason="stream_switch")
                stream_active = True
                stream_project_name = proj
                stream_batch_name = batch
                stream_t0_abs = t_abs

                # 修改：所有步数据回调统一发送到 vi_decision_res
                cb_comm_rand = lambda data: send_to_target(data, "vi_decision_res")
                cb_comm_opt = lambda data: send_to_target(data, "vi_decision_res")
                cb_radar_rand = lambda data: send_to_target(data, "vi_decision_res")
                cb_radar_opt = lambda data: send_to_target(data, "vi_decision_res")

                has_comm, has_radar = _get_project_jammer_availability(proj)
                if not has_comm and not has_radar:
                    logger.error(
                        "项目 %s 未配置任何干扰机（通信/雷达均为空），无法创建流式模拟器",
                        proj,
                    )
                    _set_system_status(
                        "error",
                        proj,
                        "未配置任何干扰机（通信/雷达均为空）",
                        reason="simulator_init_failed",
                    )
                    _reset_stream_state(reason="error_cleanup", update_status=False)
                    return

                try:
                    stream_comm_sim_rand = None
                    stream_comm_sim_opt = None
                    stream_radar_sim_rand = None
                    stream_radar_sim_opt = None

                    if has_comm:
                        stream_comm_sim_rand = communication_decision.CommunicationJammerSimulation.create_stream_simulator(
                            project_name=proj, deployment='random', silent=False, mqtt_callback=cb_comm_rand,
                            batch_name=batch,
                        )
                        stream_comm_sim_opt = communication_decision.CommunicationJammerSimulation.create_stream_simulator(
                            project_name=proj, deployment='optimized', silent=False, mqtt_callback=cb_comm_opt,
                            batch_name=batch,
                        )
                    else:
                        logger.info("项目 %s 无通信干扰机，跳过通信流式模拟器", proj)

                    if has_radar:
                        stream_radar_sim_rand = radar_decision.RadarJammerSimulation.create_stream_simulator(
                            project_name=proj, deployment='random', silent=False, mqtt_callback=cb_radar_rand,
                            batch_name=batch,
                        )
                        stream_radar_sim_opt = radar_decision.RadarJammerSimulation.create_stream_simulator(
                            project_name=proj, deployment='optimized', silent=False, mqtt_callback=cb_radar_opt,
                            batch_name=batch,
                        )
                    else:
                        logger.info("项目 %s 无雷达干扰机，跳过雷达流式模拟器", proj)

                    logger.info(
                        "流式模拟器创建成功 project=%s（通信=%s，雷达=%s），步数据将发往 vi_decision_res",
                        proj,
                        "启用" if has_comm else "跳过",
                        "启用" if has_radar else "跳过",
                    )
                    _set_system_status("running", proj, None, reason="first_simdata_started")
                except Exception as e:
                    logger.error(f"创建流式模拟器失败: {e}", exc_info=True)
                    _set_system_status("error", proj, str(e), reason="simulator_init_failed")
                    _reset_stream_state(reason="error_cleanup", update_status=False)
                    return
            else:
                stream_batch_name = batch
                for sim in (
                    stream_comm_sim_rand, stream_comm_sim_opt,
                    stream_radar_sim_rand, stream_radar_sim_opt,
                ):
                    if sim is not None:
                        sim.project_name = proj
                        sim.batch_name = batch

            t_rel = t_abs - stream_t0_abs
            stream_last_t_rel = t_rel
            stream_frame_count += 1
            _touch_system_activity()
            logger.info(f"当前帧相对时间: {t_rel:.2f} 秒")
            _original_print(f"[FRAME] 相对时间={t_rel:.2f}s, 项目={proj}")

            sims = [
                (stream_comm_sim_rand, "通信随机"),
                (stream_comm_sim_opt, "通信优化"),
                (stream_radar_sim_rand, "雷达随机"),
                (stream_radar_sim_opt, "雷达优化")
            ]
            for sim, name in sims:
                if sim is not None:
                    try:
                        sim.update_with_frame(data, t_rel)
                    except Exception as e:
                        logger.error(f"{name} 增量更新失败: {e}", exc_info=True)
                        _set_system_status("error", proj, f"{name} 增量更新失败: {e}", reason="frame_update_failed")

        _reset_idle_timer()
        return

@app.route('/api/health', methods=['GET'])
def health():
    with _status_lock:
        system_status = _system_status
        system_reason = _system_status_reason
        system_error = _system_status_error
    with _mqtt_lock:
        mqtt_connected = _mqtt_connected

    timestamp = time.strftime('%Y-%m-%d %H:%M:%S')
    details = {
        "mqtt_connected": mqtt_connected,
        "system_status": system_status,
        "state_reason": system_reason
    }
    if system_status == "error" or not mqtt_connected:
        message = system_error or ("MQTT disconnected" if not mqtt_connected else "system in error state")
        return jsonify({
            "status": "unhealthy",
            "message": message,
            "timestamp": timestamp,
            "details": details
        })

    return jsonify({
        "status": "ok",
        "timestamp": timestamp,
        "details": details
    })

@app.route('/api/status', methods=['GET'])
def get_status():
    with _status_lock:
        status_payload = {
            "status": _system_status,
            "state_reason": _system_status_reason,
            "project": _system_status_project,
            "error": _system_status_error,
            "timestamp": time.strftime('%Y-%m-%d %H:%M:%S'),
            "last_update_time": _system_last_update_time,
        }
    with stream_lock:
        status_payload["frame_count"] = stream_frame_count
    status_payload["flow_stage"] = _get_current_flow_stage()
    return jsonify(status_payload)

@app.route('/admin', methods=['GET'])
def admin_config_page():
    return render_template('admin_config.html')


@app.route('/api/config', methods=['GET'])
def get_runtime_config():
    config = load_config()
    with _mqtt_lock:
        mqtt_connected = _mqtt_connected
    with _status_lock:
        system_status = _system_status
        state_reason = _system_status_reason

    return jsonify({
        "success": True,
        "config": public_config(config),
        "mqtt_connected": mqtt_connected,
        "system_status": system_status,
        "state_reason": state_reason,
        "restart_hint": "修改 PORT / HOST_PORT 后请在宿主机执行: docker compose up -d"
    })


@app.route('/api/config', methods=['POST'])
def update_runtime_config():
    payload = request.get_json(silent=True) or {}
    if not payload:
        return jsonify({"success": False, "message": "请求体不能为空"}), 400

    current = load_config()
    updates = {}
    for key in ("HOST", "PORT", "HOST_PORT", "SOURCE_BROKER", "SOURCE_PORT", "SOURCE_USERNAME", "SOURCE_PASSWORD"):
        if key in payload and payload[key] is not None:
            updates[key] = str(payload[key]).strip()

    merged_preview = {**current, **updates}
    err = validate_config(merged_preview)
    if err:
        return jsonify({"success": False, "message": err}), 400

    old_config = dict(current)
    try:
        saved = save_config(updates)
    except OSError as e:
        logger.error(f"保存配置失败: {e}", exc_info=True)
        return jsonify({"success": False, "message": f"保存配置失败: {e}"}), 500

    mqtt_keys = {"SOURCE_BROKER", "SOURCE_PORT", "SOURCE_USERNAME", "SOURCE_PASSWORD"}
    port_keys = {"PORT", "HOST_PORT", "HOST"}
    mqtt_changed = any(str(old_config.get(k, "")) != str(saved.get(k, "")) for k in mqtt_keys)
    port_changed = any(str(old_config.get(k, "")) != str(saved.get(k, "")) for k in port_keys)

    mqtt_reloaded = False
    if mqtt_changed:
        mqtt_reloaded = reconnect_mqtt()

    messages = ["配置已保存"]
    if mqtt_changed:
        messages.append("MQTT 已尝试重连" if mqtt_reloaded else "MQTT 重连失败，请检查 Broker 地址与端口")
    if port_changed:
        messages.append("端口配置已更新，需重启容器后生效")

    return jsonify({
        "success": True,
        "message": "；".join(messages),
        "config": public_config(saved),
        "mqtt_reloaded": mqtt_reloaded,
        "requires_container_restart": port_changed,
        "restart_hint": "在宿主机项目目录执行: docker compose up -d"
    })


@app.route('/api/fusion/decision/process/list', methods=['POST'])
def list_decision_process_simulation():
    """查询 fusion.decision_result_simulation 中的 projectname、batchname 列表。"""
    try:
        data = query_decision_result_project_batch_list()
        return jsonify({
            "code": 200,
            "data": data,
            "msg": "查询成功",
        })
    except Exception as e:
        logger.error(f"查询 decision_result_simulation 项目批次列表失败: {e}", exc_info=True)
        return jsonify({
            "code": 500,
            "data": {
                "columnList": [],
                "data": [],
                "total": 0,
            },
            "msg": f"查询失败: {e}",
        }), 500


@app.route('/api/fusion/decision/result/query', methods=['POST'])
def query_decision_result_simulation_api():
    """按 projectname、batchname 查询 fusion.decision_result_simulation。"""
    payload = request.get_json(silent=True) or {}
    projectname = str(payload.get("projectname") or "").strip()
    batchname = str(payload.get("batchname") or "").strip()

    if not projectname or not batchname:
        return jsonify({
            "code": 400,
            "data": {
                "results": [],
                "total": 0,
            },
            "msg": "projectname 和 batchname 不能为空",
        }), 400

    try:
        data = query_decision_result_simulation(projectname, batchname)
        return jsonify({
            "code": 200,
            "data": data,
            "msg": "查询成功",
        })
    except Exception as e:
        logger.error(
            "查询 decision_result_simulation 失败: project=%s batch=%s error=%s",
            projectname,
            batchname,
            e,
            exc_info=True,
        )
        return jsonify({
            "code": 500,
            "data": {
                "results": [],
                "total": 0,
            },
            "msg": f"查询失败: {e}",
        }), 500


@app.route('/api/reset', methods=['POST'])
def reset_system():
    with _status_lock:
        current_project = _system_status_project
    try:
        logger.warning("收到外部重置请求，执行流状态重置")
        _reset_stream_state(reason="manual_reset")
        _clear_stage()
        return jsonify({
            "success": True,
            "message": "已终止当前任务，系统已重置为空闲状态"
        })
    except Exception as e:
        logger.error(f"重置失败: {e}", exc_info=True)
        _set_system_status("error", current_project, str(e), reason="manual_reset_failed")
        return jsonify({
            "success": False,
            "message": f"重置失败: {str(e)}"
        }), 500

if __name__ == '__main__':
    host = os.getenv("HOST", "0.0.0.0")
    try:
        port = int(os.getenv("PORT", "17686"))
    except ValueError:
        logger.warning("环境变量 PORT 非法，回退到默认端口 17686")
        port = 17686
    logger.info(
        "启动部署决策服务（统一主题 vi_decision_res），监听地址: %s:%s，pid=%s，mqtt_client_id=%s",
        host,
        port,
        os.getpid(),
        app.config.get('MQTT_CLIENT_ID'),
    )
    try:
        from waitress import serve
        logger.info("使用 Waitress 作为 WSGI 服务启动")
        serve(app, host=host, port=port, threads=8)
    except ImportError:
        logger.warning("未安装 waitress，回退到 Flask 开发服务器")
        try:
            app.run(host=host, port=port, threaded=True, debug=False, use_reloader=False)
        except OSError as e:
            logger.error(f"Flask服务启动失败，可能是端口占用或地址不可用: host={host}, port={port}, error={e}", exc_info=True)
            raise
    except OSError as e:
        logger.error(f"服务启动失败，可能是端口占用或地址不可用: host={host}, port={port}, error={e}", exc_info=True)
        raise
    except Exception as e:
        logger.error(f"服务启动发生未预期异常: host={host}, port={port}, error={e}", exc_info=True)
        raise