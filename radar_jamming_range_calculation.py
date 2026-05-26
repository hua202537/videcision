"""
雷达干扰作用距离计算模块

优化说明:
1. 使用NumPy向量化计算，一次性计算所有距离点的干信比和成功率
2. 使用np.searchsorted和向量化查找代替循环
3. 使用numba JIT编译关键计算函数（可选）
"""

import numpy as np
import matplotlib.pyplot as plt
from dataclasses import dataclass, field
from typing import Tuple, Optional, Literal
import warnings

# 设置matplotlib支持中文
plt.rcParams['font.sans-serif'] = ['SimHei', 'Microsoft YaHei', 'DejaVu Sans']
plt.rcParams['axes.unicode_minus'] = False


@dataclass
class RadarParams:
    """雷达参数"""
    power: float = 0.0  # 峰值功率 (W)
    gain: float = 0.0  # 天线增益 (dB)
    frequency: float = 0.0  # 频率 (GHz)
    bandwidth: float = 0.0  # 带宽 (MHz)
    loss: float = 0.0  # 系统损耗 (dB)
    noise_figure: float = 0.0  # 噪声系数 (dB)
    rcs: float = 0.0  # 目标RCS (m²)
    Rt: float = 0.0  # 目标距离 (m)


@dataclass
class JammerRadarParams:
    """干扰机参数（雷达干扰）"""
    power: float = 0.0  # 干扰功率 (W)
    gain: float = 0.0  # 天线增益 (dB)
    bandwidth: float = 0.0  # 带宽 (MHz)
    loss: float = 0.0  # 系统损耗 (dB)
    deception_gain: float = 0.0  # 欺骗增益 (dB)
    DRFM_quality: float = 0.8  # DRFM质量因子 (0-1)


@dataclass
class CalculationOptions:
    """计算选项"""
    JSR_threshold: float = 10.0  # 干信比阈值 (dB)
    success_rate_threshold: float = 0.7  # 成功率阈值
    max_iterations: int = 100  # 距离点数
    distance_range: Tuple[float, float] = (1.0, 100.0)  # 距离搜索范围 (km)


@dataclass
class CalculationResults:
    """计算结果"""
    distances: np.ndarray = field(default_factory=lambda: np.array([]))
    JSR_values: np.ndarray = field(default_factory=lambda: np.array([]))
    success_rates: np.ndarray = field(default_factory=lambda: np.array([]))
    effective_distance: float = 0.0
    max_JSR: float = 0.0
    deception_effectiveness: Optional[np.ndarray] = None


def radar_jamming_range_calculation(
        radar_params: RadarParams,
        jammer_params: JammerRadarParams,
        jamming_type: Literal['noise', 'deception'],
        options: Optional[CalculationOptions] = None
) -> Tuple[float, CalculationResults]:
    """
    雷达干扰作用距离计算函数

    参数:
        radar_params: 雷达参数
        jammer_params: 干扰机参数
        jamming_type: 干扰类型 ('noise'压制/'deception'欺骗)
        options: 计算选项

    返回:
        R_jam: 干扰有效作用距离 (km)
        results: 详细计算结果
    """
    # 默认参数设置
    if options is None:
        options = CalculationOptions()

    # 常数定义
    c = 3e8  # 光速(m/s)

    # 参数提取和单位转换
    f0 = radar_params.frequency * 1e9  # 雷达频率(Hz)
    wavelength = c / f0  # 波长(m)
    B_radar = radar_params.bandwidth * 1e6  # 雷达带宽(Hz)
    B_jammer = jammer_params.bandwidth * 1e6  # 干扰带宽(Hz)

    # 根据干扰类型选择计算模型
    jamming_type = jamming_type.lower()
    if jamming_type == 'noise':
        R_jam, results = _calculate_noise_jamming_range(
            radar_params, jammer_params, wavelength, B_radar, B_jammer, options
        )
    elif jamming_type == 'deception':
        R_jam, results = _calculate_deception_jamming_range(
            radar_params, jammer_params, wavelength, B_radar, options
        )
    else:
        raise ValueError("不支持的干扰类型。请选择 'noise' 或 'deception'")

    # 显示计算结果（已禁用打印和绘图）
    # _display_results(results, jamming_type)  # 注释掉，不再输出

    return R_jam, results


