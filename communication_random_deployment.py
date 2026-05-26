import json
import math
import random
from pyproj import Transformer, CRS, Geod
import matplotlib.pyplot as plt
import numpy as np
from jamming_success_rate import JammerParams, TargetParams, jamming_success_rate

# 配置matplotlib中文字体（避免中文乱码）
plt.rcParams['font.sans-serif'] = ['SimHei', 'Microsoft YaHei', 'DejaVu Sans']
# 配置matplotlib负号显示（避免负号显示为方块）
plt.rcParams['axes.unicode_minus'] = False

# -------------------- 默认干扰机/目标参数（源自 test_ComJamSUCC_Range）--------------------
DEFAULT_JAMMER_PARAMS = {
    'tx_power': 20,          # dBW
    'jam_power': 40,         # dBW
    'antenna_gain': 12,      # dBi
    'frequency': 2.4e9,      # Hz
    'bandwidth': 200e6,      # Hz
    'modulation': 'QPSK'
}

DEFAULT_TARGET_PARAMS = {
    'tx_power': 10,          # dBW
    'antenna_gain': 15,      # dBi
    'frequency': 2.4e9,      # Hz
    'bandwidth': 10e6,       # Hz
    'modulation': 'QPSK',
    'range': 1000            # 传输距离 (m)
}

# -------------------- 辅助函数：二分搜索求半径 --------------------
def find_radius_for_success_rate(jammer, target, target_rate=0.7, tol=0.001, max_dist_km=200):
    """
    二分法求解满足压制式干扰成功率 ≥ target_rate 的最大距离（km）
    """
    low, high = 0.1, max_dist_km
    while high - low > tol:
        mid = (low + high) / 2
        rate = jamming_success_rate(jammer, target, mid, 'suppressive')
        if rate >= target_rate:
            low = mid
        else:
            high = mid
    return low  # 返回 km

