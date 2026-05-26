"""
雷达干扰实时决策模拟程序 — 流式有状态版本（含每步数据保存及 MQTT 实时发送）
"""

import json
import os
import numpy as np
import math
import logging
from datetime import datetime, timedelta
from pyproj import CRS, Transformer
from typing import List, Dict, Any, Optional, Tuple

logger = logging.getLogger(__name__)

from radar_jamming_range_calculation import (
    RadarParams, JammerRadarParams,
    _calculate_jamming_success_rate_vectorized
)

def parse_freq_to_hz(freq_str: str) -> float:
    freq_str = freq_str.strip().upper()
    if freq_str.endswith('GHZ'): return float(freq_str[:-3]) * 1e9
    elif freq_str.endswith('MHZ'): return float(freq_str[:-3]) * 1e6
    elif freq_str.endswith('KHZ'): return float(freq_str[:-3]) * 1e3
    elif freq_str.endswith('HZ'): return float(freq_str[:-2])
    else: return float(freq_str)

def parse_bandwidth_to_hz(bw_str: str) -> float:
    return parse_freq_to_hz(bw_str)

def find_radar_distance_for_success_rate(jam_type: str, target_success_rate: float = 0.7):
    radar_params = RadarParams(power=200, gain=30, frequency=16, bandwidth=100,
                               loss=4, noise_figure=3, rcs=1, Rt=2000)
    jammer_params = JammerRadarParams(power=20, gain=7, bandwidth=100, loss=30,
                                      deception_gain=6, DRFM_quality=0.85)

    c = 3e8
    B_radar = radar_params.bandwidth * 1e6
    B_jammer = jammer_params.bandwidth * 1e6
    P_t = radar_params.power
    G_t = 10 ** (radar_params.gain / 10)
    sigma = radar_params.rcs
    Rt = radar_params.Rt
    L_r = 10 ** (radar_params.loss / 10)
    P_j = jammer_params.power
    G_j = 10 ** (jammer_params.gain / 10)
    L_j = 10 ** (jammer_params.loss / 10)
    deception_gain = 10 ** (jammer_params.deception_gain / 10)
    quality = jammer_params.DRFM_quality

    low, high = 0.1, 200.0
    for _ in range(50):
        mid = (low + high) / 2
        R_m = mid * 1000
        if jam_type == 'suppressive':
            numerator = P_j * G_j * 4 * np.pi * Rt ** 4 * B_radar
            denominator = P_t * G_t * sigma * R_m ** 2 * B_jammer * L_j / L_r
            JSR_linear = numerator / denominator
            JSR_db = 10 * np.log10(JSR_linear)
            success = _calculate_jamming_success_rate_vectorized(np.array([JSR_db]), 'noise')[0]
        else:
            numerator = P_j * G_j * 4 * np.pi * Rt ** 4 * deception_gain
            denominator = P_t * G_t * sigma * R_m ** 2 * L_j / L_r
            JSR_linear = numerator / denominator
            JSR_db = 10 * np.log10(JSR_linear)
            range_factor = np.exp(-R_m / (50 * 1000))
            effective_JSR_db = JSR_db + 10 * np.log10(quality * range_factor)
            success = _calculate_jamming_success_rate_vectorized(np.array([effective_JSR_db]), 'deception')[0]
        if success > target_success_rate: low = mid
        else: high = mid
    return low * 1000

def parse_timestamp_to_seconds(timestamp_str):
    if len(timestamp_str) < 14: return 0.0
    hh = int(timestamp_str[8:10])
    mm = int(timestamp_str[10:12])
    ss = int(timestamp_str[12:14])
    ms = int(timestamp_str[14:]) if len(timestamp_str) > 14 else 0
    return hh * 3600 + mm * 60 + ss + ms / 1000.0

def parse_timestamp_to_datetime(timestamp_str):
    """解析 YYYYMMDDHHMMSSmmm 为 datetime（带毫秒）"""
    if len(timestamp_str) < 14:
        return None
    try:
        base = datetime.strptime(timestamp_str[:14], '%Y%m%d%H%M%S')
        ms_str = timestamp_str[14:17] if len(timestamp_str) >= 17 else '0'
        ms = int(ms_str.ljust(3, '0')[:3]) * 1000  # 转为微秒
        return base.replace(microsecond=ms)
    except Exception:
        return None