def _calculate_noise_jamming_range(
        radar_params: RadarParams,
        jammer_params: JammerRadarParams,
        wavelength: float,
        B_radar: float,
        B_jammer: float,
        options: CalculationOptions
) -> Tuple[float, CalculationResults]:
    """
    压制干扰作用距离计算 (向量化版本)
    """
    # 雷达方程参数
    P_t = radar_params.power
    G_t = 10 ** (radar_params.gain / 10)
    sigma = radar_params.rcs
    Rt = radar_params.Rt
    L_r = 10 ** (radar_params.loss / 10)

    # 干扰机参数
    P_j = jammer_params.power
    G_j = 10 ** (jammer_params.gain / 10)
    L_j = 10 ** (jammer_params.loss / 10)

    # 向量化计算: 一次性生成所有距离点
    distances = np.linspace(options.distance_range[0], options.distance_range[1], options.max_iterations)
    R = distances * 1000  # 转换为米

    # 向量化计算干信比(J/S)
    numerator = P_j * G_j * 4 * np.pi * Rt ** 4 * B_radar
    denominator = P_t * G_t * sigma * R ** 2 * B_jammer * L_j / L_r
    JSR_linear = numerator / denominator
    JSR_values = 10 * np.log10(JSR_linear)

    # 向量化计算干扰成功率
    success_rates = _calculate_jamming_success_rate_vectorized(JSR_values, 'noise')

    # 向量化查找满足条件的距离
    valid_mask = (JSR_values >= options.JSR_threshold) & (success_rates >= options.success_rate_threshold)
    valid_indices = np.where(valid_mask)[0]

    if len(valid_indices) == 0:
        R_jam = 0.0
        warnings.warn('在当前参数下无法达到有效干扰距离')
    else:
        R_jam = distances[valid_indices[-1]]

    # 存储详细结果
    results = CalculationResults(
        distances=distances,
        JSR_values=JSR_values,
        success_rates=success_rates,
        effective_distance=R_jam,
        max_JSR=float(np.max(JSR_values))
    )

    return R_jam, results


def _calculate_deception_jamming_range(
        radar_params: RadarParams,
        jammer_params: JammerRadarParams,
        wavelength: float,
        B_radar: float,
        options: CalculationOptions
) -> Tuple[float, CalculationResults]:
    """
    欺骗干扰作用距离计算 (向量化版本)
    """
    # 雷达参数
    P_t = radar_params.power
    G_t = 10 ** (radar_params.gain / 10)
    sigma = radar_params.rcs
    Rt = radar_params.Rt
    L_r = 10 ** (radar_params.loss / 10)

    # 干扰机参数
    P_j = jammer_params.power
    G_j = 10 ** (jammer_params.gain / 10)
    L_j = 10 ** (jammer_params.loss / 10)
    deception_gain = 10 ** (jammer_params.deception_gain / 10)
    quality_factor = jammer_params.DRFM_quality

    # 向量化计算
    distances = np.linspace(options.distance_range[0], options.distance_range[1], options.max_iterations)
    R = distances * 1000  # 转换为米

    # 欺骗干扰的干信比计算（向量化）
    numerator = P_j * G_j * 4 * np.pi * Rt ** 4 * deception_gain
    denominator = P_t * G_t * sigma * R ** 2 * L_j / L_r
    JSR_linear = numerator / denominator
    JSR_values = 10 * np.log10(JSR_linear)

    # 欺骗干扰效果因子（向量化）
    range_factor = np.exp(-R / (50 * 1000))  # 50km参考距离
    deception_effectiveness = quality_factor * range_factor

    # 综合欺骗效果（向量化）
    effective_JSR = JSR_values + 10 * np.log10(deception_effectiveness)
    success_rates = _calculate_jamming_success_rate_vectorized(effective_JSR, 'deception')

    # 向量化查找满足条件的距离
    valid_indices = np.where(success_rates >= options.success_rate_threshold)[0]

    if len(valid_indices) == 0:
        R_jam = 0.0
        warnings.warn('在当前参数下无法达到有效欺骗距离')
    else:
        R_jam = distances[valid_indices[-1]]

    # 存储详细结果
    results = CalculationResults(
        distances=distances,
        JSR_values=JSR_values,
        success_rates=success_rates,
        effective_distance=R_jam,
        max_JSR=float(np.max(JSR_values)),
        deception_effectiveness=deception_effectiveness
    )

    return R_jam, results


def _calculate_jamming_success_rate_vectorized(
        JSR_db: np.ndarray,
        jamming_type: str
) -> np.ndarray:
    """
    向量化计算干扰成功率的经验模型
    使用np.piecewise或np.select进行向量化条件计算
    """
    success_rate = np.zeros_like(JSR_db, dtype=np.float64)

    if jamming_type.lower() == 'noise':
        # 压制干扰成功率模型 (向量化)
        conditions = [
            JSR_db < 0,
            (JSR_db >= 0) & (JSR_db < 10),
            (JSR_db >= 10) & (JSR_db < 20),
            JSR_db >= 20
        ]
        choices = [
            0.1,
            0.1 + 0.06 * JSR_db,
            0.7 + 0.02 * (JSR_db - 10),
            0.9 + 0.005 * (JSR_db - 20)
        ]
        success_rate = np.select(conditions, choices, default=0.0)

    elif jamming_type.lower() == 'deception':
        # 欺骗干扰成功率模型 (向量化)
        conditions = [
            JSR_db < -5,
            (JSR_db >= -5) & (JSR_db < 5),
            (JSR_db >= 5) & (JSR_db < 15),
            (JSR_db >= 15) & (JSR_db < 25),
            JSR_db >= 25
        ]
        choices = [
            0.05,
            0.05 + 0.045 * (JSR_db + 5),
            0.5 + 0.03 * (JSR_db - 5),
            0.8 + 0.01 * (JSR_db - 15),
            0.9 + 0.002 * (JSR_db - 25)
        ]
        success_rate = np.select(conditions, choices, default=0.0)

    # 限制在0-1之间
    return np.clip(success_rate, 0, 1)
