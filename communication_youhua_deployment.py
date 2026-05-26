import json
import os
import math
import re  # 新增，用于从 UUID 提取数字
import numpy as np
from pyproj import Transformer, CRS
import matplotlib.pyplot as plt
from pyswarm import pso

# 导入现有模块
from jamming_success_rate import JammerParams, TargetParams, jamming_success_rate

# 配置 matplotlib 中文字体
plt.rcParams['font.sans-serif'] = ['SimHei', 'Microsoft YaHei', 'DejaVu Sans']
plt.rcParams['axes.unicode_minus'] = False


class JammerOptimization:
    def __init__(self, config_file=None, config=None):
        """
        初始化干扰机优化类

        Args:
            config_file: JSON配置文件路径
            config: 直接传入的配置字典
        """
        # 读取配置文件
        if config_file is not None:
            with open(config_file, 'r', encoding='utf-8') as f:
                self.config = json.load(f)
        elif config is not None:
            self.config = config
        else:
            raise ValueError("必须提供 config_file 或 config 参数")

        # 解析场景信息（输入为 WGS84）
        self.scene = self.config.get('scene', {})
        self.center_lat = float(self.scene.get('latitude', 40.0779))
        self.center_lon = float(self.scene.get('longitude', 116.554))
        self.center_alt = float(self.scene.get('altitude', 0))
        self.radius_m = float(self.scene.get('radius', 10000))
        self.projectname = self.scene.get('projectname', 'faeef')

        print(f"场景中心 (WGS84): 纬度={self.center_lat}, 经度={self.center_lon}, 高度={self.center_alt}m")
        print(f"场景半径: {self.radius_m}米, 项目名称: {self.projectname}")

        # 设置边界边距，避免干扰机部署在边界附近
        self.boundary_margin = min(500, self.radius_m * 0.1)
        self.effective_radius = self.radius_m - self.boundary_margin

        # 解析要地信息（输入为 WGS84）
        self.guard_points = self.config.get('guardPoints', [])
        print(f"要地数量: {len(self.guard_points)}")

        # 解析干扰机信息（只取 jammerType 为 8 的通信导航干扰机）
        self.original_jammers = []
        self.jammer_configs = {}

        jammer_dict = self.config.get('jammer', {})
        for jammer_key, jammer_data in jammer_dict.items():
            if 'communicationsJammerInfo' in jammer_data:
                comm_info = jammer_data['communicationsJammerInfo']
                if comm_info.get('jammerType', 0) == 8:
                    sensor_info = jammer_data['sensorInfo']
                    jammer_info = {
                        'uuid': sensor_info.get('uuid', ''),
                        'index': sensor_info.get('index', 0),
                        'latitude': float(sensor_info.get('latitude', 0)),
                        'longitude': float(sensor_info.get('longitude', 0)),
                        'altitude': float(sensor_info.get('altitude', 0)),
                        'showName': sensor_info.get('showName', ''),
                        'jammerType': 8,
                        'config': comm_info
                    }
                    self.original_jammers.append(jammer_info)
                    self.jammer_configs[jammer_key] = comm_info

        print(f"通信导航干扰机数量: {len(self.original_jammers)}")

        # 解析飞行路径（输入为 WGS84，仅用于绘图）
        self.paths = self.config.get('paths', {})
        print(f"飞行路径数量: {len(self.paths)}")

        # 解析目标信息（输入为 WGS84，仅用于保存）
        self.targets = self.config.get('targets', [])
        print(f"目标数量: {len(self.targets)}")

        # 创建 WGS84 大地测量对象
        self.wgs84 = CRS.from_epsg(4326)

        # 根据半径选择投影方法
        if self.radius_m <= 50000:
            self.projection_method = self.utm_projection
        elif self.radius_m <= 150000:
            self.projection_method = self.lambert_projection
        else:
            self.projection_method = self.mercator_projection

        # 初始化坐标转换器（基于 WGS84 中心点）
        self.transformer, self.target_crs = self.projection_method()

        # 获取中心点在投影坐标系中的坐标
        self.center_x, self.center_y = self.transformer.transform(self.center_lon, self.center_lat)

        # 存储转换后的坐标（平面坐标）
        self.guard_points_xy = []
        self.original_jammers_xy = []
        self.paths_xy = {}
        self.targets_xy = []

        # 转换所有坐标到平面坐标系
        self.convert_all_coordinates()

        # 计算干扰半径
        self.calculate_jamming_radius()

        # 预生成用于面积覆盖率的网格（优化关键点）
        self._precompute_area_grid()

    def utm_projection(self):
        """使用 UTM 投影（基于 WGS84 中心点）"""
        utm_zone = int((self.center_lon + 180) / 6) + 1
        hemisphere = 'north' if self.center_lat >= 0 else 'south'
        utm_crs = CRS.from_string(
            f"+proj=utm +zone={utm_zone} +{hemisphere} +ellps=WGS84 +datum=WGS84 +units=m +no_defs"
        )
        transformer = Transformer.from_crs(self.wgs84, utm_crs, always_xy=True)
        return transformer, utm_crs

    def lambert_projection(self):
        """使用兰伯特等角圆锥投影（基于 WGS84 中心点）"""
        lambert_crs = CRS.from_string(
            f"+proj=lcc +lat_1={self.center_lat - 1} +lat_2={self.center_lat + 1} "
            f"+lat_0={self.center_lat} +lon_0={self.center_lon} +x_0=0 +y_0=0 "
            "+ellps=WGS84 +datum=WGS84 +units=m +no_defs"
        )
        transformer = Transformer.from_crs(self.wgs84, lambert_crs, always_xy=True)
        return transformer, lambert_crs

    def mercator_projection(self):
        """使用墨卡托投影（基于 WGS84 中心点）"""
        mercator_crs = CRS.from_string(
            f"+proj=merc +lat_0={self.center_lat} +lon_0={self.center_lon} "
            "+k=1.0 +x_0=0 +y_0=0 +ellps=WGS84 +datum=WGS84 +units=m +no_defs"
        )
        transformer = Transformer.from_crs(self.wgs84, mercator_crs, always_xy=True)
        return transformer, mercator_crs

    def latlon_to_xy(self, lon, lat):
        """WGS84 经纬度转换为平面坐标"""
        proj_x, proj_y = self.transformer.transform(lon, lat)
        x = proj_x - self.center_x
        y = proj_y - self.center_y
        return x, y

    def xy_to_latlon(self, x, y):
        """平面坐标转换为 WGS84 经纬度"""
        proj_x = self.center_x + x
        proj_y = self.center_y + y
        lon, lat = self.transformer.transform(proj_x, proj_y, direction='INVERSE')
        return lon, lat

    def convert_all_coordinates(self):
        """转换所有坐标到平面坐标系（基于 WGS84）"""
        # 转换要地坐标
        for guard in self.guard_points:
            lat = float(guard.get('latitude', 0))
            lon = float(guard.get('longitude', 0))
            x, y = self.latlon_to_xy(lon, lat)
            self.guard_points_xy.append({
                'index': guard.get('index', 0),
                'x': x,
                'y': y,
                'showName': guard.get('showName', ''),
                'original': guard,
                'lon': lon,
                'lat': lat
            })

        # 转换原始干扰机坐标
        for jammer in self.original_jammers:
            lat = jammer['latitude']
            lon = jammer['longitude']
            x, y = self.latlon_to_xy(lon, lat)
            self.original_jammers_xy.append({
                'uuid': jammer['uuid'],
                'x': x,
                'y': y,
                'original': jammer,
                'lon': lon,
                'lat': lat
            })

        # 转换飞行路径坐标（仅用于绘图）
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
                            'x': x,
                            'y': y,
                            'time': point_data.get('time', 0),
                            'original': point_data,
                            'lon': lon,
                            'lat': lat
                        })
            if path_points:
                self.paths_xy[path_key] = {
                    'showName': path_data.get('showName', ''),
                    'uuid': path_data.get('uuid', ''),
                    'points': path_points
                }

        # 转换目标初始坐标（仅用于保存）
        for target in self.targets:
            base_info = target.get('baseInfo', {})
            lat = float(base_info.get('latitude', 0))
            lon = float(base_info.get('longitude', 0))
            x, y = self.latlon_to_xy(lon, lat)
            self.targets_xy.append({
                'index': base_info.get('index', 0),
                'x': x,
                'y': y,
                'showName': base_info.get('showName', ''),
                'original': target,
                'lon': lon,
                'lat': lat
            })

    def calculate_jamming_radius(self):
        """计算干扰半径（基于压制干扰成功率 70%）"""
        # 使用固定参数计算干扰半径（与 test_ComJamSUCC_Range.py 一致）
        jammer_params = JammerParams(
            tx_power=20,       # dBW
            jam_power=40,      # dBW
            antenna_gain=12,   # dBi
            frequency=2.4e9,   # Hz
            bandwidth=200e6,   # Hz
            modulation='QPSK'
        )
        target_params = TargetParams(
            tx_power=10,       # dBW
            antenna_gain=15,   # dBi
            frequency=2.4e9,   # Hz
            bandwidth=10e6,    # Hz
            modulation='QPSK',
            range=1000         # 传输距离 (m)
        )

        # 计算 1-100 km 范围内的压制干扰成功率
        distances_km = np.arange(1, 100, 0.1)
        success_rates = jamming_success_rate(
            jammer_params, target_params, distances_km, jamming_type='suppressive'
        )

        # 找到成功率首次低于 70% 的临界点，取之前最大距离作为干扰半径
        threshold = 0.70
        valid_indices = np.where(success_rates >= threshold)[0]
        if len(valid_indices) > 0:
            max_distance_km = distances_km[valid_indices[-1]]
            self.jamming_radius_m = max_distance_km * 1000
        else:
            self.jamming_radius_m = 10000  # 默认 10 km

        print(f"压制干扰成功率阈值: {threshold:.0%}, 对应干扰半径: {self.jamming_radius_m / 1000:.1f} km")

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

    def calculate_guard_coverage(self, jammers_xy):
        """计算要地覆盖率（平面坐标）"""
        if not self.guard_points_xy or not jammers_xy:
            return 0.0
        covered = 0
        for guard in self.guard_points_xy:
            for jammer in jammers_xy:
                dx = guard['x'] - jammer['x']
                dy = guard['y'] - jammer['y']
                if math.hypot(dx, dy) <= self.jamming_radius_m:
                    covered += 1
                    break
        return covered / len(self.guard_points_xy)

    def calculate_jammer_coverage(self, jammers_xy):
        """
        计算干扰机覆盖面积占场景总面积的比例（向量化加速版）
        返回覆盖率（0~1）
        """
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

    def calculate_jammer_overlap(self, jammers_xy):
        """计算干扰机重叠率（平均重叠面积比例），使用向量化加速"""
        n = len(jammers_xy)
        if n <= 1:
            return 0.0
        r = self.jamming_radius_m
        total_ratio = 0.0
        count = 0
        # 提取坐标数组
        coords = np.array([[j['x'], j['y']] for j in jammers_xy])
        for i in range(n):
            for j in range(i + 1, n):
                d = math.hypot(coords[i, 0] - coords[j, 0], coords[i, 1] - coords[j, 1])
                if d >= 2 * r:
                    ratio = 0.0
                elif d <= 0:
                    ratio = 1.0
                else:
                    # 两个圆重叠面积公式
                    part1 = r**2 * math.acos((d**2 + r**2 - r**2) / (2 * d * r))
                    part2 = r**2 * math.acos((d**2 + r**2 - r**2) / (2 * d * r))
                    part3 = 0.5 * math.sqrt((-d + 2 * r) * (d + 2 * r) * (d) * (d))
                    overlap_area = part1 + part2 - part3
                    ratio = overlap_area / (math.pi * r**2)
                total_ratio += ratio
                count += 1
        return total_ratio / count if count > 0 else 0.0

    def objective_function(self, positions):
        """
        PSO 目标函数：最小化
        权重：要地覆盖率 0.4，干扰覆盖率 0.4，干扰机重叠率 0.2
        """
        num_jammers = len(self.original_jammers_xy)
        jammers_xy = []
        for i in range(num_jammers):
            x = positions[i * 2]
            y = positions[i * 2 + 1]
            if math.hypot(x, y) > self.effective_radius:
                return 1e6   # 超出有效部署区域，惩罚
            jammers_xy.append({'x': x, 'y': y})

        guard_cov = self.calculate_guard_coverage(jammers_xy)      # 要地覆盖率
        overlap = self.calculate_jammer_overlap(jammers_xy)        # 重叠率
        jammer_cov = self.calculate_jammer_coverage(jammers_xy)    # 干扰覆盖率

        # 目标值 = 0.4*(1-guard_cov) + 0.2*overlap + 0.4*(1-jammer_cov)
        return 0.4 * (1 - guard_cov) + 0.2 * overlap + 0.4 * (1 - jammer_cov)

    def optimize_jammer_positions(self):
        """使用 PSO 算法优化干扰机位置"""
        print("\n开始优化干扰机位置...")
        num_jammers = len(self.original_jammers_xy)
        if num_jammers == 0:
            print("没有通信导航干扰机需要优化")
            return []

        n_particles = 20   # 减少粒子数可加快速度
        n_iterations = 80  # 减少迭代次数
        lower_bounds = [-self.radius_m] * (num_jammers * 2)
        upper_bounds = [self.radius_m] * (num_jammers * 2)

        # 初始位置使用原始位置
        initial_positions = []
        for jammer in self.original_jammers_xy:
            initial_positions.append(jammer['x'])
            initial_positions.append(jammer['y'])

        print(f"PSO 参数: {n_particles}个粒子, {n_iterations}次迭代")
        try:
            best_position, best_value = pso(
                self.objective_function,
                lower_bounds,
                upper_bounds,
                swarmsize=n_particles,
                maxiter=n_iterations,
                debug=False
            )
            print(f"优化完成，最佳目标值: {best_value:.4f}")

            optimized_jammers_xy = []
            for i in range(num_jammers):
                optimized_jammers_xy.append({
                    'x': best_position[i * 2],
                    'y': best_position[i * 2 + 1],
                    'uuid': self.original_jammers_xy[i]['uuid']
                })
            return optimized_jammers_xy
        except Exception as e:
            print(f"PSO优化失败: {e}")
            print("使用原始位置作为优化结果")
            return [
                {'x': j['x'], 'y': j['y'], 'uuid': j['uuid']}
                for j in self.original_jammers_xy
            ]

    def plot_original_deployment(self):
        """绘制随机部署图（平面坐标），同时计算并返回指标"""
        fig, ax = plt.subplots(figsize=(12, 10))
        ax.set_aspect('equal')
        circle = plt.Circle((0, 0), self.radius_m, fill=False,
                            edgecolor='blue', linestyle='--', linewidth=1)
        ax.add_patch(circle)
        ax.scatter(0, 0, color='red', s=100, marker='*', label='场景中心')

        # 要地
        guard_x = [g['x'] for g in self.guard_points_xy]
        guard_y = [g['y'] for g in self.guard_points_xy]
        ax.scatter(guard_x, guard_y, color='orange', s=80, marker='s', label='要地')
        for g in self.guard_points_xy:
            ax.annotate(g['showName'], (g['x'], g['y']),
                        textcoords="offset points", xytext=(5, 5), ha='center', fontsize=9)

        # 飞行路径
        colors = ['red', 'green', 'blue', 'purple', 'cyan', 'orange', 'brown', 'pink']
        color_idx = 0
        for path_data in self.paths_xy.values():
            points = path_data['points']
            if points:
                x = [p['x'] for p in points]
                y = [p['y'] for p in points]
                color = colors[color_idx % len(colors)]
                ax.plot(x, y, color=color, linewidth=2, label=path_data['showName'])
                ax.scatter(x, y, color=color, s=10, alpha=0.3)
                color_idx += 1

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

        handles, labels = ax.get_legend_handles_labels()
        if len(labels) > 8:
            from matplotlib.patches import Patch
            handles = handles[:7]
            labels = labels[:7]
            handles.append(Patch(color='gray', alpha=0.5))
            labels.append(f'其他{len(self.paths_xy) - 7}条路径')
        ax.legend(handles, labels, loc='upper right')
        ax.set_xlim(-self.radius_m * 1.1, self.radius_m * 1.1)
        ax.set_ylim(-self.radius_m * 1.1, self.radius_m * 1.1)
        plt.tight_layout()
        plt.show()

        # 计算各项指标
        orig_guard_cov = self.calculate_guard_coverage(self.original_jammers_xy)
        orig_overlap = self.calculate_jammer_overlap(self.original_jammers_xy)
        orig_jammer_cov = self.calculate_jammer_coverage(self.original_jammers_xy)

        return orig_guard_cov, orig_overlap, orig_jammer_cov

    def plot_optimized_deployment(self, optimized_jammers_xy):
        """绘制优化部署图（平面坐标），同时计算并返回指标"""
        fig, ax = plt.subplots(figsize=(12, 10))
        ax.set_aspect('equal')
        circle = plt.Circle((0, 0), self.radius_m, fill=False,
                            edgecolor='blue', linestyle='--', linewidth=1)
        ax.add_patch(circle)
        ax.scatter(0, 0, color='red', s=100, marker='*', label='场景中心')

        # 要地
        guard_x = [g['x'] for g in self.guard_points_xy]
        guard_y = [g['y'] for g in self.guard_points_xy]
        ax.scatter(guard_x, guard_y, color='orange', s=80, marker='s', label='要地')
        for g in self.guard_points_xy:
            ax.annotate(g['showName'], (g['x'], g['y']),
                        textcoords="offset points", xytext=(5, 5), ha='center', fontsize=9)

        # 飞行路径
        colors = ['red', 'green', 'blue', 'purple', 'cyan', 'orange', 'brown', 'pink']
        color_idx = 0
        for path_data in self.paths_xy.values():
            points = path_data['points']
            if points:
                x = [p['x'] for p in points]
                y = [p['y'] for p in points]
                color = colors[color_idx % len(colors)]
                ax.plot(x, y, color=color, linewidth=2, label=path_data['showName'])
                ax.scatter(x, y, color=color, s=10, alpha=0.3)
                color_idx += 1

        # 优化后干扰机
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

        handles, labels = ax.get_legend_handles_labels()
        if len(labels) > 8:
            from matplotlib.patches import Patch
            handles = handles[:7]
            labels = labels[:7]
            handles.append(Patch(color='gray', alpha=0.5))
            labels.append(f'其他{len(self.paths_xy) - 7}条路径')
        ax.legend(handles, labels, loc='upper right')
        ax.set_xlim(-self.radius_m * 1.1, self.radius_m * 1.1)
        ax.set_ylim(-self.radius_m * 1.1, self.radius_m * 1.1)
        plt.tight_layout()
        plt.show()

        # 计算各项指标
        opt_guard_cov = self.calculate_guard_coverage(optimized_jammers_xy)
        opt_overlap = self.calculate_jammer_overlap(optimized_jammers_xy)
        opt_jammer_cov = self.calculate_jammer_coverage(optimized_jammers_xy)

        return opt_guard_cov, opt_overlap, opt_jammer_cov

    def plot_results(self, optimized_jammers_xy):
        """绘制两张独立的部署图，并返回所有指标"""
        print("\n绘制随机部署图...")
        orig_guard_cov, orig_overlap, orig_jammer_cov = self.plot_original_deployment()
        print("\n绘制优化部署图...")
        opt_guard_cov, opt_overlap, opt_jammer_cov = self.plot_optimized_deployment(optimized_jammers_xy)
        return (orig_guard_cov, orig_overlap, orig_jammer_cov,
                opt_guard_cov, opt_overlap, opt_jammer_cov)

    def save_results(self, optimized_jammers_xy,
                     orig_guard_cov, orig_overlap, orig_jammer_cov,
                     opt_guard_cov, opt_overlap, opt_jammer_cov):
        """保存优化结果到 JSON 文件（输出坐标使用 WGS84），并打印表格形式的指标对比"""
        # 辅助函数：根据 uuid 生成友好的 showName
        def generate_show_name(uuid, orig_name):
            if orig_name:
                return orig_name
            # 尝试从 uuid 中提取数字
            match = re.search(r'(\d+)', uuid)
            if match:
                return f"通信导航干扰机_{match.group(1)}"
            # 回退：使用 uuid 的前8位
            return f"通信导航干扰机_{uuid[:8]}"

        # 优化后的干扰机位置（WGS84）
        optimized_jammers = []
        for jammer_xy in optimized_jammers_xy:
            orig_jammer = next((j for j in self.original_jammers if j['uuid'] == jammer_xy['uuid']), None)
            orig_name = orig_jammer.get('showName', '') if orig_jammer else ''
            show_name = generate_show_name(jammer_xy['uuid'], orig_name)
            lon, lat = self.xy_to_latlon(jammer_xy['x'], jammer_xy['y'])
            optimized_jammers.append({
                'uuid': jammer_xy['uuid'],
                'showName': show_name,
                'latitude': round(lat, 6),
                'longitude': round(lon, 6),
                'altitude': 0
            })

        # 原始干扰机（直接使用原始数据，已是 WGS84）
        original_jammers = []
        for jammer in self.original_jammers:
            orig_name = jammer.get('showName', '')
            show_name = generate_show_name(jammer['uuid'], orig_name)
            original_jammers.append({
                'uuid': jammer['uuid'],
                'showName': show_name,
                'latitude': jammer['latitude'],
                'longitude': jammer['longitude'],
                'altitude': jammer['altitude']
            })

        # 要地（直接使用原始数据）
        guard_points = []
        for guard in self.guard_points:
            guard_points.append({
                'index': guard.get('index', 0),
                'latitude': guard.get('latitude', 0),
                'longitude': guard.get('longitude', 0),
                'altitude': guard.get('altitude', 0),
                'showName': guard.get('showName', '')
            })

        # 飞行路径（直接使用原始数据）
        paths = {}
        for path_key, path_data in self.paths.items():
            points = []
            point_keys = [k for k in path_data.keys() if k.startswith('point_')]
            point_keys_sorted = sorted(point_keys, key=lambda x: int(x.split('_')[1]))
            for point_key in point_keys_sorted:
                point_data = path_data[point_key]
                points.append({
                    'index': int(point_key.split('_')[1]),
                    'position': point_data.get('position', ''),
                    'time': point_data.get('time', 0)
                })
            paths[path_key] = {
                'showName': path_data.get('showName', ''),
                'uuid': path_data.get('uuid', ''),
                'points': points
            }

        result_data = {
            'scene': {
                'altitude': self.scene.get('altitude', '0'),
                'latitude': self.scene.get('latitude', '40.0779'),
                'longitude': self.scene.get('longitude', '116.554'),
                'projectname': self.projectname,
                'radius': self.scene.get('radius', '10000'),
                'rangeType': self.scene.get('rangeType', 'circle'),
                'viewPort': self.scene.get('viewPort', ''),
                'jammingRadius': round(self.jamming_radius_m, 2)
            },
            'guardpoints': guard_points,
            'paths': paths,
            'targets': self.targets,       # 原始目标数据
            'originalJammers': original_jammers,
            'optimizedJammers': optimized_jammers,
            'metrics': {
                'original': {
                    'guardCoverage': round(orig_guard_cov, 4),
                    'jammerOverlap': round(orig_overlap, 4),
                    'jammerCoverage': round(orig_jammer_cov, 4)
                },
                'optimized': {
                    'guardCoverage': round(opt_guard_cov, 4),
                    'jammerOverlap': round(opt_overlap, 4),
                    'jammerCoverage': round(opt_jammer_cov, 4)
                }
            },
            'type': 'optimizationResult'
        }

        # 保存文件
        if not os.path.exists('data'):
            os.makedirs('data')
        project_dir = f'data/{self.projectname}'
        if not os.path.exists(project_dir):
            os.makedirs(project_dir)
        output_file = f'{project_dir}/communication_jammer_positions.json'
        with open(output_file, 'w', encoding='utf-8') as f:
            json.dump(result_data, f, ensure_ascii=False, indent=2)

        print(f"\n优化结果已保存到: {output_file}")

        # 计算差值（优化 - 随机）
        guard_diff = opt_guard_cov - orig_guard_cov
        overlap_diff = opt_overlap - orig_overlap
        coverage_diff = opt_jammer_cov - orig_jammer_cov

        # 构建表格
        print("\n" + "=" * 60)
        print("                      性能指标对比表")
        print("=" * 60)
        print(f"{'指标':<20} {'随机部署':<15} {'优化部署':<15} {'提升'}")
        print("-" * 60)

        # 要地覆盖率
        print(f"{'要地覆盖率':<20} {orig_guard_cov*100:>8.2f}%      {opt_guard_cov*100:>8.2f}%      ", end="")
        if guard_diff > 0:
            print(f"↑ {guard_diff*100:>6.2f}%")
        elif guard_diff < 0:
            print(f"↓ {abs(guard_diff)*100:>6.2f}%")
        else:
            print("—")

        # 干扰机重叠率
        print(f"{'干扰机重叠率':<20} {orig_overlap*100:>8.2f}%      {opt_overlap*100:>8.2f}%      ", end="")
        if overlap_diff > 0:
            print(f"↑ {overlap_diff*100:>6.2f}%")
        elif overlap_diff < 0:
            print(f"↓ {abs(overlap_diff)*100:>6.2f}%")
        else:
            print("—")

        # 干扰覆盖率
        print(f"{'干扰覆盖率':<20} {orig_jammer_cov*100:>8.2f}%      {opt_jammer_cov*100:>8.2f}%      ", end="")
        if coverage_diff > 0:
            print(f"↑ {coverage_diff*100:>6.2f}%")
        elif coverage_diff < 0:
            print(f"↓ {abs(coverage_diff)*100:>6.2f}%")
        else:
            print("—")

        print("=" * 60)

        return output_file


def main():
    """主函数"""
    # 支持两种输入方式：文件或字典
    # 示例1：从文件读取
    # optimizer = JammerOptimization(config_file="干扰机位置优化_v2.json")
    # 示例2：直接传入字典（支持字典输入）
    with open("干扰机位置优化_v2.json", 'r', encoding='utf-8') as f:
        config_dict = json.load(f)
    optimizer = JammerOptimization(config=config_dict)  # 字典输入

    optimized_jammers_xy = optimizer.optimize_jammer_positions()

    (orig_guard_cov, orig_overlap, orig_jammer_cov,
     opt_guard_cov, opt_overlap, opt_jammer_cov) = optimizer.plot_results(optimized_jammers_xy)

    optimizer.save_results(optimized_jammers_xy,
                           orig_guard_cov, orig_overlap, orig_jammer_cov,
                           opt_guard_cov, opt_overlap, opt_jammer_cov)

    print("\n优化完成！")


if __name__ == "__main__":
    main()