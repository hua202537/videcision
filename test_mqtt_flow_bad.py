import argparse
import json
import time
import urllib.error
import urllib.request
from pathlib import Path

import paho.mqtt.client as mqtt


def load_json(path: Path):
    with path.open("r", encoding="utf-8") as f:
        return json.load(f)


def http_get_json(url: str):
    with urllib.request.urlopen(url, timeout=8) as resp:
        return json.loads(resp.read().decode("utf-8"))


class MqttPublisher:
    def __init__(self, broker: str, port: int, username: str, password: str):
        self.broker = broker
        self.port = port
        self.connected = False
        self.client = mqtt.Client()
        if username or password:
            self.client.username_pw_set(username, password)
        self.client.on_connect = self._on_connect
        self.client.on_disconnect = self._on_disconnect

    def _on_connect(self, client, userdata, flags, rc):
        self.connected = rc == 0
        if rc == 0:
            print(f"[MQTT] connected to {self.broker}:{self.port}")
        else:
            print(f"[MQTT] connect failed, rc={rc}")

    def _on_disconnect(self, client, userdata, rc):
        self.connected = False
        print("[MQTT] disconnected")

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

    def close(self):
        self.client.loop_stop()
        self.client.disconnect()


def print_status(base_url: str, label: str):
    try:
        status = http_get_json(f"{base_url}/api/status")
        print(f"[HTTP] {label}: {json.dumps(status, ensure_ascii=False)}")
    except urllib.error.URLError as exc:
        print(f"[HTTP] {label}: request failed: {exc}")


def main():
    parser = argparse.ArgumentParser(description="MQTT bad-order test for gate validation")
    parser.add_argument("--broker", default="127.0.0.1", help="MQTT broker address")
    parser.add_argument("--port", type=int, default=1883, help="MQTT broker port")
    parser.add_argument("--username", default="1", help="MQTT username")
    parser.add_argument("--password", default="1", help="MQTT password")
    parser.add_argument("--topic-deploy", default="vi_decision", help="topic for generate/reGenerate")
    parser.add_argument("--topic-sim", default="pg_data_processor_finish", help="topic for simData")
    parser.add_argument("--base-url", default="http://127.0.0.1:17686", help="HTTP base url for status checks")
    parser.add_argument("--generate-file", default="01_干扰机部署_v1.json", help="generate payload file")
    parser.add_argument("--simdata-file", default="ronghe.json", help="simData array file")
    parser.add_argument(
        "--mode",
        choices=["simdata_only", "generate_then_simdata"],
        default="simdata_only",
        help="bad-order scenario to test",
    )
    parser.add_argument("--project", default="", help="override project name for all payloads")
    args = parser.parse_args()

    workspace = Path(__file__).resolve().parent
    generate_payload = load_json(workspace / args.generate_file)
    sim_frames = load_json(workspace / args.simdata_file)

    if not isinstance(sim_frames, list) or not sim_frames:
        raise RuntimeError("simData file must contain a non-empty list")

    project = args.project or generate_payload.get("scene", {}).get("projectname")
    if not project:
        raise RuntimeError("unable to determine project name")

    generate_payload["type"] = "generate"
    generate_payload.setdefault("scene", {})
    generate_payload["scene"]["projectname"] = project

    sim_frame = dict(sim_frames[0])
    sim_frame["type"] = "simData"
    sim_frame["projectname"] = project
    sim_frame["sensor_type"] = "AA00"

    print(f"[INFO] mode={args.mode}, project={project}")
    print("[EXPECT] Service should ignore simData and remain idle.")
    print_status(args.base_url, "before")

    publisher = MqttPublisher(args.broker, args.port, args.username, args.password)
    try:
        publisher.connect()

        if args.mode == "generate_then_simdata":
            publisher.publish_json(args.topic_deploy, generate_payload)
            time.sleep(1.0)
            print_status(args.base_url, "after generate")

        publisher.publish_json(args.topic_sim, sim_frame)
        time.sleep(1.0)
        print_status(args.base_url, "after simData")

        final_status = http_get_json(f"{args.base_url}/api/status")
        if final_status.get("status") != "idle":
            raise RuntimeError(
                f"bad-order validation failed: expected idle, got {final_status.get('status')}"
            )

        print("[PASS] Bad-order simData was blocked as expected.")
        print("[CHECK] Also inspect app logs for warning about incomplete deployment stage.")
    finally:
        publisher.close()

    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        print(f"[ERROR] {exc}")
        raise SystemExit(1)
