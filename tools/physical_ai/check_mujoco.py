#!/usr/bin/env python3
"""Load official Go2 MJCF without SDK/DDS; inspect passive dynamics and render."""
import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import time
import mujoco
import numpy as np

ROOT = Path(__file__).resolve().parents[2]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--render', action='store_true')
    parser.add_argument('--viewer-seconds', type=float, default=0)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    model = mujoco.MjModel.from_xml_path(str(ROOT/'third_party/unitree_mujoco/unitree_robots/go2/scene.xml'))
    data = mujoco.MjData(model)
    if model.nkey:
        mujoco.mj_resetDataKeyframe(model, data, 0)
    mujoco.mj_forward(model, data)
    result = {'timestamp':datetime.now(timezone.utc).isoformat(),'mujoco':mujoco.__version__,
              'scope':'official MJCF only, no SDK, DDS, joystick or robot connection',
              'nq':model.nq,'nv':model.nv,'nu':model.nu,'nbody':model.nbody,
              'joints':[mujoco.mj_id2name(model,mujoco.mjtObj.mjOBJ_JOINT,i) for i in range(model.njnt)],
              'body_mass_kg':model.body_mass.tolist(),
              'body_inertia':model.body_inertia.tolist(),
              'joint_range':model.jnt_range.tolist(),
              'geom_friction':model.geom_friction.tolist(),
              'initial_qpos':data.qpos.tolist(),'render':'NOT_TESTED','viewer':'NOT_TESTED'}
    if args.render:
        import cv2
        with mujoco.Renderer(model, height=480, width=640) as renderer:
            camera = mujoco.MjvCamera()
            camera.lookat[:] = [0,0,0.25]
            camera.distance, camera.azimuth, camera.elevation = 1.6, 135, -20
            renderer.update_scene(data, camera=camera)
            rgb = renderer.render()
            path = args.output.with_suffix('.png')
            path.parent.mkdir(parents=True, exist_ok=True)
            if not cv2.imwrite(str(path),cv2.cvtColor(rgb,cv2.COLOR_RGB2BGR)):
                raise RuntimeError('Could not write preview')
            result.update(render='PASS', image=str(path), pixel_std=float(rgb.std()))
    if args.viewer_seconds:
        from mujoco import viewer as mjviewer
        with mjviewer.launch_passive(model, data) as viewer:
            start = time.monotonic()
            while viewer.is_running() and time.monotonic()-start < args.viewer_seconds:
                viewer.sync()
                time.sleep(0.05)
        result['viewer'] = 'PASS'
    peak_contacts = 0
    # No commanded torques: gravity/contact only; this is not a standing controller.
    data.ctrl[:] = 0
    for _ in range(round(2/model.opt.timestep)):
        mujoco.mj_step(model,data)
        peak_contacts = max(peak_contacts,data.ncon)
    result.update(sim_time_s=data.time, qpos=data.qpos.tolist(),qvel=data.qvel.tolist(),
                  max_contacts=peak_contacts,
                  sensor_values=data.sensordata.tolist(),
                  status='PASS' if np.isfinite(data.qpos).all() and peak_contacts>0 and model.nu==12 else 'FAIL')
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result,ensure_ascii=False,indent=2)+'\n')
    print(json.dumps({k:result[k] for k in ['status','mujoco','nq','nv','nu','max_contacts','render','viewer']}))
    return 0 if result['status']=='PASS' else 1


if __name__ == '__main__':
    raise SystemExit(main())
