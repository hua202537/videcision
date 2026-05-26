"""
雷达干扰机位置优化部署
针对jammerType=7的雷达干扰机进行优化部署
（优化版：面积覆盖率使用numpy加速）
"""

import json
import os
import math
import re  # 新增，用于从 UUID 提取数字
import numpy as np
from pyproj import Transformer, CRS, Geod
import matplotlib.pyplot as plt
from pyswarm import pso
import sys
import io

# 导入现有的雷达干扰计算模块
from radar_jamming_range_calculation import (
    RadarParams, JammerRadarParams, CalculationOptions,
    radar_jamming_range_calculation
)

# 配置matplotlib中文字体
plt.rcParams['font.sans-serif'] = ['SimHei', 'Microsoft YaHei', 'DejaVu Sans']
plt.rcParams['axes.unicode_minus'] = False


class RadarJammerOptimization:
    def __init__(self, config_file=None, config=None):
        """
        初始化雷达干扰机优化类

        Args:
            config_file: JSON配置文件路径
            config: 直接传入的配置字典
        """
        if config_file is not None:
            with open(config_file, 'r', encoding='utf-8') as f:
                self.config = json.load(f)
        elif config is not None:
            self.config = config
        else:
            raise ValueError("必须提供 config_file 或 config 参数")

        # 解析场景信息（WGS84）
        self.scene = self.config.get('scene', {})
        self.center_lat = float(self.scene.get('latitude', 40.0779))
        self.center_lon = float(self.scene.get('longitude', 116.554))
        self.center_alt = float(self.scene.get('altitude', 0))
        self.radius_m = float(self.scene.get('radius', 10000))
        self.projectname = self.scene.get('projectname', 'faeef')

        print(f"场景中心 (WGS84): 纬度={self.center_lat}, 经度={self.center_lon}")
        print(f"场景半径: {self.radius_m}米, 项目名称: {self.projectname}")

        self.boundary_margin = min(500, self.radius_m * 0.1)
        self.effective_radius = self.radius_m - self.boundary_margin

        # 解析要地
        self.guard_points = self.config.get('guardPoints', [])
        print(f"要地数量: {len(self.guard_points)}")

        # 解析干扰机（只取jammerType=7）
        self.original_jammers = []
        self.jammer_configs = {}
        jammer_dict = self.config.get('jammer', {})
        for jammer_key, jammer_data in jammer_dict.items():
            if 'radarJammerInfo' in jammer_data:
                radar_info = jammer_data['radarJammerInfo']
                if radar_info.get('jammerType', 0) == 7:
                    sensor_info = jammer_data['sensorInfo']
                    jammer_info = {
                        'uuid': sensor_info.get('uuid', ''),
                        'index': sensor_info.get('index', 0),
                        'latitude': float(sensor_info.get('latitude', 0)),
                        'longitude': float(sensor_info.get('longitude', 0)),
                        'altitude': float(sensor_info.get('altitude', 0)),
                        'showName': sensor_info.get('showName', ''),
                        'jammerType': 7,
                        'config': radar_info
                    }
                    self.original_jammers.append(jammer_info)
                    self.jammer_configs[jammer_key] = radar_info

        print(f"雷达干扰机数量: {len(self.original_jammers)}")

        # 解析飞行路径（用于绘图，不参与优化）
        self.paths = self.config.get('paths', {})
        print(f"飞行路径数量: {len(self.paths)}")

        self.targets = self.config.get('targets', [])
        print(f"目标数量: {len(self.targets)}")

        # 投影设置
        self.geod = Geod(ellps='WGS84')
        self.wgs84 = CRS.from_epsg(4326)

        if self.radius_m <= 50000:
            self.projection_method = self.utm_projection
        elif self.radius_m <= 150000:
            self.projection_method = self.lambert_projection
        else:
            self.projection_method = self.mercator_projection

        self.transformer, self.target_crs = self.projection_method()
        self.center_x, self.center_y = self.transformer.transform(self.center_lon, self.center_lat)

        # 存储转换后的坐标
        self.guard_points_xy = []
        self.original_jammers_xy = []
        self.paths_xy = {}
        self.targets_xy = []

        self.convert_all_coordinates()

        # 计算干扰半径
        self.calculate_jamming_radius()

        # 预生成用于面积覆盖率的网格（优化关键点）
        self._precompute_area_grid()

    def utm_projection(self):
        utm_zone = int((self.center_lon + 180) / 6) + 1
        hemisphere = 'north' if self.center_lat >= 0 else 'south'
        utm_crs = CRS.from_string(
            f"+proj=utm +zone={utm_zone} +{hemisphere} +ellps=WGS84 +datum=WGS84 +units=m +no_defs")
        transformer = Transformer.from_crs(self.wgs84, utm_crs, always_xy=True)
        return transformer, utm_crs

    def lambert_projection(self):
        lambert_crs = CRS.from_string(
            f"+proj=lcc +lat_1={self.center_lat - 1} +lat_2={self.center_lat + 1} "
            f"+lat_0={self.center_lat} +lon_0={self.center_lon} +x_0=0 +y_0=0 "
            "+ellps=WGS84 +datum=WGS84 +units=m +no_defs")
        transformer = Transformer.from_crs(self.wgs84, lambert_crs, always_xy=True)
        return transformer, lambert_crs

    def mercator_projection(self):
        mercator_crs = CRS.from_string(
            f"+proj=merc +lat_0={self.center_lat} +lon_0={self.center_lon} "
            "+k=1.0 +x_0=0 +y_0=0 +ellps=WGS84 +datum=WGS84 +units=m +no_defs")
        transformer = Transformer.from_crs(self.wgs84, mercator_crs, always_xy=True)
        return transformer, mercator_crs

    def latlon_to_xy(self, lon, lat):
        proj_x, proj_y = self.transformer.transform(lon, lat)
        x = proj_x - self.center_x
        y = proj_y - self.center_y
        return x, y

    def xy_to_latlon(self, x, y):
        proj_x = self.center_x + x
        proj_y = self.center_y + y
        lon, lat = self.transformer.transform(proj_x, proj_y, direction='INVERSE')
        return lon, lat

    def convert_all_coordinates(self):
        # 转换要地
        for guard in self.guard_points:
            lat = float(guard.get('latitude', 0))
            lon = float(guard.get('longitude', 0))
            x, y = self.latlon_to_xy(lon, lat)
            self.guard_points_xy.append({
                'index': guard.get('index', 0),
                'x': x, 'y': y, 'showname': guard.get('showName', ''),
                'original': guard, 'lon': lon, 'lat': lat
            })

        # 转换原始干扰机
        for jammer in self.original_jammers:
            lat = jammer['latitude']
            lon = jammer['longitude']
            x, y = self.latlon_to_xy(lon, lat)
            self.original_jammers_xy.append({
                'uuid': jammer['uuid'],
                'x': x, 'y': y,
                'original': jammer, 'lon': lon, 'lat': lat
            })

        # 转换路径（仅用于绘图）
        for path_key, path_data in self.paths.items():
            path_points = []
            point_keys = [key for key in path_data.keys() if key.startswith('point_')]
            point_keys_sorted = sorted(point_keys, key=lambda x: int(x.split('_')[1]))
            for point_key in point_keys_sorted:
                point_data = path_data[point_key]
                pos_str = point_data.get('position', '')
                if pos_str:
                    parts = pos_str.split()
                    if len(parts) >= 2:
                        lon = float(parts[0])
                        lat = float(parts[1])
                        x, y = self.latlon_to_xy(lon, lat)
                        path_points.append({
                            'index': int(point_key.split('_')[1]),
                            'x': x, 'y': y,
                            'time': point_data.get('time', 0),
                            'original': point_data, 'lon': lon, 'lat': lat
                        })
            if path_points:
                self.paths_xy[path_key] = {
                    'showname': path_data.get('showName', ''),
                    'uuid': path_data.get('uuid', ''),
                    'points': path_points
                }

        # 转换目标
        for target in self.targets:
            base_info = target.get('baseInfo', {})
            lat = float(base_info.get('latitude', 0))
            lon = float(base_info.get('longitude', 0))
            x, y = self.latlon_to_xy(lon, lat)
            self.targets_xy.append({
                'index': base_info.get('index', 0),
                'x': x, 'y': y,
                'showname': base_info.get('showName', ''),
                'original': target, 'lon': lon, 'lat': lat
            })

    def suppress_print(func):
        def wrapper(*args, **kwargs):
            original_stdout = sys.stdout
            sys.stdout = io.StringIO()
            try:
                result = func(*args, **kwargs)
            finally:
                sys.stdout = original_stdout
            return result
        return wrapper

    @suppress_print
    def silent_radar_jamming_calculation(self, radar_params, jammer_params, options):
        R_jam, results = radar_jamming_range_calculation(
            radar_params, jammer_params, 'noise', options
        )
        return R_jam, results

    def calculate_jamming_radius(self):
        scene_radius_km = self.radius_m / 1000
        print(f"场景半径: {scene_radius_km:.1f}km, 固定使用70%成功率阈值计算干扰半径")

        # 与 test_RadJamSUCC_Range.py 相同参数
        radar_params = RadarParams(
            power=200, gain=30, frequency=16, bandwidth=100,
            loss=4, noise_figure=3, rcs=1, Rt=2000
        )
        jammer_params = JammerRadarParams(
            power=20, gain=7, bandwidth=100, loss=30,
            deception_gain=6, DRFM_quality=0.85
        )
        options = CalculationOptions(
            success_rate_threshold=0.7,
            distance_range=(1.0, 100.0),
            max_iterations=100
        )

        try:
            R_jam, results = self.silent_radar_jamming_calculation(
                radar_params, jammer_params, options
            )
            self.jamming_radius_m = R_jam * 1000
            print(f"计算得到的雷达干扰半径: {R_jam:.2f}km ({self.jamming_radius_m:.0f}米)")
        except Exception as e:
            print(f"雷达干扰半径计算失败: {e}")
            if scene_radius_km <= 10:
                self.jamming_radius_m = 5000
            elif scene_radius_km <= 50:
                self.jamming_radius_m = 15000
            else:
                self.jamming_radius_m = 30000
            print(f"默认干扰半径: {self.jamming_radius_m / 1000:.1f}km")

    def _precompute_area_grid(self):
        """预生成用于面积覆盖率计算的网格点坐标（numpy数组）"""
        # 根据场景半径自适应步长：保证约2000个采样点（可调）
        step = min(self.radius_m / 50, 100)  # 默认步长不超过100米，不少于半径/50
        step = max(step, 10)  # 最小10米，防止过密
        x = np.arange(-self.radius_m, self.radius_m + step, step)
        y = np.arange(-self.radius_m, self.radius_m + step, step)
        xx, yy = np.meshgrid(x, y)
        # 只保留场景圆盘内的点
        r2 = xx**2 + yy**2
        mask = r2 <= self.radius_m**2
        self.grid_points = np.column_stack((xx[mask], yy[mask]))  # N x 2
        self.grid_points_count = len(self.grid_points)
        print(f"面积覆盖率采样点数量: {self.grid_points_count}")

    def calculate_area_coverage(self, jammers_xy):
        """使用numpy向量化计算干扰覆盖率（速度快）"""
        if not jammers_xy or self.grid_points_count == 0:
            return 0.0

        # 构建干扰机坐标数组 (M x 2)
        jammer_coords = np.array([[j['x'], j['y']] for j in jammers_xy])
        M = len(jammer_coords)

        # 计算每个网格点到每个干扰机的距离平方 (N x M)
        # 使用广播：grid_points[:, None, :] - jammer_coords[None, :, :]
        diff = self.grid_points[:, np.newaxis, :] - jammer_coords[np.newaxis, :, :]
        dist_sq = np.sum(diff**2, axis=2)  # N x M
        # 是否有任意干扰机距离 <= 半径
        covered = np.any(dist_sq <= self.jamming_radius_m**2, axis=1)
        covered_count = np.sum(covered)
        return covered_count / self.grid_points_count

    def calculate_guard_coverage(self, jammers_xy):
        if not self.guard_points_xy or not jammers_xy:
            return 0.0
        covered = 0
        for guard in self.guard_points_xy:
            gx, gy = guard['x'], guard['y']
            for jammer in jammers_xy:
                if math.hypot(gx - jammer['x'], gy - jammer['y']) <= self.jamming_radius_m:
                    covered += 1
                    break
        return covered / len(self.guard_points_xy)

    def calculate_jammer_overlap(self, jammers_xy):
        """计算平均重叠率，使用向量化加速"""
        n = len(jammers_xy)
        if n <= 1:
            return 0.0
        r = self.jamming_radius_m
        total_ratio = 0.0
        count = 0
        # 提取坐标数组
        coords = np.array([[j['x'], j['y']] for j in jammers_xy])
        for i in range(n):
            for j in range(i+1, n):
                d = math.hypot(coords[i,0]-coords[j,0], coords[i,1]-coords[j,1])
                if d >= 2*r:
                    ratio = 0.0
                elif d <= 0:
                    ratio = 1.0
                else:
                    # 两圆重叠面积公式
                    part1 = r**2 * math.acos((d**2 + r**2 - r**2) / (2*d*r))
                    part2 = r**2 * math.acos((d**2 + r**2 - r**2) / (2*d*r))
                    part3 = 0.5 * math.sqrt((-d+2*r)*(d)*(d)*(d+2*r))
                    overlap_area = part1 + part2 - part3
                    ratio = overlap_area / (math.pi * r**2)
                total_ratio += ratio
                count += 1
        return total_ratio / count if count > 0 else 0.0

    def objective_function(self, positions):
        num_jammers = len(self.original_jammers_xy)
        jammers_xy = []
        for i in range(num_jammers):
            x = positions[i*2]
            y = positions[i*2+1]
            if math.hypot(x, y) > self.effective_radius:
                return 1e6
            jammers_xy.append({'x': x, 'y': y})
        guard_cov = self.calculate_guard_coverage(jammers_xy)
        area_cov = self.calculate_area_coverage(jammers_xy)
        overlap = self.calculate_jammer_overlap(jammers_xy)
        # 目标：最大化覆盖、最小化重叠
        return 0.4*(1-guard_cov) + 0.4*(1-area_cov) + 0.2*overlap

    def optimize_jammer_positions(self):
        print("\n开始优化雷达干扰机位置...")
        num_jammers = len(self.original_jammers_xy)
        if num_jammers == 0:
            print("没有雷达干扰机需要优化")
            return []
        n_particles = 20   # 减少粒子数可加快速度
        n_iterations = 80  # 减少迭代次数
        lb = [-self.radius_m] * (num_jammers * 2)
        ub = [self.radius_m] * (num_jammers * 2)
        initial = []
        for jammer in self.original_jammers_xy:
            initial.append(jammer['x'])
            initial.append(jammer['y'])
        print(f"PSO参数: {n_particles}个粒子, {n_iterations}次迭代")
        try:
            best_pos, best_val = pso(self.objective_function, lb, ub,
                                     swarmsize=n_particles, maxiter=n_iterations,
                                     debug=False)
            print(f"优化完成，最佳目标值: {best_val:.4f}")
            optimized = []
            for i in range(num_jammers):
                optimized.append({'x': best_pos[i*2], 'y': best_pos[i*2+1],
                                  'uuid': self.original_jammers_xy[i]['uuid']})
            return optimized
        except Exception as e:
            print(f"PSO优化失败: {e}，使用原始位置")
            return [{'x': j['x'], 'y': j['y'], 'uuid': j['uuid']}
                    for j in self.original_jammers_xy]

    # 绘图方法保持不变，省略部分代码以节省篇幅（实际使用时保留）
    def plot_original_deployment(self):
        fig, ax = plt.subplots(figsize=(12,10))
        ax.set_aspect('equal')
        circle = plt.Circle((0,0), self.radius_m, fill=False, edgecolor='blue', linestyle='--')
        ax.add_patch(circle)
        ax.scatter(0,0, color='red', s=100, marker='*', label='场景中心')
        # 要地
        guard_x = [g['x'] for g in self.guard_points_xy]
        guard_y = [g['y'] for g in self.guard_points_xy]
        ax.scatter(guard_x, guard_y, color='orange', s=80, marker='s', label='要地')
        for g in self.guard_points_xy:
            ax.annotate(g['showname'], (g['x'], g['y']), xytext=(5,5), textcoords='offset points', fontsize=9)
        # 路径
        colors = ['red','green','blue','purple','cyan','orange','brown','pink']
        for idx, (pk, pd) in enumerate(self.paths_xy.items()):
            pts = pd['points']
            if pts:
                px = [p['x'] for p in pts]
                py = [p['y'] for p in pts]
                col = colors[idx%len(colors)]
                ax.plot(px, py, color=col, linewidth=2, label=pd['showname'])
                ax.scatter(px, py, color=col, s=10, alpha=0.3)
        # 原始干扰机
        if self.original_jammers_xy:
            orig_x = [j['x'] for j in self.original_jammers_xy]
            orig_y = [j['y'] for j in self.original_jammers_xy]
            ax.scatter(orig_x, orig_y, color='green', s=60, marker='o', label='原始干扰机')
            for j in self.original_jammers_xy:
                circle = plt.Circle((j['x'], j['y']), self.jamming_radius_m,
                                    fill=False, edgecolor='green', linestyle=':', alpha=0.5)
                ax.add_patch(circle)
        ax.set_xlabel('东向距离 (米)')
        ax.set_ylabel('北向距离 (米)')
        ax.set_title('随机部署')
        ax.grid(True, linestyle='--', alpha=0.7)
        ax.legend(loc='upper right')
        ax.set_xlim(-self.radius_m*1.1, self.radius_m*1.1)
        ax.set_ylim(-self.radius_m*1.1, self.radius_m*1.1)
        plt.tight_layout()
        plt.show()
        # 计算指标
        orig_guard = self.calculate_guard_coverage(self.original_jammers_xy)
        orig_area = self.calculate_area_coverage(self.original_jammers_xy)
        orig_overlap = self.calculate_jammer_overlap(self.original_jammers_xy)
        return orig_guard, orig_area, orig_overlap

    def plot_optimized_deployment(self, optimized_jammers_xy):
        fig, ax = plt.subplots(figsize=(12,10))
        ax.set_aspect('equal')
        circle = plt.Circle((0,0), self.radius_m, fill=False, edgecolor='blue', linestyle='--')
        ax.add_patch(circle)
        ax.scatter(0,0, color='red', s=100, marker='*', label='场景中心')
        guard_x = [g['x'] for g in self.guard_points_xy]
        guard_y = [g['y'] for g in self.guard_points_xy]
        ax.scatter(guard_x, guard_y, color='orange', s=80, marker='s', label='要地')
        for g in self.guard_points_xy:
            ax.annotate(g['showname'], (g['x'], g['y']), xytext=(5,5), textcoords='offset points', fontsize=9)
        colors = ['red','green','blue','purple','cyan','orange','brown','pink']
        for idx, (pk, pd) in enumerate(self.paths_xy.items()):
            pts = pd['points']
            if pts:
                px = [p['x'] for p in pts]
                py = [p['y'] for p in pts]
                col = colors[idx%len(colors)]
                ax.plot(px, py, color=col, linewidth=2, label=pd['showname'])
                ax.scatter(px, py, color=col, s=10, alpha=0.3)
        if optimized_jammers_xy:
            opt_x = [j['x'] for j in optimized_jammers_xy]
            opt_y = [j['y'] for j in optimized_jammers_xy]
            ax.scatter(opt_x, opt_y, color='blue', s=60, marker='^', label='优化干扰机')
            for j in optimized_jammers_xy:
                circle = plt.Circle((j['x'], j['y']), self.jamming_radius_m,
                                    fill=False, edgecolor='blue', linestyle=':', alpha=0.5)
                ax.add_patch(circle)
        ax.set_xlabel('东向距离 (米)')
        ax.set_ylabel('北向距离 (米)')
        ax.set_title('优化部署')
        ax.grid(True, linestyle='--', alpha=0.7)
        ax.legend(loc='upper right')
        ax.set_xlim(-self.radius_m*1.1, self.radius_m*1.1)
        ax.set_ylim(-self.radius_m*1.1, self.radius_m*1.1)
        plt.tight_layout()
        plt.show()
        opt_guard = self.calculate_guard_coverage(optimized_jammers_xy)
        opt_area = self.calculate_area_coverage(optimized_jammers_xy)
        opt_overlap = self.calculate_jammer_overlap(optimized_jammers_xy)
        return opt_guard, opt_area, opt_overlap

    def plot_results(self, optimized_jammers_xy):
        print("\n绘制随机部署图...")
        orig_guard, orig_area, orig_overlap = self.plot_original_deployment()
        print("\n绘制优化部署图...")
        opt_guard, opt_area, opt_overlap = self.plot_optimized_deployment(optimized_jammers_xy)
        return orig_guard, orig_area, orig_overlap, opt_guard, opt_area, opt_overlap

    def save_results(self, optimized_jammers_xy,
                     orig_guard, orig_area, orig_overlap,
                     opt_guard, opt_area, opt_overlap):
        # 辅助函数：根据 uuid 生成友好的 showName
        def generate_show_name(uuid, orig_name):
            if orig_name:
                return orig_name
            match = re.search(r'(\d+)', uuid)
            if match:
                return f"雷达干扰机_{match.group(1)}"
            return f"雷达干扰机_{uuid[:8]}"

        # 转换优化后干扰机坐标回经纬度
        opt_jammers = []
        for jxy in optimized_jammers_xy:
            orig_jammer = next((j for j in self.original_jammers_xy if j['uuid'] == jxy['uuid']), None)
            orig_name = orig_jammer['original'].get('showName', '') if orig_jammer else ''
            show_name = generate_show_name(jxy['uuid'], orig_name)
            lon, lat = self.xy_to_latlon(jxy['x'], jxy['y'])
            opt_jammers.append({
                'uuid': jxy['uuid'],
                'showName': show_name,
                'latitude': round(lat, 6),
                'longitude': round(lon, 6),
                'altitude': 0
            })
        # 原始干扰机
        orig_jammers = []
        for j in self.original_jammers_xy:
            orig_name = j['original'].get('showName', '')
            show_name = generate_show_name(j['uuid'], orig_name)
            orig_jammers.append({
                'uuid': j['uuid'],
                'showName': show_name,
                'latitude': j['original']['latitude'],
                'longitude': j['original']['longitude'],
                'altitude': j['original']['altitude']
            })
        # 要地
        guards = [{'index': g['index'], 'latitude': g['original']['latitude'],
                   'longitude': g['original']['longitude'], 'altitude': g['original'].get('altitude',0),
                   'showname': g['showname']} for g in self.guard_points_xy]
        # 路径（原样）
        paths = {}
        for pk, pd in self.paths.items():
            pts = []
            for key in sorted([k for k in pd.keys() if k.startswith('point_')], key=lambda x: int(x.split('_')[1])):
                pts.append({'index': int(key.split('_')[1]),
                            'position': pd[key].get('position', ''),
                            'time': pd[key].get('time', 0)})
            paths[pk] = {'showname': pd.get('showName', ''), 'uuid': pd.get('uuid', ''), 'points': pts}
        result = {
            'scene': {
                'altitude': self.scene.get('altitude', '0'),
                'latitude': self.center_lat,
                'longitude': self.center_lon,
                'projectname': self.projectname,
                'radius': self.radius_m,
                'rangetype': self.scene.get('rangeType', 'circle'),
                'viewport': self.scene.get('viewPort', ''),
                'jammingradius': round(self.jamming_radius_m, 2)
            },
            'guardpoints': guards,
            'paths': paths,
            'targets': self.targets,
            'originaljammers': orig_jammers,
            'optimizedjammers': opt_jammers,
            'metrics': {
                'original': {'guardcoverage': round(orig_guard,4), 'areacoverage': round(orig_area,4), 'jammeroverlap': round(orig_overlap,4)},
                'optimized': {'guardcoverage': round(opt_guard,4), 'areacoverage': round(opt_area,4), 'jammeroverlap': round(opt_overlap,4)}
            },
            'type': 'optimizationresult'
        }
        os.makedirs('data', exist_ok=True)
        proj_dir = f'data/{self.projectname}'
        os.makedirs(proj_dir, exist_ok=True)
        out_file = f'{proj_dir}/radar_jammer_positions.json'
        with open(out_file, 'w', encoding='utf-8') as f:
            json.dump(result, f, ensure_ascii=False, indent=2)
        print(f"\n优化结果已保存到: {out_file}")

        # 打印对比表格
        print("\n" + "="*70)
        print("干扰机部署效果对比")
        print("="*70)
        print(f"{'指标':<15} {'随机部署':<15} {'优化部署':<15} {'变化幅度':<15}")
        print("-"*70)
        change_guard = opt_guard - orig_guard
        arrow = '↑' if change_guard>1e-6 else ('↓' if change_guard<-1e-6 else '—')
        print(f"{'要地覆盖率':<15} {orig_guard*100:>12.2f}%  {opt_guard*100:>12.2f}%  {arrow} {abs(change_guard*100):>6.2f}%")
        change_area = opt_area - orig_area
        arrow = '↑' if change_area>1e-6 else ('↓' if change_area<-1e-6 else '—')
        print(f"{'干扰覆盖率':<15} {orig_area*100:>12.2f}%  {opt_area*100:>12.2f}%  {arrow} {abs(change_area*100):>6.2f}%")
        change_overlap = opt_overlap - orig_overlap
        arrow = '↑' if change_overlap>1e-6 else ('↓' if change_overlap<-1e-6 else '—')
        print(f"{'干扰机重叠率':<15} {orig_overlap*100:>12.2f}%  {opt_overlap*100:>12.2f}%  {arrow} {abs(change_overlap*100):>6.2f}%")
        print("="*70)
        return out_file


def main():
    # 支持两种输入方式：文件或字典
    with open("干扰机位置优化_v2.json", 'r', encoding='utf-8') as f:
        config_dict = json.load(f)
    optimizer = RadarJammerOptimization(config=config_dict)

    optimized = optimizer.optimize_jammer_positions()
    orig_g, orig_a, orig_o, opt_g, opt_a, opt_o = optimizer.plot_results(optimized)
    optimizer.save_results(optimized, orig_g, orig_a, orig_o, opt_g, opt_a, opt_o)
    print("\n优化完成！")


if __name__ == "__main__":
    main()