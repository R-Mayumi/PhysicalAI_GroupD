#!/usr/bin/env python3
"""購読した静止点群の座標変換と平面候補を可視化する。校正・運動指令の変更なし。"""
import argparse
from collections import deque
from datetime import datetime
import json
import math
import os
from pathlib import Path
import sys
import tempfile
import threading
import time
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / 'vendor/unitree_sdk2_python'))
import numpy as np
from go2_lidar import Calibration, cloud_xyz, rotation
from go2_lidar_check import diagnostic_pose


def fit_plane(points):
    if len(points) < 30:
        return {'status': 'insufficient_points', 'points': len(points)}
    rng = np.random.default_rng(0)
    best = np.zeros(len(points), dtype=bool)
    for _ in range(96):
        selected = points[rng.choice(len(points), 3, replace=False)]
        normal = np.cross(selected[1] - selected[0], selected[2] - selected[0])
        length = np.linalg.norm(normal)
        if length < 1e-9 or abs(normal[2]) / length < math.cos(math.radians(45)):
            continue
        normal /= length
        mask = np.abs((points - selected[0]) @ normal) <= 0.02
        if np.count_nonzero(mask) > np.count_nonzero(best):
            best = mask
    if np.count_nonzero(best) < max(30, 0.25 * len(points)):
        return {'status': 'no_dominant_plane', 'points': len(points)}
    selected = points[best]
    coeff, _, rank, _ = np.linalg.lstsq(np.column_stack((selected[:, :2], np.ones(len(selected)))), selected[:, 2], rcond=None)
    if rank < 3:
        return {'status': 'degenerate', 'points': len(points)}
    a, b, c = map(float, coeff)
    residual = selected[:, 2] - (a * selected[:, 0] + b * selected[:, 1] + c)
    return {'status': 'candidate', 'points': len(points), 'inliers': len(selected),
            'inlier_fraction': float(len(selected) / len(points)),
            'z_equals_a_x_plus_b_y_plus_c': [a, b, c],
            'tilt_deg': math.degrees(math.atan(math.hypot(a, b))),
            'rms_vertical_residual_m': float(np.sqrt(np.mean(residual ** 2))),
            'note': '周囲の平面候補。床であることや正しい校正を保証しない。'}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--net-if', required=True)
    parser.add_argument('--calibration', type=Path, required=True)
    parser.add_argument('--output-prefix', type=Path, required=True)
    parser.add_argument('--frames', type=int, default=1, help='静止した同一センサー座標の観測フレーム数')
    args = parser.parse_args()
    if not 1 <= args.frames <= 30:
        parser.error('--framesは1〜30で指定してください。')
    calibration = Calibration.load(args.calibration)
    from unitree_sdk2py.core.channel import ChannelFactoryInitialize, ChannelSubscriber
    from unitree_sdk2py.idl.unitree_go.msg.dds_ import SportModeState_
    from unitree_sdk2py.idl.sensor_msgs.msg.dds_ import PointCloud2_
    lock = threading.Lock()
    state = packet = None
    packets = deque(maxlen=args.frames)
    subscribers = []

    def receive_state(message):
        nonlocal state
        value = {'received': time.monotonic(), 'position': tuple(message.position),
                 'velocity': tuple(message.velocity), 'rpy': tuple(message.imu_state.rpy),
                 'yaw_speed': message.yaw_speed, 'error_code': message.error_code,
                 'body_height_m': message.body_height}
        with lock:
            state = value

    def receive_cloud(message):
        nonlocal packet
        received = time.monotonic()
        with lock:
            packet = message, received, state
            if state is not None:
                packets.append(packet)

    try:
        ChannelFactoryInitialize(0, args.net_if)
        for topic, kind, callback in [('rt/sportmodestate', SportModeState_, receive_state),
                                       ('rt/utlidar/cloud', PointCloud2_, receive_cloud)]:
            sub = ChannelSubscriber(topic, kind)
            subscribers.append(sub)
            sub.Init(callback, 1)
        time.sleep(max(2, args.frames / 10 + 0.5))
        with lock:
            snapshot = packet
            snapshots = list(packets)
        if snapshot is None or time.monotonic() - snapshot[1] >= 0.25:
            raise RuntimeError('新しい点群を受信できません。')
        message, received, scan_state = snapshot
        pose = diagnostic_pose(scan_state, received)
        if len(snapshots) != args.frames:
            raise RuntimeError(f'必要な点群フレームを受信できません: {len(snapshots)}/{args.frames}')
        for _, sample_time, sample_state in snapshots:
            diagnostic_pose(sample_state, sample_time)
    finally:
        for sub in subscribers:
            sub.Close()

    raw = np.concatenate([np.asarray(list(cloud_xyz(item[0])), dtype=float) for item in snapshots])
    raw = raw[np.isfinite(raw).all(axis=1)]
    raw = raw[(np.linalg.norm(raw, axis=1) > 1e-3) & (np.linalg.norm(raw, axis=1) <= 3)]
    imu = np.asarray(rotation(pose.rpy))
    transform = imu @ np.asarray(calibration.matrix)
    transformed = raw @ transform.T + imu @ np.asarray(calibration.xyz)
    # 同じ静止配置の板あり／なしを後から比較できるよう、観測点だけを保存する。
    snapshot_file = args.output_prefix.with_suffix('.npz')
    np.savez_compressed(snapshot_file, raw_xyz_m=raw, transformed_xyz_m=transformed,
                        position_m=np.asarray(scan_state['position']), rpy_rad=np.asarray(pose.rpy),
                        received_monotonic_s=received, error_code=scan_state['error_code'],
                        frame_count=len(snapshots))
    radius = np.linalg.norm(transformed[:, :2], axis=1)
    mask = (radius >= 0.45) & (radius <= 1.0) & (np.abs(transformed[:, 2]) <= 1)
    transformed_fit = fit_plane(transformed[mask])
    raw_radius = np.linalg.norm(raw[:, :2], axis=1)
    raw_fit = fit_plane(raw[(raw_radius >= 0.45) & (raw_radius <= 1.0) & (np.abs(raw[:, 2]) <= 1)])
    report = {'timestamp': datetime.now(ZoneInfo('Asia/Tokyo')).isoformat(),
              'scope': 'DDS subscriptions and offline visualization only; no commands or calibration changes',
              'motion_authorized': False, 'state_code': scan_state['error_code'],
              'body_height_m': scan_state['body_height_m'], 'rpy_rad': pose.rpy,
              'configured_ground_z_m': calibration.ground_z, 'finite_points': len(raw),
              'frame_count': len(snapshots),
              'point_snapshot': str(snapshot_file),
              'raw_sensor_frame_plane': raw_fit, 'configured_transform_plane': transformed_fit}
    os.environ.setdefault('MPLCONFIGDIR', str(Path(tempfile.gettempdir()) / 'go2-matplotlib'))
    import matplotlib
    matplotlib.use('Agg')
    import matplotlib.pyplot as plt
    fig, axes = plt.subplots(2, 2, figsize=(11, 8), constrained_layout=True)
    for ax, data, horizontal, vertical, title in [
        (axes[0, 0], raw, 0, 2, 'Raw sensor frame: X-Z'),
        (axes[0, 1], transformed, 0, 2, 'Configured transform + IMU: X-Z'),
        (axes[1, 0], transformed, 1, 2, 'Configured transform + IMU: Y-Z'),
        (axes[1, 1], transformed, 0, 1, 'Configured transform + IMU: X-Y')]:
        ax.scatter(data[:, horizontal], data[:, vertical], s=2, c=data[:, 2], cmap='viridis', vmin=-0.7, vmax=0.3)
        ax.set(xlabel='XYZ'[horizontal] + ' [m]', ylabel='XYZ'[vertical] + ' [m]', title=title)
        ax.grid(True, alpha=0.3)
        ax.set_aspect('equal', adjustable='box')
        ax.set_xlim(-1.2, 1.2)
        ax.set_ylim((-0.8, 0.6) if vertical == 2 else (-1.2, 1.2))
    for ax in (axes[0, 1], axes[1, 0]):
        ax.axhline(calibration.ground_z, c='red', lw=1.5, label=f'Configured floor: {calibration.ground_z:.3f} m')
        ax.legend(fontsize=8)
    fig.suptitle(f'Read-only LiDAR diagnostic | state {scan_state["error_code"]} | no motion authorization')
    figure = args.output_prefix.with_suffix('.png')
    fig.savefig(figure, dpi=160)
    plt.close(fig)
    report['figure'] = str(figure)
    args.output_prefix.with_suffix('.json').write_text(json.dumps(report, ensure_ascii=False, indent=2) + '\n')
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