class CoordinateConverter:
    def __init__(self, center_lon, center_lat, radius_m):
        self.center_lon = center_lon
        self.center_lat = center_lat
        self.radius_m = radius_m
        self.wgs84 = CRS.from_epsg(4326)
        self._select_projection()
        self._init_transformer()

    def _select_projection(self):
        if self.radius_m <= 50000:
            self.proj_name = 'utm'
            utm_zone = int((self.center_lon + 180) / 6) + 1
            hemisphere = 'north' if self.center_lat >= 0 else 'south'
            self.proj_crs = CRS.from_string(
                f"+proj=utm +zone={utm_zone} +{hemisphere} +ellps=WGS84 +datum=WGS84 +units=m +no_defs"
            )
        elif self.radius_m <= 150000:
            self.proj_name = 'lcc'
            self.proj_crs = CRS.from_string(
                f"+proj=lcc +lat_1={self.center_lat - 1} +lat_2={self.center_lat + 1} "
                f"+lat_0={self.center_lat} +lon_0={self.center_lon} +x_0=0 +y_0=0 "
                "+ellps=WGS84 +datum=WGS84 +units=m +no_defs"
            )
        else:
            self.proj_name = 'merc'
            self.proj_crs = CRS.from_string(
                f"+proj=merc +lat_0={self.center_lat} +lon_0={self.center_lon} "
                "+k=1.0 +x_0=0 +y_0=0 +ellps=WGS84 +datum=WGS84 +units=m +no_defs"
            )

    def _init_transformer(self):
        self.transformer_to_proj = Transformer.from_crs(self.wgs84, self.proj_crs, always_xy=True)
        self.transformer_from_proj = Transformer.from_crs(self.proj_crs, self.wgs84, always_xy=True)
        self.center_x, self.center_y = self.transformer_to_proj.transform(self.center_lon, self.center_lat)

    def lonlat_to_xy(self, lon, lat):
        proj_x, proj_y = self.transformer_to_proj.transform(lon, lat)
        return proj_x - self.center_x, proj_y - self.center_y

class Jammer:
    def __init__(self, idx, uuid, x, y, alt, mode, jam_type, show_name):
        self.id = idx
        self.uuid = uuid
        self.x = x
        self.y = y
        self.alt = alt
        self.mode = mode
        self.jam_type = jam_type
        self.show_name = show_name
        self.active = False
        self.current_target_id = None
        self.pointing_azimuth = 0.0
        self.pointing_elevation = 0.0
        self.effective_radius = 0.0
        self.horiz_beamwidth = 20.0
        self.vert_beamwidth = 25.0

    def reset(self):
        self.active = False
        self.current_target_id = None
        self.pointing_azimuth = 0.0
        self.pointing_elevation = 0.0
        self.effective_radius = 0.0

class Target:
    def __init__(self, idx, name, threat_value, threat_grade, scene_radius, radar_freq_hz, radar_bandwidth_hz):
        self.id = idx
        self.name = name
        self.time_series: List[Tuple[float, float, float, float]] = []
        self.threat_value = threat_value
        self.threat_grade = threat_grade
        self.scene_radius = scene_radius
        self.radar_freq_hz = radar_freq_hz
        self.radar_bandwidth_hz = radar_bandwidth_hz

    def add_position(self, t_rel, x, y, alt):
        self.time_series.append((t_rel, x, y, alt))

    def current_position_xy(self, t):
        if not self.time_series: return None, None, None
        if t <= self.time_series[0][0]: return self.time_series[0][1:]
        if t >= self.time_series[-1][0]: return self.time_series[-1][1:]
        for i in range(len(self.time_series) - 1):
            t1, x1, y1, alt1 = self.time_series[i]
            t2, x2, y2, alt2 = self.time_series[i+1]
            if t1 <= t <= t2:
                ratio = (t - t1) / (t2 - t1)
                x = x1 + (x2 - x1) * ratio
                y = y1 + (y2 - y1) * ratio
                alt = alt1 + (alt2 - alt1) * ratio
                return x, y, alt
        return self.time_series[-1][1:]

    def has_finished(self, t):
        x, y, _ = self.current_position_xy(t)
        if x is None: return True
        return math.hypot(x, y) > self.scene_radius * 1.5

