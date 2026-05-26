"""
雷达干扰作用距离测试脚本
对应MATLAB文件: test_RadJamSUCC_Range.m
"""

import matplotlib.pyplot as plt
from radar_jamming_range_calculation import (
    RadarParams, JammerRadarParams, CalculationOptions,
    radar_jamming_range_calculation
)


def main():
    """主测试函数"""
    # 定义雷达参数
    radar = RadarParams(
        power=200,             # 200W
        gain=30,               # 30dB
        frequency=16,          # 16GHz
        bandwidth=100,         # 100MHz
        loss=4,                # 4dB
        noise_figure=3,        # 3dB
        rcs=1,                 # 1m² RCS
        Rt=2000                # 2000m (2km)
    )
    
    # 定义干扰机参数（压制干扰和欺骗干扰共用）
    jammer = JammerRadarParams(
        power=20,             # 100W
        gain=7,               # 15dB
        bandwidth=100,         # 500MHz
        loss=30,               # 3dB+19dB方向图损耗（旁瓣进入）
        deception_gain=6,      # 6dB欺骗增益
        DRFM_quality=0.85      # DRFM质量因子
    )
    
    # 计算压制干扰作用距离
    print("正在计算压制干扰作用距离...")
    R_jam_noise, results_noise = radar_jamming_range_calculation(
        radar, jammer, 'noise'
    )
    
    # 计算欺骗干扰作用距离
    print("\n正在计算欺骗干扰作用距离...")
    R_jam_deception, results_deception = radar_jamming_range_calculation(
        radar, jammer, 'deception'
    )
    
    # 输出对比结果
    print('\n=== 对比结果 ===')
    print(f'压制干扰有效距离: {R_jam_noise:.2f} km')
    print(f'欺骗干扰有效距离: {R_jam_deception:.2f} km')
    
    if R_jam_noise > 0:
        print(f'欺骗/压制距离比: {R_jam_deception/R_jam_noise:.2f}')
    else:
        print('欺骗/压制距离比: N/A (压制距离为0)')
    
    # 保持所有图窗打开直到用户关闭
    plt.show()


if __name__ == '__main__':
    main()
