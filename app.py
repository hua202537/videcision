import os
import json
import time
import math
import random
import logging
import builtins
import sys
import io
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from pyproj import Transformer, CRS
from logging.handlers import RotatingFileHandler
from queue import Queue

from flask import Flask, jsonify, request, g
from flask_mqtt import Mqtt
from flask_cors import CORS
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

# ---------- MQTT 配置 ----------
#app.config['MQTT_BROKER_URL'] = os.getenv("SOURCE_BROKER", "127.0.0.1")
#app.config['MQTT_BROKER_PORT'] = int(os.getenv("SOURCE_PORT", 1883))
#app.config['MQTT_USERNAME'] = os.getenv("SOURCE_USERNAME", "1")
#app.config['MQTT_PASSWORD'] = os.getenv("SOURCE_PASSWORD", "1")
#app.config['MQTT_KEEPALIVE'] = 120
#app.config['MQTT_TLS_ENABLED'] = False


app.config['MQTT_BROKER_URL'] = os.getenv("SOURCE_BROKER", "172.16.10.13")
app.config['MQTT_BROKER_PORT'] = int(os.getenv("SOURCE_PORT", 30502))
app.config['MQTT_USERNAME'] = os.getenv("SOURCE_USERNAME", "test")
app.config['MQTT_PASSWORD'] = os.getenv("SOURCE_PASSWORD", "test")
app.config['MQTT_KEEPALIVE'] = 120
app.config['MQTT_TLS_ENABLED'] = False

mqtt_receiver = Mqtt(app)
mqtt_receiver.init_app(app)

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
stream_comm_sim_rand: Optional[communication_decision.CommunicationJammerSimulation] = None
stream_comm_sim_opt: Optional[communication_decision.CommunicationJammerSimulation] = None
stream_radar_sim_rand: Optional[radar_decision.RadarJammerSimulation] = None
stream_radar_sim_opt: Optional[radar_decision.RadarJammerSimulation] = None
stream_lock = threading.RLock()
stream_t0_abs: Optional[float] = None
stream_last_t_rel: Optional[float] = None

finalizing = False
finalizing_lock = threading.Lock()

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
    return str(data.get("type") or data.get("Type") or "").strip()

def _extract_project_name(data: dict, default: str = "default") -> str:
    proj = data.get("projectname") or data.get("projectName")
    if not proj:
        scene = data.get("scene") or data.get("Scene") or {}
        proj = scene.get("projectname") or scene.get("projectName")
    if not proj:
        proj = default
    return str(proj).strip() or default

