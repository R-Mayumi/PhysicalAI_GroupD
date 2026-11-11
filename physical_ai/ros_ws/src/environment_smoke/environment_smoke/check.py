"""Cross-process local ROS pub/sub and CLI verification; never uses a robot API."""
import argparse
import json
from pathlib import Path
import subprocess
import sys
import threading
import time
import rclpy
from rclpy.node import Node
from rclpy.utilities import get_rmw_implementation_identifier
from std_msgs.msg import String


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--publisher', action='store_true')
    parser.add_argument('--seconds', type=float, default=5)
    parser.add_argument('--output', type=Path)
    parser.add_argument('--stop-file', type=Path)
    args, _ = parser.parse_known_args()
    rclpy.init()
    node = Node('environment_audit_publisher' if args.publisher else 'environment_audit_listener')
    received = []
    topic = '/environment_audit/heartbeat'
    child = None
    cli = {}
    cli_thread = None
    if args.publisher:
        pub = node.create_publisher(String, topic, 10)
        def send():
            msg = String()
            msg.data = str(time.monotonic())
            pub.publish(msg)
        node.create_timer(0.1, send)
    else:
        sub = node.create_subscription(String, topic, lambda msg:received.append(time.monotonic()), 10)
        child_args = [sys.executable, str(Path(__file__).resolve()),
                      '--publisher', '--seconds', str(args.seconds)]
        if args.stop_file:
            child_args += ['--stop-file',str(args.stop_file)]
        child = subprocess.Popen(child_args)
    def check_cli():
        for kind in ['topic','node']:
            p = subprocess.run(['ros2',kind,'list','--no-daemon','--spin-time','0.5'],
                               text=True, capture_output=True, timeout=10)
            cli[kind] = {'returncode':p.returncode,'stdout':p.stdout,'stderr':p.stderr}
    start = time.monotonic()
    try:
        while time.monotonic()-start < args.seconds and not (args.stop_file and args.stop_file.exists()):
            rclpy.spin_once(node, timeout_sec=0.1)
            if not args.publisher and cli_thread is None and time.monotonic()-start > 2:
                cli_thread = threading.Thread(target=check_cli)
                cli_thread.start()
    finally:
        if cli_thread:
            cli_thread.join(timeout=25)
        if child:
            try:
                child.wait(timeout=5)
            except subprocess.TimeoutExpired:
                child.kill()
                child.wait()
        node.destroy_node()
        if rclpy.ok():
            rclpy.shutdown()
    if not args.publisher:
        gaps = [b-a for a,b in zip(received, received[1:])]
        cli_ok = (len(cli)==2 and all(p['returncode']==0 for p in cli.values())
                  and topic in cli['topic']['stdout']
                  and '/environment_audit_publisher' in cli['node']['stdout'])
        result = {'status':'PASS' if len(received)>5 and cli_ok else 'FAIL',
                  'received':len(received), 'elapsed_s':time.monotonic()-start,
                  'max_gap_s':max(gaps, default=None),
                  'rmw':get_rmw_implementation_identifier(), 'cli':cli,
                  'scope':'domain 42, localhost, synthetic String heartbeat; no robot communication'}
        if args.output:
            args.output.parent.mkdir(parents=True, exist_ok=True)
            args.output.write_text(json.dumps(result, indent=2)+'\n')
        print(json.dumps(result), flush=True)
        return 0 if result['status']=='PASS' else 1
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
