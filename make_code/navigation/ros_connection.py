"""Go2向けROS 2接続。OSのネットワーク設定は変更しない。

実行プロセスのRMW_IMPLEMENTATION、CYCLONEDDS_URI、ROS_LOCALHOST_ONLYを
Go2接続用に設定し、Domainは--domain-id（既定0）を使う。
参考: https://github.com/unitreerobotics/unitree_ros2#connect-to-unitree-robot
"""

import ipaddress
import json
import math
import os
from pathlib import Path
import shlex
import signal
import subprocess
import tempfile
from xml.sax.saxutils import quoteattr

try:
    import rclpy
except ImportError as exc:
    raise SystemExit(
        "ROS 2を読み込めません。プロジェクト直下の .venv/bin/python で実行してください。"
    ) from exc

ROOT = Path(__file__).resolve().parents[2]


def positive_seconds(value):
    value = float(value)
    if not math.isfinite(value) or value <= 0:
        raise ValueError("待ち時間は有限の正数で指定してください。")
    return value


def add_connection_arguments(parser):
    parser.add_argument("--net-if", help="Go2側NIC。省略時はGO2_NET_IF、.env、自動検出の順")
    parser.add_argument("--domain-id", type=int, default=0, help="DDS Domain（既定: 0）")


def configured_interface():
    if "GO2_NET_IF" in os.environ:
        return os.environ["GO2_NET_IF"].strip() or None
    env_file = ROOT / ".env"
    value = None
    if env_file.is_file():
        for line in env_file.read_text(encoding="utf-8").splitlines():
            key, sep, raw = line.partition("=")
            if sep and key.strip() == "GO2_NET_IF":
                parts = shlex.split(raw, comments=True)
                if len(parts) > 1:
                    raise ValueError(".envのGO2_NET_IFにはNIC名を1つ指定してください。")
                value = parts[0] if parts else None
    return value


def network_interface(explicit=None):
    result = subprocess.run(
        ["ip", "-j", "-4", "addr", "show"], capture_output=True, text=True, check=False
    )
    if result.returncode:
        raise RuntimeError("NIC情報を取得できません。PCの通常の端末から実行してください。")
    interfaces = json.loads(result.stdout)
    if explicit:
        matches = [item for item in interfaces if item["ifname"] == explicit]
    else:
        subnet = ipaddress.ip_network("192.168.123.0/24")
        matches = [item for item in interfaces if any(
            ipaddress.ip_address(addr["local"]) in subnet
            for addr in item.get("addr_info", []) if addr.get("family") == "inet"
        )]
    if len(matches) != 1 or not any(
        addr.get("family") == "inet" for addr in matches[0].get("addr_info", [])
    ):
        raise RuntimeError(
            "Go2用のIPv4アドレスを持つNICを特定できません。"
            "接続と ip -br -4 addr を確認し、--net-if を指定してください。"
        )
    return matches[0]["ifname"]


def init_ros(net_if=None, domain_id=0):
    if not 0 <= domain_id <= 232:
        raise ValueError("--domain-id は0〜232で指定してください。")
    interface = network_interface(net_if if net_if is not None else configured_interface())
    os.environ["RMW_IMPLEMENTATION"] = "rmw_cyclonedds_cpp"
    os.environ["ROS_LOCALHOST_ONLY"] = "0"
    os.environ["CYCLONEDDS_URI"] = (
        '<CycloneDDS><Domain Id="any"><General><Interfaces>'
        f'<NetworkInterface name={quoteattr(interface)}/>'
        '</Interfaces></General></Domain></CycloneDDS>'
    )
    os.environ.setdefault("ROS_LOG_DIR", str(Path(tempfile.gettempdir()) / "go2_navigation_logs"))
    # rclpyがSIGINT時に先にcontextを閉じると、finallyで停止を送れない。
    from rclpy.signals import SignalHandlerOptions
    rclpy.init(args=[], domain_id=domain_id, signal_handler_options=SignalHandlerOptions.NO)
    signal.signal(signal.SIGTERM, _interrupt)
    print(f"ROS 2: NIC={interface}, Domain={domain_id}, rmw_cyclonedds_cpp", flush=True)


def _interrupt(signum, frame):
    raise KeyboardInterrupt
