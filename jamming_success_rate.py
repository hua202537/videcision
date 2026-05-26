"""
干扰成功率计算模块
计算干扰成功率随距离变化的函数

优化说明:
1. 使用NumPy进行向量化计算，避免Python循环
2. 使用np.where和np.clip进行条件向量化
3. 支持标量和数组输入
"""

import numpy as np
from dataclasses import dataclass
from typing import Union, Literal

# 类型别名
ArrayLike = Union[float, np.ndarray]


@dataclass
class JammerParams:
    """干扰机参数"""
    tx_power: float = 0.0       # 发射功率 (dBW)
    jam_power: float = 0.0      # 干扰功率 (dBW)
    antenna_gain: float = 0.0   # 天线增益 (dBi)
    frequency: float = 0.0      # 频率 (Hz)
    bandwidth: float = 0.0      # 带宽 (Hz)
    modulation: str = 'QPSK'    # 调制方式


@dataclass
class TargetParams:
    """目标参数"""
    tx_power: float = 0.0       # 发射功率 (dBW)
    antenna_gain: float = 0.0   # 天线增益 (dBi)
    frequency: float = 0.0      # 频率 (Hz)
    bandwidth: float = 0.0      # 带宽 (Hz)
    modulation: str = 'QPSK'    # 调制方式
    range: float = 0.0          # 传输距离 (m)


def jamming_success_rate(
    jammer_params: JammerParams,
    target_params: TargetParams,
    distance: ArrayLike,
    jamming_type: Literal['deceptive', 'suppressive'] = 'suppressive'
) -> ArrayLike:
    """
    计算干扰成功率随距离变化
    
    参数:
        jammer_params: 干扰机参数
        target_params: 目标参数
        distance: 干扰距离 (km)，支持标量或数组
        jamming_type: 干扰类型 ('deceptive'欺骗式 / 'suppressive'压制式)
    
    返回:
        success_rate: 干扰成功率 (0-1)
    """
    # 确保distance是numpy数组以便向量化计算
    distance = np.asarray(distance)
    scalar_input = distance.ndim == 0
    distance = np.atleast_1d(distance)
    
    # 将距离转换为米
    distance_m = distance * 1000
    
    jamming_type = jamming_type.lower()
    if jamming_type == 'deceptive':
        success_rate = _deceptive_jamming_success(jammer_params, target_params, distance_m)
    elif jamming_type == 'suppressive':
        success_rate = _suppressive_jamming_success(jammer_params, target_params, distance_m)
    else:
        raise ValueError(f'不支持的干扰类型: {jamming_type}')
    
    # 如果输入是标量，返回标量
    if scalar_input:
        return float(success_rate[0])
    return success_rate


def _deceptive_jamming_success(
    jammer: JammerParams,
    target: TargetParams,
    distance: np.ndarray
) -> np.ndarray:
    """
    欺骗式干扰成功率计算 (向量化版本)
    """
    # 计算干信比 (JSR)
    JSR_db = _calculate_jsr(jammer, target, distance)
    
    # 计算欺骗信号质量因子
    quality_factor = _calculate_deception_quality(jammer, target)
    
    # 欺骗干扰成功率模型 (向量化计算)
    success_rate = np.zeros_like(JSR_db, dtype=np.float64)
    
    # JSR_db < -5
    mask1 = JSR_db < -5
    success_rate[mask1] = 0.05
    
    # -5 <= JSR_db < 5
    mask2 = (JSR_db >= -5) & (JSR_db < 5)
    success_rate[mask2] = 0.05 + 0.045 * (JSR_db[mask2] + 5)
    
    # 5 <= JSR_db < 15
    mask3 = (JSR_db >= 5) & (JSR_db < 15)
    success_rate[mask3] = 0.5 + 0.03 * (JSR_db[mask3] - 5)
    
    # 15 <= JSR_db < 25
    mask4 = (JSR_db >= 15) & (JSR_db < 25)
    success_rate[mask4] = 0.8 + 0.01 * (JSR_db[mask4] - 15)
    
    # JSR_db >= 25
    mask5 = JSR_db >= 25
    success_rate[mask5] = 0.9 + 0.002 * (JSR_db[mask5] - 25)
    
    return success_rate * quality_factor