def _ensure_project_in_config(config: dict, project_name: str) -> None:
    if not project_name or project_name == "default":
        return
    scene = config.setdefault("scene", {})
    if not scene.get("projectname") and not scene.get("projectName"):
        scene["projectname"] = project_name
    if not config.get("projectname") and not config.get("projectName"):
        config["projectname"] = project_name

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
    with _flow_stage_lock:
        if project is None:
            _project_flow_stage.clear()
            _generate_done_global = False
            _pending_random_deployment = None
            _touch_system_activity()
            logger.info("已清空所有项目流程阶段与随机部署缓存")
            return
        if project in _project_flow_stage:
            _project_flow_stage.pop(project, None)
            _touch_system_activity()
            logger.info(f"已清空项目流程阶段: {project}")

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
    global stream_active, stream_project_name
    global stream_comm_sim_rand, stream_comm_sim_opt
    global stream_radar_sim_rand, stream_radar_sim_opt
    global stream_t0_abs, stream_last_t_rel, stream_frame_count
    with stream_lock:
        logger.debug("重置流式状态")
        stream_active = False
        stream_project_name = None
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
        global stream_active, stream_project_name
        global stream_comm_sim_rand, stream_comm_sim_opt
        global stream_radar_sim_rand, stream_radar_sim_opt
        global stream_last_t_rel

        logger.info("========== 开始最终化数据流 ==========")
        proj = None
        with stream_lock:
            if not stream_active:
                logger.warning("流式状态未激活，跳过最终化")
                return
            comm_rand = stream_comm_sim_rand
            comm_opt = stream_comm_sim_opt
            radar_rand = stream_radar_sim_rand
            radar_opt = stream_radar_sim_opt
            proj = stream_project_name
            stream_active = False
            stream_project_name = None
            stream_comm_sim_rand = None
            stream_comm_sim_opt = None
            stream_radar_sim_rand = None
            stream_radar_sim_opt = None
            last_t_rel = stream_last_t_rel
            stream_last_t_rel = None
        _cancel_idle_timer()

        if None in (comm_rand, comm_opt, radar_rand, radar_opt):
            logger.warning("部分流式模拟器未初始化，无法最终化")
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

        with concurrent.futures.ThreadPoolExecutor(max_workers=4) as executor:
            future_comm_rand = executor.submit(get_metrics, comm_rand, "通信随机")
            future_comm_opt = executor.submit(get_metrics, comm_opt, "通信优化")
            future_radar_rand = executor.submit(get_metrics, radar_rand, "雷达随机")
            future_radar_opt = executor.submit(get_metrics, radar_opt, "雷达优化")

            comm_rand_metrics = future_comm_rand.result(timeout=30)
            comm_opt_metrics = future_comm_opt.result(timeout=30)
            radar_rand_metrics = future_radar_rand.result(timeout=30)
            radar_opt_metrics = future_radar_opt.result(timeout=30)

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

        result = {
            "Status": True,
            "Message": "应对决策处理完成",
            "Timestamp": time.strftime('%Y-%m-%d %H:%M:%S'),
            "ProjectName": proj or "default",
            "Type": "fusion_result",
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
    project_name = _extract_project_name(config, default="")
    if not project_name:
        project_name = "default"
    _ensure_project_in_config(config, project_name)
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
        "干扰机位置已保存至 %s（随机→original*，优化→optimized*；通信 %d/%d，雷达 %d/%d）",
        project_dir,
        len(comm_original_show),
        len(comm_optimized_show),
        len(radar_original_show),
        len(radar_optimized_show),
    )

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
    scene_raw = data.get("Scene") or data.get("scene") or {}
    range_info = scene_raw.get("range") or scene_raw.get("Range") or {}
    project_name = (
        scene_raw.get("projectName")
        or scene_raw.get("projectname")
        or _extract_project_name(data, default="")
    )
    if not project_name:
        project_name = "default"

    center_lon = float(range_info.get("longitude", 0))
    center_lat = float(range_info.get("latitude", 0))
    center_alt = float(range_info.get("altitude", 0))
    radius_m = float(range_info.get("radius", 3000))
    range_type = range_info.get("rangeType") or range_info.get("rangetype") or "circle"

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
        "scene": {
            "latitude": str(range_info.get("latitude", center_lat)),
            "longitude": str(range_info.get("longitude", center_lon)),
            "altitude": str(range_info.get("altitude", center_alt)),
            "radius": radius_m,
            "projectname": project_name,
        },
        "guardPoints": data.get("guardPoints") or data.get("GuardPoints") or [],
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

    return {
        "Status": True,
        "Message": "jammerStart 部署完成，可接收融合数据",
        "Timestamp": time.strftime('%Y-%m-%d %H:%M:%S'),
        "ProjectName": project_name,
        "Type": "jammerStart_return",
        "RadarJammerPositions": radar_optimized,
        "CommunicationJammerPositions": comm_optimized,
        "RadarPerformanceMetrics": {},
        "CommunicationPerformanceMetrics": {},
    }

# ---------- 随机部署 ----------
def run_random_deployment(config: dict) -> dict:
    logger.info("执行随机部署 (generate)")
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
        "ProjectName": config.get("scene", {}).get("projectname", ""),
        "Type": "deployment_random_return"
    }

# ---------- 优化部署 ----------
def run_optimization_deployment(config: dict) -> dict:
    logger.info("执行优化部署 (reGenerate)")

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

    project_name = _extract_project_name(config, default="")
    _ensure_project_in_config(config, project_name)
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
        "ProjectName": config.get("scene", {}).get("projectname", ""),
        "Type": "deployment_return"
    }

# ---------- MQTT 消息处理 ----------
@mqtt_receiver.on_connect()
def on_connect(client, userdata, flags, rc):
    global _mqtt_connected
    if rc == 0:
        with _mqtt_lock:
            if not _mqtt_connected:
                logger.info("MQTT 连接成功")
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
    try:
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
                    item.get("sensor_type"),
                    len(message.payload)
                )
                _process_single_message(message.topic, item)
        else:
            logger.info(
                "MQTT消息摘要: topic=%s type=%s project=%s sensor_type=%s payload_size=%s",
                message.topic,
                _get_message_type(raw_data),
                _extract_project_name(raw_data, default=""),
                raw_data.get("sensor_type"),
                len(message.payload)
            )
            _process_single_message(message.topic, raw_data)
    except Exception as e:
        logger.error(f"消息处理出错: {e}", exc_info=True)

