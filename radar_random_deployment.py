import json
import math
import random
from pyproj import Transformer, CRS, Geod
import matplotlib.pyplot as plt
import numpy as np

# 导入雷达干扰计算模块
from radar_jamming_range_calculation import (
    RadarParams, JammerRadarParams, radar_jamming_range_calculation
)

# 配置matplotlib中文字体
plt.rcParams['font.sans-serif'] = ['SimHei', 'Microsoft YaHei', 'DejaVu Sans']
plt.rcParams['axes.unicode_minus'] = False


class JammerDeployment:
    def __init__(self, config):
        """
        初始化干扰机部署类

        Args:
            config: 配置文件路径（字符串）或字典
        """
        # 支持字典或文件路径
        if isinstance(config, str):
            with open(config, 'r', encoding='utf-8') as f:
                self.config = json.load(f)
        elif isinstance(config, dict):
            self.config = config
        else:
            raise TypeError("config 必须是文件路径字符串或字典")

        # 解析配置参数
        self.center_lat = float(self.config['scene']['latitude'])
        self.center_lon = float(self.config['scene']['longitude'])
        self.center_alt = float(self.config['scene']['altitude'])
        self.jammer_count = self.config['jammer']['radarJammingNumber']

        # 计算压制干扰有效距离作为部署半径
        self.radius_m = self._compute_jamming_radius()

        print(f"中心点: 纬度={self.center_lat}, 经度={self.center_lon}, 高度={self.center_alt}m")
        print(f"干扰机数量: {self.jammer_count}")
        print(f"部署半径（压制干扰有效距离）: {self.radius_m / 1000:.2f} km")

        # 创建WGS84大地测量对象，用于精确距离计算
        self.geod = Geod(ellps='WGS84')

        # 定义WGS84坐标系（经纬度）
        self.wgs84 = CRS.from_epsg(4326)  # WGS84 (lat, lon)

        # 根据半径选择适当的投影方法
        if self.radius_m <= 50000:  # 50km以下使用UTM投影
            self.projection_method = self.utm_projection
        elif self.radius_m <= 150000:  # 150km以下使用兰伯特等角圆锥投影
            self.projection_method = self.lambert_projection
        else:  # 更大范围使用墨卡托投影
            self.projection_method = self.mercator_projection

    def _compute_jamming_radius(self):
        """
        根据预设的雷达和干扰机参数计算压制干扰有效距离（70%成功率）
        返回：部署半径（米）
        """
        # 雷达参数（参考 test_RadJamSUCC_Range.py）
        radar = RadarParams(
            power=200,           # 200W
            gain=30,            # 30dB
            frequency=16,       # 16GHz
            bandwidth=100,      # 100MHz
            loss=4,             # 4dB
            noise_figure=3,     # 3dB
            rcs=1,              # 1m² RCS
            Rt=2000             # 2000m (2km)
        )

        # 干扰机参数（参考 test_RadJamSUCC_Range.py）
        jammer = JammerRadarParams(
            power=20,           # 20W
            gain=7,             # 7dB
            bandwidth=100,      # 100MHz
            loss=30,            # 30dB（含方向图损耗）
            deception_gain=6,   # 6dB欺骗增益
            DRFM_quality=0.85   # DRFM质量因子
        )

        # 计算压制干扰有效距离（km）
        R_jam_noise, _ = radar_jamming_range_calculation(radar, jammer, 'noise')

        if R_jam_noise <= 0:
            raise ValueError("压制干扰有效距离计算失败，请检查参数")

        # 转换为米
        return R_jam_noise * 1000

    def utm_projection(self):
        """UTM投影（适用于小范围，精度高）"""
        utm_zone = int((self.center_lon + 180) / 6) + 1
        hemisphere = 'north' if self.center_lat >= 0 else 'south'
        utm_crs = CRS.from_string(
            f"+proj=utm +zone={utm_zone} +{hemisphere} +ellps=WGS84 +datum=WGS84 +units=m +no_defs"
        )
        transformer = Transformer.from_crs(self.wgs84, utm_crs, always_xy=True)
        return transformer, utm_crs

    def lambert_projection(self):
        """兰伯特等角圆锥投影（适用于中等范围）"""
        lambert_crs = CRS.from_string(
            f"+proj=lcc +lat_1={self.center_lat - 1} +lat_2={self.center_lat + 1} "
            f"+lat_0={self.center_lat} +lon_0={self.center_lon} +x_0=0 +y_0=0 "
            "+ellps=WGS84 +datum=WGS84 +units=m +no_defs"
        )
        transformer = Transformer.from_crs(self.wgs84, lambert_crs, always_xy=True)
        return transformer, lambert_crs

    def mercator_projection(self):
        """墨卡托投影（适用于较大范围）"""
        mercator_crs = CRS.from_string(
            f"+proj=merc +lat_0={self.center_lat} +lon_0={self.center_lon} "
            "+k=1.0 +x_0=0 +y_0=0 +ellps=WGS84 +datum=WGS84 +units=m +no_defs"
        )
        transformer = Transformer.from_crs(self.wgs84, mercator_crs, always_xy=True)
        return transformer, mercator_crs

    def generate_random_points_in_circle(self, radius):
        """在圆形区域内生成随机点（均匀分布）"""
        points = []
        for _ in range(self.jammer_count):
            r = radius*0.7 * math.sqrt(random.random())
            theta = random.random() * 2 * math.pi
            x = r * math.cos(theta)
            y = r * math.sin(theta)
            points.append((x, y))
        return points

    def convert_to_geographic(self, points_xy, transformer, target_crs):
        """将平面坐标转换为地理坐标（经纬度）"""
        geographic_points = []
        center_x, center_y = transformer.transform(self.center_lon, self.center_lat)
        for x, y in points_xy:
            proj_x = center_x + x
            proj_y = center_y + y
            lon, lat = transformer.transform(proj_x, proj_y, direction='INVERSE')
            geographic_points.append((lon, lat))
        return geographic_points

    def generate_deployment(self):
        """生成干扰机部署位置"""
        transformer, target_crs = self.projection_method()
        points_xy = self.generate_random_points_in_circle(self.radius_m)
        points_geo = self.convert_to_geographic(points_xy, transformer, target_crs)

        deployment = []
        for i, (lon, lat) in enumerate(points_geo, 1):
            deployment.append({
                'id': i,
                'latitude': round(lat, 8),
                'longitude': round(lon, 8)
            })
        return deployment, points_xy

    def plot_deployment(self, points_xy, deployment):
        """
        绘制干扰机部署图（只显示平面部署图）
        """
        fig, ax = plt.subplots(1, 1, figsize=(8, 8))

        # 绘制圆形边界
        circle = plt.Circle((0, 0), self.radius_m, fill=False,
                            edgecolor='blue', linestyle='--', linewidth=1)
        ax.add_patch(circle)

        # 绘制中心点
        ax.scatter(0, 0, color='red', s=100, marker='*', label='中心点')

        # 绘制干扰机位置
        x_coords = [p[0] for p in points_xy]
        y_coords = [p[1] for p in points_xy]
        ax.scatter(x_coords, y_coords, color='green', s=50, marker='o', label='干扰机')

        # 标记干扰机编号
        for i, (x, y) in enumerate(points_xy, 1):
            ax.annotate(f'J{i}', (x, y), textcoords="offset points",
                        xytext=(5, 5), ha='center', fontsize=9)

        ax.set_xlabel('东向距离 (米)')
        ax.set_ylabel('北向距离 (米)')
        ax.set_title(f'干扰机部署平面图 (半径: {self.radius_m / 1000:.2f} km)')
        ax.grid(True, linestyle='--', alpha=0.7)
        ax.legend()
        ax.set_aspect('equal', adjustable='box')
        ax.set_xlim(-self.radius_m * 1.1, self.radius_m * 1.1)
        ax.set_ylim(-self.radius_m * 1.1, self.radius_m * 1.1)

        plt.tight_layout()
        plt.show()


def main():
    # 配置文件路径（可修改）
    config_file = "01_干扰机部署_v1.json"

    try:
        # 创建部署对象（支持文件路径）
        deployment_system = JammerDeployment(config_file)

        # 生成部署位置
        deployment_data, points_xy = deployment_system.generate_deployment()

        # 输出部署数据
        print("返回的干扰机部署位置数据：")
        print("[", end="")
        for i, item in enumerate(deployment_data):
            if i == 0:
                print(f"{{'id': {item['id']}, 'latitude': {item['latitude']}, 'longitude': {item['longitude']}}}", end="")
            else:
                print(f",\n {{'id': {item['id']}, 'latitude': {item['latitude']}, 'longitude': {item['longitude']}}}", end="")
        print("]")

        # 绘制部署图（仅平面图）
        deployment_system.plot_deployment(points_xy, deployment_data)

    except FileNotFoundError:
        print(f"错误: 找不到配置文件 '{config_file}'")
        print("请确保配置文件在当前目录下")
    except KeyError as e:
        print(f"错误: 配置文件中缺少必要的键: {e}")
    except Exception as e:
        print(f"错误: {e}")
        import traceback
        traceback.print_exc()


if __name__ == "__main__":
    main()