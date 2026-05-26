"""
通信干扰成功率测试脚本
对应MATLAB文件: test_ComJamSUCC_Range.m

优化说明:
1. 使用向量化计算替代循环，大幅提升计算速度
2. 使用NumPy数组操作，避免Python原生循环
"""

import numpy as np
import matplotlib.pyplot as plt
from jamming_success_rate import JammerParams, TargetParams, jamming_success_rate

# 设置matplotlib支持中文
plt.rcParams['font.sans-serif'] = ['SimHei', 'Microsoft YaHei', 'DejaVu Sans']
plt.rcParams['axes.unicode_minus'] = False


def main():
    """主测试函数"""
    # 定义干扰机参数
    jammer = JammerParams(
        tx_power=20,           # dBW
        jam_power=40,          # dBW
        antenna_gain=12,       # dBi
        frequency=2.4e9,       # Hz
        bandwidth=200e6,       # Hz
        modulation='QPSK'
    )
    
    # 定义目标参数
    target = TargetParams(
        tx_power=10,           # dB
        antenna_gain=15,       # dBi
        frequency=2.4e9,       # Hz
        bandwidth=10e6,        # Hz
        modulation='QPSK',
        range=1000             # 传输距离 (m)
    )
    
    # 计算不同距离的干扰成功率 (1-50km)
    # 使用向量化计算 - 直接传递数组，避免循环
    distances = np.arange(1, 50.1, 0.1)
    
    # 向量化计算 - 一次性计算所有距离的成功率
    deceptive_rates = jamming_success_rate(jammer, target, distances, 'deceptive')
    suppressive_rates = jamming_success_rate(jammer, target, distances, 'suppressive')
    
    # 绘制结果
    plt.figure(figsize=(10, 6))
    plt.plot(distances, deceptive_rates, 'b-', linewidth=2, label='欺骗式通信干扰')
    plt.plot(distances, suppressive_rates, 'r-', linewidth=2, label='压制式通信干扰')
    
    plt.xlabel('干扰距离 (km)')
    plt.ylabel('干扰成功率')
    plt.title('干扰成功率随距离的变化关系')
    plt.legend()
    plt.grid(True)
    plt.ylim([0, 1])
    
    plt.tight_layout()
    plt.show()
    
    # 输出一些关键数据用于验证
    print("=== 通信干扰成功率计算结果 ===")
    print(f"距离范围: {distances[0]:.1f} - {distances[-1]:.1f} km")
    print(f"\n欺骗式干扰:")
    print(f"  最大成功率: {np.max(deceptive_rates):.4f}")
    print(f"  最小成功率: {np.min(deceptive_rates):.4f}")
    print(f"  距离1km时成功率: {deceptive_rates[0]:.4f}")
    print(f"  距离25km时成功率: {deceptive_rates[240]:.4f}")
    print(f"  距离50km时成功率: {deceptive_rates[-1]:.4f}")
    
    print(f"\n压制式干扰:")
    print(f"  最大成功率: {np.max(suppressive_rates):.4f}")
    print(f"  最小成功率: {np.min(suppressive_rates):.4f}")
    print(f"  距离1km时成功率: {suppressive_rates[0]:.4f}")
    print(f"  距离25km时成功率: {suppressive_rates[240]:.4f}")
    print(f"  距离50km时成功率: {suppressive_rates[-1]:.4f}")


if __name__ == '__main__':
    main()