class RadarJammerSimulation:
    radar_strategies = [
        {"mode": "suppress_narrowband",     "params": {"cf_mhz": 10000.0, "bandwidth_mhz": 50.0}, "jam_type": "suppressive"},
        {"mode": "suppress_comb_spectrum",  "params": {"cf_mhz": 15000.0, "bandwidth_mhz": 100.0}, "jam_type": "suppressive"},
        {"mode": "suppress_single_tone",    "params": {"cf_mhz": 8000.0,  "bandwidth_mhz": 1.0},  "jam_type": "suppressive"},
        {"mode": "deception_slice_forward", "params": {"cf_mhz": 8000.0,  "bandwidth_mhz": 30.0}, "jam_type": "deceptive"},
        {"mode": "deception_dense_copy",    "params": {"cf_mhz": 10000.0, "bandwidth_mhz": 40.0}, "jam_type": "deceptive"},
        {"mode": "deception_range",         "params": {"cf_mhz": 12000.0, "bandwidth_mhz": 20.0}, "jam_type": "deceptive"},
        {"mode": "deception_velocity",      "params": {"cf_mhz": 15000.0, "bandwidth_mhz": 25.0}, "jam_type": "deceptive"},
    ]

    @classmethod
    def create_stream_simulator(cls, project_name: str, deployment: str = 'optimized', silent: bool = True,
                                mqtt_callback=None):
        config_dir = os.path.dirname(os.path.abspath(__file__))
        jammer_file = os.path.join(config_dir, "data", project_name, "radar_jammer_positions.json")
        if not os.path.exists(jammer_file):
            raise FileNotFoundError(f"未找到雷达干扰机文件: {jammer_file}")

        sim = cls.__new__(cls)
        sim.config_dir = config_dir
        sim.deployment = deployment
        sim.silent = silent
        sim.project_name = project_name
        sim.mqtt_callback = mqtt_callback

        with open(jammer_file, 'r', encoding='utf-8') as f:
            sim.radar_data = json.load(f)

        sim.radius_suppressive = find_radar_distance_for_success_rate('suppressive', 0.7)
        sim.radius_deceptive = find_radar_distance_for_success_rate('deceptive', 0.7)
        sim.fixed_success_rate = 0.7

        sim._setup_coordinate_system()
        sim._load_jammers_and_guardpoints()
        sim._setup_coverage_grid()

        sim.targets: List[Target] = []
        sim.target_dict: Dict[int, Target] = {}

        sim.dt = 0.1
        sim.num_jammers = len(sim.jammers)
        sim._jam_duration_per = np.zeros(sim.num_jammers)
        sim._jam_length_per = np.zeros(sim.num_jammers)
        sim._coverage_area_integral_per = np.zeros(sim.num_jammers)

        sim._last_target_pos_per = [None] * sim.num_jammers
        sim._last_target_id_per = [None] * sim.num_jammers

        sim._current_time = 0.0
        sim._max_t_rel = 0.0
        sim._total_steps = 0

        # 绝对时间基准（精确到毫秒）
        sim.sim_start_datetime = None

        sim.target_radar_params = {}
        for tgt_info in sim.radar_data.get('targets', []):
            idx = tgt_info.get('baseInfo', {}).get('index')
            if idx is not None:
                radar_info = tgt_info.get('modelInfo', {}).get('radarSignalInfo', {})
                freq_hz = parse_freq_to_hz(radar_info.get('carrierFreq', '0 Hz'))
                bw_hz = parse_bandwidth_to_hz(radar_info.get('bandwidth', '0 Hz'))
                sim.target_radar_params[idx] = (freq_hz, bw_hz)

        sim.results_dir = os.path.join(config_dir, "data", project_name, "radar_data",
                                       "random" if deployment == "random" else "optimized")
        sim.data_dir = os.path.join(sim.results_dir, "data")
        try:
            os.makedirs(sim.data_dir, exist_ok=True)
            logger.info(f"[雷达-{deployment}] 数据目录已创建: {sim.data_dir}")
        except Exception as e:
            logger.error(f"[雷达-{deployment}] 创建数据目录失败: {e}")

        logger.info(f"[雷达] 流式模拟器创建成功，部署类型: {deployment}，干扰机数量: {sim.num_jammers}，有效半径: 压制{sim.radius_suppressive:.0f}m 欺骗{sim.radius_deceptive:.0f}m")
        return sim

    def _setup_coordinate_system(self):
        scene = self.radar_data['scene']
        self.center_lon = float(scene['longitude'])
        self.center_lat = float(scene['latitude'])
        self.radius_m = float(scene['radius'])
        self.converter = CoordinateConverter(self.center_lon, self.center_lat, self.radius_m)

    def _load_jammers_and_guardpoints(self):
        self.guard_points_xy = []
        for gp in self.radar_data['guardpoints']:
            lon = float(gp['longitude'])
            lat = float(gp['latitude'])
            alt = float(gp.get('altitude', 0))
            x, y = self.converter.lonlat_to_xy(lon, lat)
            self.guard_points_xy.append({'index': gp['index'], 'x': x, 'y': y, 'alt': alt, 'name': gp['showName']})

        if self.deployment == 'random':
            jammer_list_key = 'originaljammers' if 'originaljammers' in self.radar_data else 'originalJammers'
        else:
            jammer_list_key = 'optimizedjammers' if 'optimizedjammers' in self.radar_data else 'optimizedJammers'

        jammer_data = self.radar_data.get(jammer_list_key, [])
        if not jammer_data:
            raise ValueError(f"未找到干扰机列表: {jammer_list_key}")

        num_strategies = len(self.radar_strategies)
        self.jammers = []
        for i, jam in enumerate(jammer_data):
            lon = float(jam['longitude'])
            lat = float(jam['latitude'])
            alt = float(jam.get('altitude', 0))
            x, y = self.converter.lonlat_to_xy(lon, lat)
            strategy = self.radar_strategies[i % num_strategies]
            show_name = jam.get('showName', f"雷达干扰机_{jam['uuid']}")
            jammer = Jammer(i, jam['uuid'], x, y, alt, strategy['mode'], strategy['jam_type'], show_name)
            self.jammers.append(jammer)

        logger.info(f"[雷达-{self.deployment}] 加载 {len(self.jammers)} 个干扰机，示例坐标: ({self.jammers[0].x:.0f}, {self.jammers[0].y:.0f})")

    def _setup_coverage_grid(self):
        self.grid_cell_size = 50
        R = self.radius_m
        x_coords = np.arange(-R, R, self.grid_cell_size) + self.grid_cell_size/2
        y_coords = np.arange(-R, R, self.grid_cell_size) + self.grid_cell_size/2
        self.grid_x, self.grid_y = np.meshgrid(x_coords, y_coords)
        self.grid_x = self.grid_x.flatten()
        self.grid_y = self.grid_y.flatten()
        dist_from_center = np.hypot(self.grid_x, self.grid_y)
        mask = dist_from_center <= R
        self.grid_x = self.grid_x[mask]
        self.grid_y = self.grid_y[mask]
        self.grid_cell_area = self.grid_cell_size ** 2

    def _ensure_target_exists(self, tgt_info: dict, t_rel: float):
        tid = tgt_info.get('id')
        if tid is None: return None
        if tid not in self.target_dict:
            threat_val = tgt_info.get('threat_value', 0.5)
            threat_grade = tgt_info.get('threat_grade', '中威胁')
            name = tgt_info.get('class', f'Target_{tid}')
            freq_hz, bw_hz = self.target_radar_params.get(tid, (10e9, 100e6))
            target = Target(tid, name, threat_val, threat_grade, self.radius_m, freq_hz, bw_hz)
            self.target_dict[tid] = target
            self.targets.append(target)
            logger.info(f"[雷达-{self.deployment}] 新目标 {tid}，威胁值 {threat_val:.2f}，雷达频率 {freq_hz/1e6:.1f}MHz，带宽 {bw_hz/1e6:.1f}MHz")

        loc = tgt_info.get('location', [0, 0, 0])
        if len(loc) >= 3: lon, lat, alt = loc[0], loc[1], loc[2]
        else: lon, lat, alt = loc[0], loc[1], 0
        x, y = self.converter.lonlat_to_xy(lon, lat)
        self.target_dict[tid].add_position(t_rel, x, y, alt)
        logger.debug(f"[雷达-{self.deployment}] 目标{tid} 位置更新: t={t_rel:.1f}s, xy=({x:.0f},{y:.0f})")
        return self.target_dict[tid]

    def update_with_frame(self, frame: dict, t_rel: float):
        targets_in_frame = frame.get('target_info', [])
        logger.info(f"[雷达-{self.deployment}] 收到帧，t_rel={t_rel:.2f}，目标数={len(targets_in_frame)}")
        if targets_in_frame:
            sample = targets_in_frame[0]
            loc = sample.get('location', [])
            if len(loc) >= 2:
                logger.info(f"[雷达-{self.deployment}] 示例目标: id={sample.get('id')}, lon={loc[0]:.6f}, lat={loc[1]:.6f}")

        # 初始化绝对时间基准（精确到毫秒）
        if self.sim_start_datetime is None:
            time_str = frame.get('time', '')
            frame_dt = parse_timestamp_to_datetime(time_str)
            if frame_dt:
                self.sim_start_datetime = frame_dt - timedelta(seconds=t_rel)
                logger.info(f"[雷达-{self.deployment}] 模拟起始时间: {self.sim_start_datetime.strftime('%Y-%m-%d %H:%M:%S.%f')[:-3]}")
            else:
                logger.warning(f"[雷达-{self.deployment}] 无效时间戳，使用系统当前时间")
                self.sim_start_datetime = datetime.now() - timedelta(seconds=t_rel)

        for tgt in targets_in_frame:
            self._ensure_target_exists(tgt, t_rel)

        if t_rel <= self._current_time:
            logger.debug(f"[雷达-{self.deployment}] 时间未推进 (当前={self._current_time:.2f})，跳过")
            return

        start_t = self._current_time
        end_t = t_rel
        self._max_t_rel = max(self._max_t_rel, end_t)

        current_t = start_t
        steps = 0
        while current_t < end_t:
            next_t = min(current_t + self.dt, end_t)
            self._simulate_step(next_t)
            current_t = next_t
            steps += 1

        self._current_time = end_t
        active_count = sum(1 for j in self.jammers if j.active)
        logger.info(f"[雷达-{self.deployment}] 推进完成，步数={steps}，当前时间={self._current_time:.2f}，目标数={len(self.targets)}，激活干扰机={active_count}")

    def _simulate_step(self, t: float):
        self._update_jammer_states(t)
        areas = self._compute_jammer_coverage_areas(t)
        self._coverage_area_integral_per += np.array(areas) * self.dt

        for i, jammer in enumerate(self.jammers):
            if jammer.active and jammer.current_target_id is not None:
                target = self.target_dict.get(jammer.current_target_id)
                if target and not target.has_finished(t):
                    x, y, _ = target.current_position_xy(t)
                    if x is not None:
                        if self._last_target_id_per[i] == jammer.current_target_id and self._last_target_pos_per[i] is not None:
                            last_x, last_y, _ = self._last_target_pos_per[i]
                            dist = math.hypot(x - last_x, y - last_y)
                            if dist > 0:
                                self._jam_duration_per[i] += self.dt
                                self._jam_length_per[i] += dist
                        else:
                            self._jam_duration_per[i] += self.dt
                        self._last_target_id_per[i] = jammer.current_target_id
                        self._last_target_pos_per[i] = (x, y, 0)
                    else:
                        self._last_target_id_per[i] = None
                        self._last_target_pos_per[i] = None
                else:
                    self._last_target_id_per[i] = None
                    self._last_target_pos_per[i] = None
            else:
                self._last_target_id_per[i] = None
                self._last_target_pos_per[i] = None

        self._total_steps += 1
        self._save_data(t)

    def _save_data(self, t: float):
        """保存当前时间步的干扰机状态，并可选发送 MQTT。时间精确到毫秒"""
        if self.sim_start_datetime:
            abs_time = self.sim_start_datetime + timedelta(seconds=t)
            time_str = abs_time.strftime('%Y-%m-%d %H:%M:%S.%f')[:-3]
        else:
            time_str = f"{t:.1f}"

        data = {
            "type": f"radar_{self.deployment}",
            "time": time_str,
            "jammers": []
        }
        for jammer in self.jammers:
            jammer_info = {
                "jammer_id": jammer.uuid,
                "showName": jammer.show_name,
                "azimuth": jammer.pointing_azimuth,
                "elevation": jammer.pointing_elevation,
                "jam_radius": jammer.effective_radius,
                "active": jammer.active,
                "mode": jammer.mode,
                "jam_type": jammer.jam_type
            }
            data["jammers"].append(jammer_info)

        # 本地文件保存
        filename = f"data_{t:.1f}.json"
        filepath = os.path.join(self.data_dir, filename)
        try:
            with open(filepath, 'w', encoding='utf-8') as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
            logger.debug(f"[雷达-{self.deployment}] 保存步数据: {filepath}")
        except Exception as e:
            logger.error(f"[雷达-{self.deployment}] 保存数据失败 {filepath}: {e}")

        # MQTT 实时发送
        if self.mqtt_callback is not None:
            try:
                self.mqtt_callback(data)
                logger.debug(f"[雷达-{self.deployment}] MQTT 步数据已发送, t={t:.1f}")
            except Exception as e:
                logger.error(f"[雷达-{self.deployment}] MQTT 发送步数据失败: {e}", exc_info=True)

    def _update_jammer_states(self, current_time):
        active_targets = [t for t in self.targets if not t.has_finished(current_time)]
        if not active_targets:
            for jammer in self.jammers:
                jammer.active = False
                jammer.current_target_id = None
                jammer.effective_radius = 0.0
            return

        feasible = {}
        jam_type_dict = {}
        info = {}
        coverage_gain = {}
        for jammer in self.jammers:
            for target in active_targets:
                jtype, success, dist, az, el, eff_rad, mode = self._evaluate_jammer_for_target(jammer, target, current_time)
                if jtype is not None:
                    feasible[(jammer.id, target.id)] = True
                    jam_type_dict[(jammer.id, target.id)] = jtype
                    info[(jammer.id, target.id)] = (success, dist, az, el, eff_rad, mode)
                    target_potential = self._calculate_target_coverage_potential(target, current_time)
                    coverage_gain[(jammer.id, target.id)] = target.threat_value * target_potential
                else:
                    feasible[(jammer.id, target.id)] = False
                    coverage_gain[(jammer.id, target.id)] = 0

        active_targets.sort(
            key=lambda t: max(coverage_gain.get((j.id, t.id), 0) for j in self.jammers),
            reverse=True
        )

        unassigned_jammers = set(self.jammers)
        assignments = {}

        for target in active_targets:
            best_jammer = None
            max_gain = -1
            best_info = None
            best_jtype = None
            for jammer in unassigned_jammers:
                if feasible.get((jammer.id, target.id), False):
                    gain = coverage_gain[(jammer.id, target.id)]
                    if gain > max_gain:
                        max_gain = gain
                        best_jammer = jammer
                        best_jtype = jam_type_dict[(jammer.id, target.id)]
                        best_info = info[(jammer.id, target.id)]
            if best_jammer is not None:
                success, dist, az, el, eff_rad, mode = best_info
                assignments[best_jammer] = (target.id, best_jtype, az, el, dist, eff_rad, mode)
                unassigned_jammers.remove(best_jammer)

        for jammer in list(unassigned_jammers):
            best_target = None
            max_gain = -1
            best_jtype = None
            best_info = None
            for target in active_targets:
                if feasible.get((jammer.id, target.id), False):
                    gain = coverage_gain[(jammer.id, target.id)]
                    if gain > max_gain:
                        max_gain = gain
                        best_target = target
                        best_jtype = jam_type_dict[(jammer.id, target.id)]
                        best_info = info[(jammer.id, target.id)]
            if best_target is not None:
                success, dist, az, el, eff_rad, mode = best_info
                assignments[jammer] = (best_target.id, best_jtype, az, el, dist, eff_rad, mode)
                unassigned_jammers.remove(jammer)

        for jammer in self.jammers:
            if jammer in assignments:
                target_id, jtype, az, el, dist, eff_rad, mode = assignments[jammer]
                jammer.active = True
                jammer.current_target_id = target_id
                jammer.jam_type = jtype
                jammer.mode = mode
                jammer.pointing_azimuth = az
                jammer.pointing_elevation = el
                jammer.effective_radius = eff_rad
            else:
                jammer.active = False
                jammer.current_target_id = None
                jammer.effective_radius = 0.0

    def _evaluate_jammer_for_target(self, jammer, target, current_time):
        if target.has_finished(current_time): return None, 0, None, None, None, None, None
        pos = target.current_position_xy(current_time)
        if pos[0] is None: return None, 0, None, None, None, None, None
        tx, ty, talt = pos
        dx = tx - jammer.x
        dy = ty - jammer.y
        dz = talt - jammer.alt
        dist = math.hypot(dx, dy)
        if dist > 100000: return None, 0, None, None, None, None, None
        azimuth = math.degrees(math.atan2(dx, dy)) % 360
        elevation = math.degrees(math.atan2(dz, dist)) if dist > 0 else (90 if dz > 0 else -90)

        if dist <= self.radius_suppressive:
            jam_type = 'suppressive'
            effective_radius = self.radius_suppressive
        elif dist <= self.radius_deceptive:
            jam_type = 'deceptive'
            effective_radius = self.radius_deceptive
        else:
            return None, 0, None, None, None, None, None

        target_freq_hz = target.radar_freq_hz
        candidate_strategies = [s for s in self.radar_strategies if s['jam_type'] == jam_type]
        if not candidate_strategies:
            return None, 0, None, None, None, None, None

        best_strategy = None
        min_freq_diff = float('inf')
        for strat in candidate_strategies:
            strat_freq_hz = strat['params']['cf_mhz'] * 1e6
            diff = abs(strat_freq_hz - target_freq_hz)
            if diff < min_freq_diff:
                min_freq_diff = diff
                best_strategy = strat

        if best_strategy is None:
            return None, 0, None, None, None, None, None

        return jam_type, self.fixed_success_rate, dist, azimuth, elevation, effective_radius, best_strategy['mode']

    def _calculate_target_coverage_potential(self, target, current_time):
        if target.has_finished(current_time): return 0
        tx, ty, _ = target.current_position_xy(current_time)
        if tx is None: return 0
        max_radius = max(self.radius_suppressive, self.radius_deceptive)
        dx = self.grid_x - tx
        dy = self.grid_y - ty
        dist = np.hypot(dx, dy)
        mask = dist <= max_radius
        return np.sum(mask)

    def _compute_jammer_coverage_areas(self, current_time):
        areas = [0.0] * len(self.jammers)
        if not self.grid_x.size: return areas
        for i, jammer in enumerate(self.jammers):
            if not jammer.active or jammer.effective_radius <= 0: continue
            dx = self.grid_x - jammer.x
            dy = self.grid_y - jammer.y
            dist_to_jammer = np.hypot(dx, dy)
            az_rad = np.arctan2(dx, dy)
            az_deg = np.degrees(az_rad) % 360
            angle_diff = np.abs((az_deg - jammer.pointing_azimuth + 180) % 360 - 180)
            mask = (dist_to_jammer <= jammer.effective_radius) & (angle_diff <= jammer.horiz_beamwidth / 2)
            covered_cells = np.sum(mask)
            areas[i] = covered_cells * self.grid_cell_area
        return areas

    def finalize(self) -> Dict[str, Any]:
        total_time = self._total_steps * self.dt
        if total_time <= 0:
            total_time = self.dt
        avg_coverage_per = self._coverage_area_integral_per / total_time

        jammer_metrics = []
        for i, jammer in enumerate(self.jammers):
            jammer_metrics.append({
                'jammer_id': jammer.uuid,
                'showName': jammer.show_name,
                'jam_duration_s': float(self._jam_duration_per[i]),
                'effective_jamming_length_m': float(self._jam_length_per[i]),
                'effective_coverage_area_m2': float(avg_coverage_per[i])
            })

        overall = {
            'jam_duration': float(np.mean(self._jam_duration_per)) if self.num_jammers > 0 else 0.0,
            'effective_jamming_length': float(np.sum(self._jam_length_per)),
            'effective_coverage_area': float(np.mean(avg_coverage_per))
        }
        logger.info(f"[雷达-{self.deployment}] 最终指标: 总时间={total_time:.1f}s, 平均干扰时长={overall['jam_duration']:.2f}s, 总有效长度={overall['effective_jamming_length']:.2f}m")
        return {'jammers': jammer_metrics, 'overall': overall}

    def run_simulation(self):
        raise NotImplementedError("批量模拟已禁用，请使用流式接口")

    @classmethod
    def compare_deployments(cls, verbose=True, **kwargs):
        raise NotImplementedError("批量对比已禁用，请使用流式接口")