def _process_single_message(topic, data):
    global stream_active, stream_project_name
    global stream_comm_sim_rand, stream_comm_sim_opt
    global stream_radar_sim_rand, stream_radar_sim_opt
    global stream_t0_abs, stream_last_t_rel, stream_frame_count
    global finalizing

    # 部署消息处理
    msg_type = _get_message_type(data)
    topic_is_vi_decision = topic == "vi_decision" or topic.startswith("vi_decision/")
    topic_is_pg_finish = topic == "pg_data_processor_finish" or topic.startswith("pg_data_processor_finish/")

    if topic_is_vi_decision:
        if msg_type == "generate":
            result = run_random_deployment(data)
            send_to_target(result)
            _on_generate_completed()
            return
        elif msg_type == "reGenerate":
            proj = _extract_project_name(data)
            result = run_optimization_deployment(data)
            send_to_target(result)
            _on_regenerate_completed(proj)
            return
        elif msg_type == "jammerStart":
            result = run_jammer_start_deployment(data)
            send_to_target(result)
            _on_regenerate_completed(result.get("ProjectName") or _extract_project_name(data))
            return

    # 手动调试触发最终化
    if msg_type == "debug_finalize":
        logger.info("收到调试指令，手动触发最终化")
        _finalize_stream(idle_reason="debug_finalize")
        return

    # 融合数据流处理
    is_simdata = False
    if topic_is_pg_finish or topic_is_vi_decision:
        if msg_type == "simData" and data.get("sensor_type") == "AA00":
            is_simdata = True

    if is_simdata:
        with finalizing_lock:
            if finalizing:
                logger.debug("正在最终化，忽略新到达的 simData 帧")
                return

        proj = _extract_project_name(data)
        if not _can_start_decision(proj):
            with _flow_stage_lock:
                stage = _project_flow_stage.get(proj, {})
                logger.warning(
                    "融合数据到达但部署阶段未完成，忽略。project=%s，"
                    "generate_done=%s，regenerate_done=%s（需先完成 generate+reGenerate 或 jammerStart）",
                    proj,
                    stage.get("generate_done", False),
                    stage.get("regenerate_done", False),
                )
            return
        logger.debug(f"处理 simData，项目: {proj}")

        t_abs = communication_decision.parse_timestamp_to_seconds(data.get('time', ''))
        if t_abs == 0.0:
            t_abs = time.time()
            logger.warning(f"时间解析失败，使用当前时间: {t_abs}，原始时间串: {data.get('time')}")

        with stream_lock:
            if not stream_active or stream_project_name != proj:
                logger.info(f"开始新数据流，项目: {proj} (原项目: {stream_project_name})")
                _reset_stream_state(reason="stream_switch")
                stream_active = True
                stream_project_name = proj
                stream_t0_abs = t_abs

                # 修改：所有步数据回调统一发送到 vi_decision_res
                cb_comm_rand = lambda data: send_to_target(data, "vi_decision_res")
                cb_comm_opt = lambda data: send_to_target(data, "vi_decision_res")
                cb_radar_rand = lambda data: send_to_target(data, "vi_decision_res")
                cb_radar_opt = lambda data: send_to_target(data, "vi_decision_res")

                try:
                    stream_comm_sim_rand = communication_decision.CommunicationJammerSimulation.create_stream_simulator(
                        project_name=proj, deployment='random', silent=False, mqtt_callback=cb_comm_rand
                    )
                    stream_comm_sim_opt = communication_decision.CommunicationJammerSimulation.create_stream_simulator(
                        project_name=proj, deployment='optimized', silent=False, mqtt_callback=cb_comm_opt
                    )
                    stream_radar_sim_rand = radar_decision.RadarJammerSimulation.create_stream_simulator(
                        project_name=proj, deployment='random', silent=False, mqtt_callback=cb_radar_rand
                    )
                    stream_radar_sim_opt = radar_decision.RadarJammerSimulation.create_stream_simulator(
                        project_name=proj, deployment='optimized', silent=False, mqtt_callback=cb_radar_opt
                    )
                    logger.info("流式模拟器（随机+优化）创建成功，所有步数据将发往 vi_decision_res")
                    _set_system_status("running", proj, None, reason="first_simdata_started")
                except Exception as e:
                    logger.error(f"创建流式模拟器失败: {e}", exc_info=True)
                    _set_system_status("error", proj, str(e), reason="simulator_init_failed")
                    _reset_stream_state(reason="error_cleanup", update_status=False)
                    return

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

    if msg_type == "simStop":
        proj = _extract_project_name(data, default="")
        with stream_lock:
            if not stream_active:
                logger.debug("收到 simStop 但流已非活跃，忽略")
                return
        with finalizing_lock:
            if finalizing:
                logger.debug("收到 simStop 但已有最终化进程，忽略")
                return
        logger.info("收到 simStop，结束数据流")
        _finalize_stream(idle_reason="normal_finished")
        if proj:
            _clear_stage(proj)
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
    logger.info(f"启动部署决策服务（统一主题 vi_decision_res），监听地址: {host}:{port}")
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