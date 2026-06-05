#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
模拟融合数据发送脚本（固定间隔版）
忽略原始时间戳，按照固定时间间隔（默认3秒）依次发送 ronghe.json 中的数据帧到 MQTT Broker。
"""

import json
import time
import argparse
import logging
from typing import List, Dict, Any

import paho.mqtt.client as mqtt

# ---------- 日志配置 ----------
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)


def load_fusion_frames(file_path: str) -> List[Dict[str, Any]]:
    """从 JSON 文件加载融合数据帧列表"""
    with open(file_path, 'r', encoding='utf-8') as f:
        data = json.load(f)
    if not isinstance(data, list):
        raise ValueError("JSON 文件内容应为数组格式")
    logger.info(f"成功加载 {len(data)} 条融合数据帧")
    return data


class MQTTSender:
    """MQTT 发送器"""

    def __init__(self, broker: str, port: int, username: str, password: str, topic: str):
        self.broker = broker
        self.port = port
        self.username = username
        self.password = password
        self.topic = topic
        self.client = mqtt.Client()
        self.client.username_pw_set(username, password)
        self.client.on_connect = self._on_connect
        self.client.on_disconnect = self._on_disconnect
        self.connected = False

    def _on_connect(self, client, userdata, flags, rc):
        if rc == 0:
            self.connected = True
            logger.info(f"已连接到 MQTT Broker {self.broker}:{self.port}")
        else:
            logger.error(f"连接失败，返回码: {rc}")

    def _on_disconnect(self, client, userdata, rc):
        self.connected = False
        logger.info("与 MQTT Broker 断开连接")

    def connect(self) -> bool:
        """连接到 Broker，返回是否成功"""
        try:
            self.client.connect(self.broker, self.port, keepalive=60)
            self.client.loop_start()
            timeout = 5
            start = time.time()
            while not self.connected and (time.time() - start) < timeout:
                time.sleep(0.1)
            if not self.connected:
                logger.error(f"连接超时 ({timeout}秒)")
                return False
            return True
        except Exception as e:
            logger.error(f"连接异常: {e}")
            return False

    def publish(self, payload: dict) -> bool:
        """发布一条消息"""
        try:
            json_str = json.dumps(payload, ensure_ascii=False)
            info = self.client.publish(self.topic, json_str, qos=1)
            info.wait_for_publish()
            if info.rc == mqtt.MQTT_ERR_SUCCESS:
                return True
            else:
                logger.error(f"发布失败，返回码: {info.rc}")
                return False
        except Exception as e:
            logger.error(f"发布异常: {e}")
            return False

    def disconnect(self):
        self.client.loop_stop()
        self.client.disconnect()


def main():
    parser = argparse.ArgumentParser(description="模拟融合数据发送（固定间隔版，默认3秒/条）")
    parser.add_argument("--file", "-f", default="ronghe.json", help="融合数据 JSON 文件路径")
    parser.add_argument("--broker", "-b", default="127.0.0.1", help="MQTT Broker 地址")
    parser.add_argument("--port", "-p", type=int, default=1883, help="MQTT Broker 端口")
    parser.add_argument("--username", "-u", default="1", help="MQTT 用户名")
    parser.add_argument("--password", "-P", default="1", help="MQTT 密码")
    parser.add_argument("--topic", "-t", default="pg_data_processor_finish", help="MQTT 发布主题")
    parser.add_argument("--interval", "-i", type=float, default=3.0,
                        help="固定发送间隔（秒），默认3.0")
    parser.add_argument("--speed", "-s", type=float, default=1.0,
                        help="发送速度倍率（实际间隔 = interval / speed），默认1.0")
    parser.add_argument("--dry-run", action="store_true", help="仅预览配置，不实际发送")
    args = parser.parse_args()

    # 计算实际发送间隔
    if args.speed <= 0:
        logger.error("速度倍率必须大于0")
        return 1
    actual_interval = args.interval / args.speed

    # 加载数据
    try:
        frames = load_fusion_frames(args.file)
    except Exception as e:
        logger.error(f"加载文件失败: {e}")
        return 1

    if not frames:
        logger.warning("文件中没有数据帧")
        return 0

    logger.info(f"总数据帧数: {len(frames)}")
    logger.info(f"固定发送间隔: {args.interval} 秒")
    if args.speed != 1.0:
        logger.info(f"速度倍率: {args.speed}x, 实际间隔: {actual_interval:.3f} 秒")
    total_time = (len(frames) - 1) * actual_interval
    logger.info(f"预计总耗时: {total_time:.2f} 秒")

    if args.dry_run:
        logger.info("Dry-run 模式，未实际发送消息")
        return 0

    # 初始化 MQTT 发送器
    sender = MQTTSender(args.broker, args.port, args.username, args.password, args.topic)
    if not sender.connect():
        logger.error("无法连接 MQTT Broker，退出")
        return 1

    logger.info(f"开始发送数据到主题 '{args.topic}'（固定间隔 {actual_interval:.3f} 秒）...")
    success_count = 0
    start_time = time.time()

    try:
        for i, frame in enumerate(frames):
            if i > 0:
                time.sleep(actual_interval)

            if sender.publish(frame):
                success_count += 1
                if (i + 1) % 10 == 0 or i == len(frames) - 1:
                    logger.info(f"已发送 {i+1}/{len(frames)} 条消息")
            else:
                logger.error(f"发送第 {i+1} 条消息失败")

        elapsed = time.time() - start_time
        logger.info(f"发送完成！成功: {success_count}/{len(frames)}，实际耗时: {elapsed:.2f} 秒")
    except KeyboardInterrupt:
        logger.info("收到中断信号，停止发送")
    finally:
        sender.disconnect()

    return 0 if success_count == len(frames) else 1


if __name__ == "__main__":
    exit(main())