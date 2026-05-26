#!/usr/bin/env python3
"""
决策服务自检脚本（终端运行）

检查项：
  1. GET /api/health — HTTP 200 + 业务 ok/unhealthy
  2. GET /api/status — 任务状态 idle/running/error
  3. 可选 POST /api/reset
  4. 可选 --mqtt-flow — MQTT 全流程：generate → reGenerate → simData → running → simStop → idle

用法：
  python check_service.py
  python check_service.py --reset
  python check_service.py --mqtt-flow
  python check_service.py --mqtt-flow --frames 1
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Dict, Optional, Tuple


_WORKSPACE = Path(__file__).resolve().parent


def _load_dotenv_vars() -> Dict[str, str]:
    out: Dict[str, str] = {}
    env_path = _WORKSPACE / ".env"
    if not env_path.is_file():
        return out
    for line in env_path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        out[key.strip()] = value.strip().strip('"').strip("'")
    return out


def _load_port_from_dotenv() -> Optional[int]:
    raw = _load_dotenv_vars().get("HOST_PORT")
    if raw is None:
        return None
    try:
        return int(raw)
    except ValueError:
        return None


def _resolve_mqtt_broker(broker: str) -> str:
    """脚本在宿主机运行时，把 Docker 专用主机名映射到本机。"""
    if broker in ("host.docker.internal", "gateway.docker.internal"):
        return "127.0.0.1"
    return broker


def http_request(
    method: str,
    url: str,
    timeout: float = 8.0,
    data: Optional[bytes] = None,
) -> Tuple[int, Dict[str, Any], Optional[str]]:
    """返回 (status_code, json_body_or_wrap, error_message)。"""
    req = urllib.request.Request(url, method=method.upper(), data=data)
    if data is not None:
        req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8", errors="replace")
            code = resp.status
    except urllib.error.HTTPError as e:
        raw = e.read().decode("utf-8", errors="replace")
        code = e.code
    except urllib.error.URLError as e:
        return 0, {}, str(e.reason if hasattr(e, "reason") else e)
    except Exception as e:
        return 0, {}, str(e)

    try:
        body = json.loads(raw) if raw else {}
    except json.JSONDecodeError:
        body = {"_raw": raw}
    return code, body, None


def _print_section(title: str) -> None:
    print()
    print("=" * 60)
    print(title)
    print("=" * 60)


def _result(label: str, ok: bool, detail: str = "") -> bool:
    mark = "PASS" if ok else "FAIL"
    line = f"  [{mark}] {label}"
    if detail:
        line += f" — {detail}"
    print(line)
    return ok


def _warn(label: str, detail: str = "") -> None:
    line = f"  [WARN] {label}"
    if detail:
        line += f" — {detail}"
    print(line)


def check_health(base_url: str) -> Tuple[bool, Dict[str, Any]]:
    url = f"{base_url}/api/health"
    print(f"  请求: GET {url}")
    code, body, err = http_request("GET", url)
    if err:
        _result("连接 /api/health", False, err)
        return False, {}

    print(f"  HTTP 状态码: {code}")
    print(f"  响应 JSON:\n{json.dumps(body, ensure_ascii=False, indent=2)}")

    ok_http_200 = _result(
        "HTTP 始终 200（不再因 unhealthy 返回 503）",
        code == 200,
        f"实际 code={code}",
    )

    status = body.get("status")
    details = body.get("details") or {}
    mqtt_ok = details.get("mqtt_connected")
    sys_st = details.get("system_status")

    if status == "ok":
        _result("业务健康 status=ok", True, f"mqtt_connected={mqtt_ok}, system_status={sys_st}")
    elif status == "unhealthy":
        _warn(
            "业务不健康 status=unhealthy（HTTP 仍应为 200）",
            body.get("message", ""),
        )
        _result("业务健康 status=ok", False, "当前为 unhealthy，请检查 MQTT 或系统 error")
    else:
        _result("响应含 status 字段", False, f"未知 status={status!r}")

    return ok_http_200, body


def check_status(base_url: str) -> Tuple[bool, Dict[str, Any]]:
    url = f"{base_url}/api/status"
    print(f"  请求: GET {url}")
    code, body, err = http_request("GET", url)
    if err:
        _result("连接 /api/status", False, err)
        return False, {}

    print(f"  HTTP 状态码: {code}")
    print(f"  响应 JSON:\n{json.dumps(body, ensure_ascii=False, indent=2)}")

    ok = _result("HTTP 200", code == 200, f"实际 code={code}")

    biz = body.get("status")
    valid_biz = biz in ("idle", "running", "error")
    _result(
        "任务状态字段合法 (idle/running/error)",
        valid_biz,
        f"当前 status={biz!r}, reason={body.get('state_reason')!r}",
    )

    if biz == "running":
        print(f"  >> 任务运行中: project={body.get('project')!r}, frame_count={body.get('frame_count')}")
    elif biz == "error":
        print(f"  >> 任务异常: error={body.get('error')!r}")
    else:
        print(
            f"  >> 任务空闲: project={body.get('project')!r}, "
            f"frame_count={body.get('frame_count')}, reason={body.get('state_reason')!r}"
        )

    flow = body.get("flow_stage")
    if flow:
        print(f"  >> 流程阶段: {json.dumps(flow, ensure_ascii=False)}")

    return ok and valid_biz, body


def check_reset(base_url: str) -> bool:
    url = f"{base_url}/api/reset"
    print(f"  请求: POST {url}")
    code, body, err = http_request("POST", url, data=b"{}")
    if err:
        return _result("连接 /api/reset", False, err)

    print(f"  HTTP 状态码: {code}")
    print(f"  响应 JSON:\n{json.dumps(body, ensure_ascii=False, indent=2)}")

    ok = _result("重置接口 HTTP 200", code == 200, f"实际 code={code}")
    if code == 200:
        _result("重置成功 success=true", bool(body.get("success")), body.get("message", ""))
    return ok


def check_mqtt_flow(
    base_url: str,
    broker: str,
    port: int,
    username: str,
    password: str,
    frames: int = 1,
    deploy_timeout: float = 60.0,
    running_timeout: float = 30.0,
    idle_timeout: float = 60.0,
) -> bool:
    """MQTT 业务流：generate → reGenerate → simData → status=running → simStop → status=idle。"""
    try:
        from test_mqtt_flow import MqttPublisher, load_json, wait_for_status
    except ImportError as e:
        _result("导入 test_mqtt_flow / paho-mqtt", False, str(e))
        return False

    generate_file = _WORKSPACE / "01_干扰机部署_v1.json"
    regenerate_file = _WORKSPACE / "干扰机位置优化_v2.json"
    simdata_file = _WORKSPACE / "ronghe.json"
    for p in (generate_file, regenerate_file, simdata_file):
        if not p.is_file():
            return _result(f"测试数据文件存在: {p.name}", False, "文件缺失")

    generate_payload = load_json(generate_file)
    regenerate_payload = load_json(regenerate_file)
    sim_frames = load_json(simdata_file)
    if not isinstance(sim_frames, list) or not sim_frames:
        return _result("ronghe.json 为非空数组", False)

    project = (
        generate_payload.get("scene", {}).get("projectname")
        or regenerate_payload.get("scene", {}).get("projectname")
    )
    if not project:
        return _result("解析项目名称", False)
    print(f"  测试项目: {project}")
    print(f"  MQTT Broker: {broker}:{port}")

    generate_payload["type"] = "generate"
    generate_payload.setdefault("scene", {})["projectname"] = project
    regenerate_payload["type"] = "reGenerate"
    regenerate_payload.setdefault("scene", {})["projectname"] = project

    selected_frames = []
    for frame in sim_frames[: max(1, frames)]:
        copied = dict(frame)
        copied["type"] = "simData"
        copied["projectname"] = project
        copied["sensor_type"] = "AA00"
        selected_frames.append(copied)

    sim_stop_payload = {"type": "simStop", "projectname": project}
    topic_deploy = "vi_decision"
    topic_sim = "pg_data_processor_finish"
    response_topic = "vi_decision_res"

    all_ok = True
    publisher = MqttPublisher(broker, port, username, password, response_topic)
    try:
        try:
            publisher.connect()
            all_ok = _result("MQTT 连接 Broker", True) and all_ok
        except Exception as e:
            _result("MQTT 连接 Broker", False, str(e))
            return False

        try:
            publisher.publish_json(topic_deploy, generate_payload)
            publisher.wait_for_message({"deployment_random_return"}, deploy_timeout)
            all_ok = _result("generate → deployment_random_return", True) and all_ok
        except Exception as e:
            all_ok = _result("generate → deployment_random_return", False, str(e)) and False

        try:
            publisher.publish_json(topic_deploy, regenerate_payload)
            publisher.wait_for_message({"deployment_return"}, deploy_timeout)
            all_ok = _result("reGenerate → deployment_return", True) and all_ok
        except Exception as e:
            all_ok = _result("reGenerate → deployment_return", False, str(e)) and False

        try:
            import time

            for index, frame in enumerate(selected_frames, start=1):
                publisher.publish_json(topic_sim, frame)
                print(f"  已发送 simData 帧 {index}/{len(selected_frames)}")
                if index == 1:
                    running_status = wait_for_status(
                        base_url, "running", running_timeout, project
                    )
                    fc = running_status.get("frame_count", 0)
                    all_ok = _result(
                        "首帧 simData 后 status=running",
                        True,
                        f"project={project!r}, frame_count={fc}",
                    ) and all_ok
                elif index < len(selected_frames):
                    time.sleep(1.0)
        except Exception as e:
            all_ok = _result("simData → status=running", False, str(e)) and False

        try:
            publisher.publish_json(topic_sim, sim_stop_payload)
            try:
                publisher.wait_for_message(
                    {"fusion_result", "decision_result"}, idle_timeout
                )
                _result("simStop → 收到 fusion/decision 结果", True)
            except Exception as e:
                # 最终化较慢时可能先 idle 后 MQTT 回包，不单独判失败
                _warn("simStop MQTT 回包等待", str(e))

            # simStop 完成后 project 会清空，只校验 status=idle，不要求 project 仍匹配
            idle_status = wait_for_status(base_url, "idle", idle_timeout)
            reason = idle_status.get("state_reason")
            all_ok = _result(
                "simStop 后 status=idle",
                idle_status.get("status") == "idle",
                f"state_reason={reason!r}, frame_count={idle_status.get('frame_count')}",
            ) and all_ok
        except Exception as e:
            all_ok = _result("simStop → status=idle", False, str(e)) and False

    finally:
        publisher.close()

    return all_ok


def main() -> int:
    if sys.platform == "win32":
        try:
            sys.stdout.reconfigure(encoding="utf-8")
            sys.stderr.reconfigure(encoding="utf-8")
        except Exception:
            pass

    default_port = _load_port_from_dotenv() or 17686
    parser = argparse.ArgumentParser(description="决策服务 HTTP 自检")
    parser.add_argument(
        "--url",
        default=os.getenv("CHECK_BASE_URL", f"http://127.0.0.1:{default_port}"),
        help=f"服务根地址（默认 http://127.0.0.1:{default_port}）",
    )
    parser.add_argument(
        "--reset",
        action="store_true",
        help="额外调用 POST /api/reset（会终止当前任务并置为 idle）",
    )
    parser.add_argument(
        "--require-healthy",
        action="store_true",
        help="要求 /api/health 的 body.status 必须为 ok，否则整体 FAIL",
    )
    parser.add_argument(
        "--mqtt-flow",
        action="store_true",
        help="额外跑 MQTT 全流程（需本机 MQTT 已启动，且测试 JSON 文件存在）",
    )
    parser.add_argument(
        "--mqtt-broker",
        default=None,
        help="MQTT 地址（默认读 .env 的 SOURCE_BROKER，宿主机脚本会将 host.docker.internal 转为 127.0.0.1）",
    )
    parser.add_argument(
        "--mqtt-port",
        type=int,
        default=None,
        help="MQTT 端口（默认 .env SOURCE_PORT 或 1883）",
    )
    parser.add_argument(
        "--frames",
        type=int,
        default=1,
        help="MQTT 流测试发送的 simData 帧数（默认 1，加快自检）",
    )
    args = parser.parse_args()
    base = args.url.rstrip("/")
    dotenv = _load_dotenv_vars()
    mqtt_broker = _resolve_mqtt_broker(
        args.mqtt_broker or dotenv.get("SOURCE_BROKER", "127.0.0.1")
    )
    try:
        mqtt_port = args.mqtt_port or int(dotenv.get("SOURCE_PORT", "1883"))
    except ValueError:
        mqtt_port = 1883
    mqtt_user = dotenv.get("SOURCE_USERNAME", "1")
    mqtt_pass = dotenv.get("SOURCE_PASSWORD", "1")

    print("决策服务自检")
    print(f"目标: {base}")

    all_ok = True

    _print_section("1. 健康检查 GET /api/health")
    health_ok, health_body = check_health(base)
    all_ok = all_ok and health_ok

    if args.require_healthy and health_body.get("status") != "ok":
        all_ok = False
        _result("require-healthy: body.status 必须为 ok", False)

    _print_section("2. 任务状态 GET /api/status")
    status_ok, _ = check_status(base)
    all_ok = all_ok and status_ok

    if args.reset:
        _print_section("3. 重置任务 POST /api/reset")
        reset_ok = check_reset(base)
        all_ok = all_ok and reset_ok

        _print_section("4. 重置后再次查询状态")
        print("  （等待 0.5s）")
        import time

        time.sleep(0.5)
        _, status_after = check_status(base)
        if status_after.get("status") != "idle":
            all_ok = False
            _result("重置后 status 应为 idle", False, f"实际={status_after.get('status')!r}")
        else:
            _result("重置后 status 应为 idle", True)

    if args.mqtt_flow:
        _print_section("5. MQTT 业务流 generate → reGenerate → simData → simStop")
        print("  前置条件: 本机 MQTT 已运行；Docker 服务已启动；测试 JSON 在项目目录")
        mqtt_ok = check_mqtt_flow(
            base,
            mqtt_broker,
            mqtt_port,
            mqtt_user,
            mqtt_pass,
            frames=max(1, args.frames),
        )
        all_ok = all_ok and mqtt_ok

        _print_section("6. MQTT 流结束后 HTTP 状态")
        _, final_status = check_status(base)

    _print_section("汇总")
    if all_ok:
        print("  全部检查通过。")
        if health_body.get("status") == "unhealthy":
            print("  提示: HTTP 策略正常，但 MQTT/系统尚未健康，请检查 .env 中 SOURCE_BROKER 等配置。")
    else:
        print("  存在失败项，请根据上方 [FAIL] 排查。")

    return 0 if all_ok else 1


if __name__ == "__main__":
    sys.exit(main())
