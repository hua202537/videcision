import argparse
import json
import sys
import time
import urllib.error
import urllib.request
from collections import Counter
from pathlib import Path

import paho.mqtt.client as mqtt


def load_json(path: Path):
    with path.open("r", encoding="utf-8") as f:
        return json.load(f)


def http_get_json(url: str):
    with urllib.request.urlopen(url, timeout=8) as resp:
        return json.loads(resp.read().decode("utf-8"))


class MqttPublisher:
    def __init__(self, broker: str, port: int, username: str, password: str, response_topic: str):
        self.broker = broker
        self.port = port
        self.response_topic = response_topic
        self.connected = False
        self.received_messages = []
        self.message_counts = Counter()
        self.client = mqtt.Client()
        if username or password:
            self.client.username_pw_set(username, password)
        self.client.on_connect = self._on_connect
        self.client.on_disconnect = self._on_disconnect
        self.client.on_message = self._on_message

    def _on_connect(self, client, userdata, flags, rc):
        self.connected = rc == 0
        if rc == 0:
            client.subscribe(self.response_topic, qos=1)
            print(f"[MQTT] connected to {self.broker}:{self.port}")
        else:
            print(f"[MQTT] connect failed, rc={rc}")

    def _on_disconnect(self, client, userdata, rc):
        self.connected = False
        print("[MQTT] disconnected")

    def _on_message(self, client, userdata, message):
        try:
            payload = json.loads(message.payload.decode("utf-8"))
        except Exception:
            return
        msg_type = payload.get("Type") or payload.get("type") or "unknown"
        self.received_messages.append(payload)
        self.message_counts[msg_type] += 1
        if msg_type in {"deployment_random_return", "deployment_return", "fusion_result", "decision_result"}:
            print(f"[MQTT] received {msg_type} <- {message.topic}")

    def connect(self):
        self.client.connect(self.broker, self.port, keepalive=60)
        self.client.loop_start()
        deadline = time.time() + 5
        while not self.connected and time.time() < deadline:
            time.sleep(0.1)
        if not self.connected:
            raise RuntimeError("MQTT connect timeout")

    def publish_json(self, topic: str, payload: dict):
        body = json.dumps(payload, ensure_ascii=False)
        info = self.client.publish(topic, body, qos=1)
        info.wait_for_publish()
        if info.rc != mqtt.MQTT_ERR_SUCCESS:
            raise RuntimeError(f"publish failed, rc={info.rc}, topic={topic}")
        print(f"[MQTT] published {payload.get('type')} -> {topic}")

    def wait_for_message(self, expected_values, timeout: float):
        deadline = time.time() + timeout
        while time.time() < deadline:
            for index, payload in enumerate(self.received_messages):
                msg_type = payload.get("Type") or payload.get("type")
                if msg_type in expected_values:
                    return self.received_messages.pop(index)
            time.sleep(0.2)
        raise RuntimeError(f"timeout waiting for MQTT response: {expected_values}")

    def close(self):
        self.client.loop_stop()
        self.client.disconnect()

    def summarize_messages(self, label: str, reset: bool = False):
        if not self.message_counts:
            print(f"[MQTT] {label}: no response messages received")
        else:
            summary = ", ".join(f"{key}={value}" for key, value in sorted(self.message_counts.items()))
            print(f"[MQTT] {label}: {summary}")
        if reset:
            self.message_counts.clear()


def print_status(base_url: str, label: str):
    try:
        status = http_get_json(f"{base_url}/api/status")
        print(f"[HTTP] {label}: {json.dumps(status, ensure_ascii=False)}")
    except urllib.error.URLError as exc:
        print(f"[HTTP] {label}: request failed: {exc}")


def wait_for_status(base_url: str, expected_status: str, timeout: float, project: str = ""):
    deadline = time.time() + timeout
    last_status = None
    while time.time() < deadline:
        last_status = http_get_json(f"{base_url}/api/status")
        matches_status = last_status.get("status") == expected_status
        matches_project = True if not project else last_status.get("project") == project
        if matches_status and matches_project:
            return last_status
        time.sleep(0.5)
    raise RuntimeError(
        f"timeout waiting for status={expected_status}, project={project or '<any>'}, last={last_status}"
    )