def _suppressive_jamming_success(
    jammer: JammerParams,
    target: TargetParams,
    distance: np.ndarray
) -> np.ndarray:
    """
    压制式干扰成功率计算 (向量化版本)
    """
    # 计算干信比 (JSR)
    jsr = _calculate_jsr(jammer, target, distance)
    
    # 计算压制效果因子
    suppression_factor = _calculate_suppression_factor(jammer, target)
    
    # 压制成功率模型 (指数衰减)
    threshold = 10.0  # 干信比阈值 (dB)
    gamma = 0.3       # 衰减系数
    
    # 向量化条件计算
    success_rate = np.where(
        jsr > threshold,
        suppression_factor * (1 - gamma * np.exp(-gamma * (jsr - threshold))),
        suppression_factor * (1 - gamma) * (jsr / threshold)
    )
    
    # 限制在0-1范围内
    return np.clip(success_rate, 0, 1)


def _calculate_jsr(
    jammer: JammerParams,
    target: TargetParams,
    distance: np.ndarray
) -> np.ndarray:
    """
    计算干信比 (Jamming to Signal Ratio)
    向量化版本，distance可以是数组
    """
    c = 3e8  # 光速
    
    # 信号功率计算 (使用numpy的log10进行向量化)
    signal_power = (target.tx_power + target.antenna_gain - 
                   jammer.antenna_gain - 
                   20 * np.log10(target.range) - 
                   20 * np.log10(4 * np.pi * target.frequency / c))
    
    # 干扰功率计算 (向量化)
    jamming_power = (jammer.jam_power + jammer.antenna_gain - 
                    target.antenna_gain - 
                    20 * np.log10(distance) - 
                    20 * np.log10(4 * np.pi * jammer.frequency / c))
    
    # 干信比 (dB)
    return jamming_power - signal_power


def _calculate_deception_quality(jammer: JammerParams, target: TargetParams) -> float:
    """
    计算欺骗信号质量因子
    """
    # 频率匹配度
    freq_match = 1 - min(1, abs(jammer.frequency - target.frequency) / (target.bandwidth / 2))
    
    # 调制匹配度 (简化模型)
    if jammer.modulation.upper() == target.modulation.upper():
        mod_match = 2.5
    else:
        mod_match = 0.3
    
    # 功率控制精度
    power_accuracy = 0.8  # 假设值
    
    # 总体质量因子
    return 0.4 * freq_match + 0.3 * mod_match + 0.3 * power_accuracy


def _calculate_suppression_factor(jammer: JammerParams, target: TargetParams) -> float:
    """
    计算压制效果因子
    """
    # 带宽匹配度
    if jammer.bandwidth >= target.bandwidth:
        bw_match = 1.0
    else:
        bw_match = jammer.bandwidth / target.bandwidth
    
    # 频率覆盖度
    freq_overlap = _calculate_frequency_overlap(jammer, target)
    
    # 干扰机效能
    jammer_efficiency = min(1, jammer.jam_power / jammer.jam_power)  # 归一化
    
    # 总体压制因子
    return 0.4 * bw_match + 0.3 * freq_overlap + 0.3 * jammer_efficiency


def _calculate_frequency_overlap(jammer: JammerParams, target: TargetParams) -> float:
    """
    计算频率重叠度
    """
    jammer_low = jammer.frequency - jammer.bandwidth / 2
    jammer_high = jammer.frequency + jammer.bandwidth / 2
    target_low = target.frequency - target.bandwidth / 2
    target_high = target.frequency + target.bandwidth / 2
    
    # 计算重叠带宽
    overlap_bw = max(0, min(jammer_high, target_high) - max(jammer_low, target_low))
    
    # 重叠比例
    return overlap_bw / target.bandwidth
