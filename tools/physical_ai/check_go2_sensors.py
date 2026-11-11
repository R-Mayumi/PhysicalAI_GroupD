#!/usr/bin/env python3
"""Subscribe to Go2 sensors and request one camera image. No motion or setters."""
import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import sys
import threading
import time

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT/'vendor/unitree_sdk2_python'))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--net-if', required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--seconds', type=float, default=8)
    args = parser.parse_args()
    if not 1 <= args.seconds <= 60:
        parser.error('--seconds must be between 1 and 60')
    from unitree_sdk2py.core.channel import ChannelFactoryInitialize, ChannelSubscriber
    from unitree_sdk2py.idl.unitree_go.msg.dds_ import LowState_, SportModeState_
    from unitree_sdk2py.idl.sensor_msgs.msg.dds_ import PointCloud2_
    from unitree_sdk2py.go2.video.video_client import VideoClient
    import cv2
    import numpy as np
    result = {'started_at':datetime.now(timezone.utc).isoformat(),
              'interface':args.net_if, 'domain_id':0,
              'scope':'sensor subscriptions and VideoClient.GetImageSample only',
              'topics':{}, 'motion':'NOT_TESTED', 'ros2_robot':'NOT_TESTED'}
    lock = threading.Lock()
    subs = []

    def receive(msg, topic):
        with lock:
            item = result['topics'][topic]
            now = time.monotonic()
            item.setdefault('first', now)
            item['last'] = now
            item['count'] += 1
            if isinstance(msg, LowState_):
                item['sample'] = {'imu_rpy':list(msg.imu_state.rpy),
                                  'imu_quaternion':list(msg.imu_state.quaternion),
                                  'motor_state_slots':len(msg.motor_state),
                                  'motor_q':[m.q for m in msg.motor_state],
                                  'motor_dq':[m.dq for m in msg.motor_state],
                                  'battery_soc':msg.bms_state.soc}
            elif isinstance(msg, SportModeState_):
                item['sample'] = {'error_code':msg.error_code, 'mode':msg.mode}
                item.setdefault('observed_error_codes', [])
                if msg.error_code not in item['observed_error_codes']:
                    item['observed_error_codes'].append(msg.error_code)
            else:
                item['sample'] = {'width':msg.width, 'height':msg.height,
                                  'frame_id':msg.header.frame_id,
                                  'fields':[f.name for f in msg.fields],
                                  'data_bytes':len(msg.data), 'point_step':msg.point_step}
    try:
        ChannelFactoryInitialize(0, args.net_if)
        for topic, cls in [('rt/lf/lowstate', LowState_),
                           ('rt/lf/sportmodestate', SportModeState_),
                           ('rt/utlidar/cloud', PointCloud2_)]:
            result['topics'][topic] = {'count':0}
            sub = ChannelSubscriber(topic, cls)
            sub.Init(lambda msg, t=topic:receive(msg,t), 10)
            subs.append(sub)
        time.sleep(args.seconds)
        client = VideoClient()
        client.SetTimeout(4.0)
        client.Init()
        start = time.monotonic()
        code, data = client.GetImageSample()
        frame = cv2.imdecode(np.frombuffer(bytes(data), dtype=np.uint8), cv2.IMREAD_COLOR) if code == 0 and data else None
        result['camera'] = {'code':code, 'bytes':len(data) if data else 0,
                            'decoded_shape':list(frame.shape) if frame is not None else None,
                            'elapsed_s':time.monotonic()-start, 'image_saved':False,
                            'status':'PASS' if frame is not None else 'NOT_TESTED'}
    except Exception as exc:
        result['exception'] = repr(exc)
    finally:
        for sub in subs:
            sub.Close()
        with lock:
            for item in result['topics'].values():
                first, last = item.pop('first', None), item.pop('last', None)
                span = last-first if first is not None and last is not None else 0
                item['approx_hz'] = (item['count']-1)/span if span > 0 else None
                item['status'] = 'PASS' if item['count'] else 'NOT_TESTED'
        result['finished_at'] = datetime.now(timezone.utc).isoformat()
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(result,ensure_ascii=False,indent=2)+'\n')
        print(json.dumps(result, ensure_ascii=False))
    return 0 if result.get('camera',{}).get('status') == 'PASS' and all(i['count'] for i in result['topics'].values()) else 1


if __name__ == '__main__':
    raise SystemExit(main())