# -------------------- 主类 --------------------
class JammerDeployment:
    def __init__(self, config):
        """
        初始化干扰机部署类
        Args:
            config: JSON配置文件路径 (str) 或 配置字典 (dict)
        """
        # 读取配置：支持文件路径或字典
        if isinstance(config, dict):
            self.config = config
        else:
            with open(config, 'r', encoding='utf-8') as f:
                self.config = json.load(f)

        # 解析配置参数（原有）
        self.center_lat = float(self.config['scene']['latitude'])
        self.center_lon = float(self.config['scene']['longitude'])
        self.center_alt = float(self.config['scene']['altitude'])
        self.jammer_count = self.config['jammer']['communicationJammerNumber']

        # 读取干扰机/目标参数（若无则用默认值）
        jammer_cfg = self.config.get('jammer_params', {})
        target_cfg = self.config.get('target_params', {})
        self.jammer_params = JammerParams(**{**DEFAULT_JAMMER_PARAMS, **jammer_cfg})
        self.target_params = TargetParams(**{**DEFAULT_TARGET_PARAMS, **target_cfg})

        # 计算干扰机有效半径（压制式干扰成功率 70%）
        jammer_radius_km = find_radius_for_success_rate(
            self.jammer_params, self.target_params, target_rate=0.7
        )
        self.radius_m = jammer_radius_km * 1000  # 转为米

        print(f"中心点: 纬度={self.center_lat}, 经度={self.center_lon}, 高度={self.center_alt}m")
        print(f"干扰机有效半径: {self.radius_m:.0f}米 (基于压制式干扰成功率≥70%)")
        print(f"干扰机数量: {self.jammer_count}")

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

    def utm_projection(self):
        """
        使用UTM投影（适用于小范围，精度高）
        返回：转换器对象和目标CRS
        """
        # 计算UTM区域
        utm_zone = int((self.center_lon + 180) / 6) + 1
        hemisphere = 'north' if self.center_lat >= 0 else 'south'

        # 定义UTM坐标系
        utm_crs = CRS.from_string(
            f"+proj=utm +zone={utm_zone} +{hemisphere} +ellps=WGS84 +datum=WGS84 +units=m +no_defs")

        # 创建转换器
        transformer = Transformer.from_crs(self.wgs84, utm_crs, always_xy=True)
        return transformer, utm_crs

    def lambert_projection(self):
        """
        使用兰伯特等角圆锥投影（适用于中等范围）
        返回：转换器对象和目标CRS
        """
        # 定义兰伯特等角圆锥投影
        lambert_crs = CRS.from_string(
            f"+proj=lcc +lat_1={self.center_lat - 1} +lat_2={self.center_lat + 1} "
            f"+lat_0={self.center_lat} +lon_0={self.center_lon} +x_0=0 +y_0=0 "
            "+ellps=WGS84 +datum=WGS84 +units=m +no_defs"
        )

        # 创建转换器
        transformer = Transformer.from_crs(self.wgs84, lambert_crs, always_xy=True)
        return transformer, lambert_crs

    def mercator_projection(self):
        """
        使用墨卡托投影（适用于较大范围）
        返回：转换器对象和目标CRS
        """
        # 定义墨卡托投影
        mercator_crs = CRS.from_string(
            f"+proj=merc +lat_0={self.center_lat} +lon_0={self.center_lon} "
            "+k=1.0 +x_0=0 +y_0=0 +ellps=WGS84 +datum=WGS84 +units=m +no_defs"
        )

        # 创建转换器
        transformer = Transformer.from_crs(self.wgs84, mercator_crs, always_xy=True)
        return transformer, mercator_crs

    def generate_random_points_in_circle(self, radius):
        """
        在圆形区域内生成随机点
        Args:
            radius: 半径（米）
        Returns:
            平面坐标列表 [(x1, y1), (x2, y2), ...]
        """
        points = []
        for _ in range(self.jammer_count):
            # 在圆形内均匀分布的随机点
            r = radius*0.7 * math.sqrt(random.random())  # 开方确保均匀分布
            theta = random.random() * 2 * math.pi
            x = r * math.cos(theta)
            y = r * math.sin(theta)
            points.append((x, y))
        return points

    def convert_to_geographic(self, points_xy, transformer, target_crs):
        """
        将平面坐标转换为地理坐标（经纬度）
        Args:
            points_xy: 平面坐标列表 [(x1, y1), (x2, y2), ...]
            transformer: 坐标转换器
            target_crs: 投影坐标系
        Returns:
            地理坐标列表 [(lon1, lat1), (lon2, lat2), ...]
        """
        geographic_points = []
        # 获取中心点在投影坐标系中的坐标
        center_x, center_y = transformer.transform(self.center_lon, self.center_lat)

        for x, y in points_xy:
            # 将相对坐标转换为绝对投影坐标
            proj_x = center_x + x
            proj_y = center_y + y
            # 反投影：从投影坐标转回经纬度
            lon, lat = transformer.transform(proj_x, proj_y, direction='INVERSE')
            geographic_points.append((lon, lat))

        return geographic_points

    def generate_deployment(self):
        """
        生成干扰机部署位置
        Returns:
            部署位置列表 [{'id': 1, 'latitude': ..., 'longitude': ...}, ...]
        """
        # 根据半径选择合适的投影方法
        transformer, target_crs = self.projection_method()

        # 生成平面坐标系中的随机点（使用计算出的半径）
        points_xy = self.generate_random_points_in_circle(self.radius_m)

        # 转换为地理坐标（经纬度）
        points_geo = self.convert_to_geographic(points_xy, transformer, target_crs)

        # 格式化输出
        deployment = []
        for i, (lon, lat) in enumerate(points_geo, 1):
            deployment.append({
                'id': i,
                'latitude': round(lat, 8),  # 保留8位小数，约1厘米精度
                'longitude': round(lon, 8)
            })

        return deployment, points_xy

    def plot_deployment(self, points_xy, deployment):
        """
        绘制干扰机部署平面图（仅第一张子图）
        Args:
            points_xy: 平面坐标点列表
            deployment: 部署位置数据
        """
        fig, ax = plt.subplots(1, 1, figsize=(8, 8))  # 改为单子图

        ax.set_aspect('equal', adjustable='box')

        # 绘制圆形边界（干扰机有效覆盖半径）
        circle = plt.Circle((0, 0), self.radius_m, fill=False,
                            edgecolor='blue', linestyle='--', linewidth=2, label='干扰有效边界')
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
        ax.set_title(f'干扰机部署平面图 (干扰半径: {self.radius_m/1000:.1f} km, 压制成功率≥70%)')
        ax.grid(True, linestyle='--', alpha=0.7)
        ax.legend()
        ax.set_xlim(-self.radius_m * 1.1, self.radius_m * 1.1)
        ax.set_ylim(-self.radius_m * 1.1, self.radius_m * 1.1)

        plt.tight_layout()
        plt.show()


def main():
    config_file = "01_干扰机部署_v1.json"
    try:
        # 直接使用文件路径，如果文件不存在会抛出 FileNotFoundError
        deployment_system = JammerDeployment(config_file)
    except FileNotFoundError:
        print(f"错误: 找不到配置文件 '{config_file}'")
        print("请确保配置文件在当前目录下")
        return

    # 生成部署位置
    deployment_data, points_xy = deployment_system.generate_deployment()

    # 输出部署数据（按照要求的多行格式）
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


if __name__ == "__main__":
    main()