def main():
    parser = argparse.ArgumentParser(description="MQTT flow test: generate -> reGenerate -> simData -> simStop")
    parser.add_argument("--broker", default="127.0.0.1", help="MQTT broker address")
    parser.add_argument("--port", type=int, default=1883, help="MQTT broker port")
    parser.add_argument("--username", default="1", help="MQTT username")
    parser.add_argument("--password", default="1", help="MQTT password")
    parser.add_argument("--topic-deploy", default="vi_decision", help="topic for generate/reGenerate")
    parser.add_argument("--topic-sim", default="pg_data_processor_finish", help="topic for simData/simStop")
    parser.add_argument("--base-url", default="http://127.0.0.1:17686", help="HTTP base url for status checks")
    parser.add_argument("--response-topic", default="vi_decision_res", help="MQTT topic for deployment/final responses")
    parser.add_argument("--generate-file", default="01_干扰机部署_v1.json", help="generate payload file")
    parser.add_argument("--regenerate-file", default="干扰机位置优化_v2.json", help="reGenerate payload file")
    parser.add_argument("--simdata-file", default="ronghe.json", help="simData array file")
    parser.add_argument("--frames", type=int, default=3, help="number of simData frames to send")
    parser.add_argument("--interval", type=float, default=1.0, help="seconds between simData frames")
    parser.add_argument("--project", default="", help="override project name for all payloads")
    parser.add_argument("--skip-stop", action="store_true", help="do not send simStop")
    parser.add_argument("--deploy-timeout", type=float, default=30.0, help="seconds to wait for deployment response")
    parser.add_argument("--running-timeout", type=float, default=20.0, help="seconds to wait for running status")
    parser.add_argument("--idle-timeout", type=float, default=20.0, help="seconds to wait for idle status")
    args = parser.parse_args()

    workspace = Path(__file__).resolve().parent
    generate_payload = load_json(workspace / args.generate_file)
    regenerate_payload = load_json(workspace / args.regenerate_file)
    sim_frames = load_json(workspace / args.simdata_file)

    if not isinstance(sim_frames, list) or not sim_frames:
        raise RuntimeError("simData file must contain a non-empty list")

    project = args.project or generate_payload.get("scene", {}).get("projectname") or regenerate_payload.get("scene", {}).get("projectname")
    if not project:
        raise RuntimeError("unable to determine project name")

    generate_payload["type"] = "generate"
    generate_payload.setdefault("scene", {})
    generate_payload["scene"]["projectname"] = project

    regenerate_payload["type"] = "reGenerate"
    regenerate_payload.setdefault("scene", {})
    regenerate_payload["scene"]["projectname"] = project

    selected_frames = []
    for frame in sim_frames[: max(1, args.frames)]:
        copied = dict(frame)
        copied["type"] = "simData"
        copied["projectname"] = project
        copied["sensor_type"] = "AA00"
        selected_frames.append(copied)

    sim_stop_payload = {
        "type": "simStop",
        "projectname": project,
    }

    print(f"[INFO] project={project}")
    print_status(args.base_url, "before")

    publisher = MqttPublisher(args.broker, args.port, args.username, args.password, args.response_topic)
    try:
        publisher.connect()

        publisher.publish_json(args.topic_deploy, generate_payload)
        generate_resp = publisher.wait_for_message({"deployment_random_return"}, args.deploy_timeout)
        print(f"[CHECK] generate response received: {generate_resp.get('Message')}")
        publisher.summarize_messages("summary after generate", reset=True)
        print_status(args.base_url, "after generate")

        publisher.publish_json(args.topic_deploy, regenerate_payload)
        regenerate_resp = publisher.wait_for_message({"deployment_return"}, args.deploy_timeout)
        print(f"[CHECK] reGenerate response received: {regenerate_resp.get('Message')}")
        publisher.summarize_messages("summary after reGenerate", reset=True)
        print_status(args.base_url, "after reGenerate")

        for index, frame in enumerate(selected_frames, start=1):
            publisher.publish_json(args.topic_sim, frame)
            print(f"[INFO] sent simData frame {index}/{len(selected_frames)}")
            if index == 1:
                running_status = wait_for_status(args.base_url, "running", args.running_timeout, project)
                print(f"[HTTP] running reached: {json.dumps(running_status, ensure_ascii=False)}")
                if len(selected_frames) > 1:
                    time.sleep(args.interval)
            else:
                time.sleep(args.interval)
                print_status(args.base_url, f"after simData #{index}")
            publisher.summarize_messages(f"summary after simData #{index}", reset=True)

        if not args.skip_stop:
            publisher.publish_json(args.topic_sim, sim_stop_payload)
            publisher.wait_for_message({"fusion_result", "decision_result"}, args.idle_timeout)
            idle_status = wait_for_status(args.base_url, "idle", args.idle_timeout)
            print(f"[HTTP] after simStop: {json.dumps(idle_status, ensure_ascii=False)}")
            publisher.summarize_messages("summary after simStop", reset=True)

        print("[DONE] MQTT flow test finished")
    finally:
        publisher.close()

    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        print(f"[ERROR] {exc}")
        raise SystemExit(1)
