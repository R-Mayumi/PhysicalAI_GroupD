#!/usr/bin/env python3
"""保存済み静止点群の占有理由を分類する。通信・指令送信・設定変更なし。"""
import json
import os
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import numpy as np
from go2_lidar import FLOOR_TOLERANCE, GRID_M, rotation


def main():
    root = Path(__file__).resolve().parent
    source = np.load(root / '03-chair-removed-20frames.npz')
    raw = source['raw_xyz_m']
    imu = np.asarray(rotation((*source['rpy_rad'][:2], 0.0)))
    summary = {'scope': 'offline diagnostic only; no commands or calibration changes', 'motion_authorized': False, 'source': str(root / '03-chair-removed-20frames.npz'), 'cases': {}}
    os.environ.setdefault('MPLCONFIGDIR', '/tmp/go2-matplotlib')
    import matplotlib
    matplotlib.use('Agg')
    import matplotlib.pyplot as plt
    fig, axes = plt.subplots(2, 2, figsize=(11, 8), constrained_layout=True)
    cases = [('original', ROOT / 'go2_lidar_calibration.json'), ('proposal', root / 'calibration-proposal-diagnostic-only.json')]
    for column, (label, calibration_path) in enumerate(cases):
        calibration = json.loads(calibration_path.read_text())
        body = raw @ np.asarray(rotation(calibration['lidar_rpy_rad'])).T + calibration['lidar_xyz_m']
        own = (np.abs(body) < np.array([.1881, .04675, .057])).all(axis=1)
        points = body @ imu.T
        ground = calibration['ground_z_m']
        kept = ~own & (np.linalg.norm(points[:, :2], axis=1) <= 2) & (points[:, 2] <= ground + .60)
        points = points[kept]
        height = points[:, 2]
        below = height < ground - FLOOR_TOLERANCE
        above = height > ground + FLOOR_TOLERANCE
        floor = ~(below | above)
        nearby = (np.abs(points[:, 0]) <= .7) & (np.abs(points[:, 1]) <= .5)
        # 全保存フレームを使うため、本番の0.4秒履歴より長い。運動可否の再判定には使わない。
        floor_cells = {tuple(cell) for cell in np.floor(points[floor, :2] / GRID_M).astype(int)}
        below_cells = {tuple(cell) for cell in np.floor(points[below, :2] / GRID_M).astype(int)}
        above_cells = {tuple(cell) for cell in np.floor(points[above, :2] / GRID_M).astype(int)}
        all_occupied = below_cells | above_cells
        near_heights = height[nearby & below]
        summary['cases'][label] = {
            'calibration': str(calibration_path), 'self_points_excluded': int(own.sum()),
            'near_region_abs_x_m': .7, 'near_region_abs_y_m': .5,
            'near_region_points': int(nearby.sum()), 'near_floor_band_points': int((nearby & floor).sum()),
            'near_below_floor_points': int((nearby & below).sum()), 'near_above_floor_points': int((nearby & above).sum()),
            'near_below_floor_z_5_50_95_percentile_m': np.percentile(near_heights, [5, 50, 95]).tolist() if len(near_heights) else None,
            'occupied_cells_all_saved_frames': len(all_occupied),
            'below_floor_only_occupied_cells': len(below_cells - above_cells),
            'above_floor_only_occupied_cells': len(above_cells - below_cells),
            'mixed_above_and_below_cells': len(below_cells & above_cells),
            'floor_cells_also_occupied': len(floor_cells & all_occupied),
            'note': '保存した20フレームの参考集計。本番の移動許可とは異なる。床より下の点も現行処理では占有となる。',
        }
        for row, (vertical, ylim) in enumerate(((1, (-.9, .9)), (2, (-.8, .5)))):
            ax = axes[row, column]
            for mask, color, legend in [(floor, '#328a4a', 'Floor band'), (below, '#d88819', 'Below floor band'), (above, '#c64242', 'Above floor band')]:
                subset = points[mask][::max(1, int(mask.sum()) // 7000)]
                ax.scatter(subset[:, 0], subset[:, vertical], s=2, c=color, alpha=.5, label=legend)
            ax.set(title=label, xlabel='Levelled body X [m]', ylabel='Levelled body ' + 'XYZ'[vertical] + ' [m]', xlim=(-.8, 1.4), ylim=ylim)
            ax.grid(alpha=.25)
            ax.set_aspect('equal', adjustable='box')
            if row == 1:
                ax.axhline(ground, c='black', lw=1)
                ax.axhspan(ground - FLOOR_TOLERANCE, ground + FLOOR_TOLERANCE, color='green', alpha=.1)
            ax.legend(fontsize=7)
    fig.suptitle('Saved empty-scene points by height | no motion authorization')
    fig.savefig(root / '03-occupied-height-comparison.png', dpi=160)
    plt.close(fig)
    (root / '03-occupied-height-analysis.json').write_text(json.dumps(summary, ensure_ascii=False, indent=2) + '\n')
    print(json.dumps(summary, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
