"""Go2の点群、局所ワールドモデル、移動の安全監視。"""

from collections import deque
from dataclasses import dataclass
import json
import math
import struct

from go2_voice import SAFE_RANGE_M, SafetyError


FRAME_ID = "utlidar_lidar"
SCAN_MAX_AGE = 0.25
POSE_MAX_AGE = 0.15
MAP_MAX_AGE = 0.40
GRID_M = 0.05
CELL_RADIUS = GRID_M / math.sqrt(2)
HALF_LENGTH = 0.40
HALF_WIDTH = 0.24
LINEAR_SPEED = 0.08
ANGULAR_SPEED = 0.15
STOP_HORIZON = 0.85
MIN_CLEARANCE = 0.05
LIDAR_ERROR = 0.035
POSE_ERROR = 0.03
FLOOR_TOLERANCE = 0.025
MAX_TILT = 0.20
MAX_POINTS = 20_000
ZERO = (0.0, 0.0, 0.0)


def rotation(rpy):
    cr, cp, cy = (math.cos(a) for a in rpy)
    sr, sp, sy = (math.sin(a) for a in rpy)
    return (
        (cy * cp, cy * sp * sr - sy * cr, cy * sp * cr + sy * sr),
        (sy * cp, sy * sp * sr + cy * cr, sy * sp * cr - cy * sr),
        (-sp, cp * sr, cp * cr),
    )


@dataclass(frozen=True)
class Calibration:
    xyz: tuple
    matrix: tuple
    ground_z: float

    @classmethod
    def load(cls, path):
        data = json.loads(path.read_text(encoding="utf-8"))
        xyz = tuple(float(v) for v in data["lidar_xyz_m"])
        rpy = tuple(float(v) for v in data["lidar_rpy_rad"])
        ground_z = float(data["ground_z_m"])
        if (len(xyz) != 3 or len(rpy) != 3 or
                not all(math.isfinite(v) for v in (*xyz, *rpy, ground_z)) or
                math.hypot(*xyz) > 0.75 or max(map(abs, rpy)) > math.pi or
                not -0.60 <= ground_z <= -0.10):
            raise ValueError("LiDARの取付位置・姿勢または床面の設定値が範囲外です。")
        return cls(xyz, rotation(rpy), ground_z)


@dataclass(frozen=True)
class Pose:
    received: float
    xy: tuple
    rpy: tuple
    speed: float
    yaw_speed: float


def state_monitor(state, guard, now):
    guard.check(state, now)
    roll, pitch, yaw = state["rpy"]
    speed = math.hypot(*state["velocity"][:2])
    yaw_speed = abs(state["yaw_speed"])
    age = now - state["received"]
    if (not all(math.isfinite(v) for v in (roll, pitch, yaw, age)) or
            not 0 <= age <= POSE_MAX_AGE):
        raise SafetyError("機体の姿勢が無効か、運動状態の受信が途切れました。")
    if max(abs(roll), abs(pitch)) > MAX_TILT or speed > 0.20 or yaw_speed > 0.35:
        raise SafetyError("機体の傾き、速度または角速度が監視範囲を超えました。")
    return Pose(state["received"], tuple(state["position"][:2]), state["rpy"], speed, yaw_speed)


def cloud_xyz(message):
    if message.header.frame_id != FRAME_ID:
        raise ValueError(f"LiDARの座標系が想定外です ({message.header.frame_id!r})。")
    width, height = message.width, message.height
    step, row = message.point_step, message.row_step
    if (width < 1 or height < 1 or width * height > MAX_POINTS or not 12 <= step <= 128 or
            not width * step <= row <= width * step + 128 or len(message.data) != row * height):
        raise ValueError("点群のサイズまたはデータ長が無効です。")
    offsets = {}
    for field in message.fields:
        if field.name in ("x", "y", "z"):
            if (field.name in offsets or field.datatype != 7 or field.count != 1 or
                    not 0 <= field.offset <= step - 4):
                raise ValueError("点群のXYZフィールドが無効です。")
            offsets[field.name] = field.offset
    if len(offsets) != 3 or any(abs(a - b) < 4 for a, b in
                               ((offsets["x"], offsets["y"]), (offsets["x"], offsets["z"]),
                                (offsets["y"], offsets["z"]))):
        raise ValueError("点群のXYZフィールドが欠落または重複しています。")

    data = bytes(message.data)
    unpack = struct.Struct(">f" if message.is_bigendian else "<f").unpack_from
    x, y, z = (offsets[k] for k in "xyz")
    for start in range(0, row * height, row):
        for i in range(start, start + width * step, step):
            yield unpack(data, i + x)[0], unpack(data, i + y)[0], unpack(data, i + z)[0]


def cell(x, y):
    return math.floor(x / GRID_M), math.floor(y / GRID_M)


@dataclass(frozen=True)
class Observation:
    received: float
    pose: Pose
    occupied: frozenset
    floor: frozenset
    self_points: int


def pointcloud(message, received, pose, calibration):
    """取付補正→姿勢補正→運動状態の座標系へ変換する。"""
    # R_odom_lidar = R_odom_body @ R_body_lidar
    body = calibration.matrix
    odom = rotation(pose.rpy)
    matrix = tuple(tuple(sum(axis[i] * body[i][j] for i in range(3)) for j in range(3))
                   for axis in odom)
    tx, ty, tz = (sum(a * b for a, b in zip(axis, calibration.xyz)) for axis in odom)
    rx, ry, rz = matrix
    bx, by, bz = body
    base_x, base_y, base_z = calibration.xyz
    px, py = pose.xy
    occupied, floor = set(), set()
    own = 0
    ground = calibration.ground_z
    for x, y, z in cloud_xyz(message):
        if not all(math.isfinite(v) for v in (x, y, z)) or not 1e-6 < x*x + y*y + z*z <= 9.0:
            continue
        # 公式URDFのbase collision boxの内部だけを自機点として除外する。
        if (abs(bx[0]*x + bx[1]*y + bx[2]*z + base_x) < 0.1881 and
                abs(by[0]*x + by[1]*y + by[2]*z + base_y) < 0.04675 and
                abs(bz[0]*x + bz[1]*y + bz[2]*z + base_z) < 0.057):
            own += 1
            continue
        gx = rx[0]*x + rx[1]*y + rx[2]*z + tx
        gy = ry[0]*x + ry[1]*y + ry[2]*z + ty
        gz = rz[0]*x + rz[1]*y + rz[2]*z + tz
        if gx*gx + gy*gy > 4.0 or gz > ground + 0.60:
            continue
        target = cell(px + gx, py + gy)
        if abs(gz - ground) <= FLOOR_TOLERANCE:
            floor.add(target)
        else:
            occupied.add(target)
    if len(floor) < 8:
        raise ValueError("床面の点が不足しています。LiDARの取付変換、床面高さ、点群を確認してください。")
    return Observation(received, pose, frozenset(occupied), frozenset(floor), own)


def pose_matches(previous, current):
    elapsed = abs(current.received - previous.received)
    distance = math.dist(previous.xy, current.xy)
    turn = abs(math.remainder(current.rpy[2] - previous.rpy[2], 2 * math.pi))
    return distance <= POSE_ERROR + 0.20 * elapsed and turn <= 0.06 + 0.35 * elapsed


@dataclass(frozen=True)
class WorldModel:
    pose: Pose
    fusion: Observation
    occupied: frozenset
    floor: frozenset
    oldest: float


class LocalMap:
    def __init__(self):
        self.frames = deque()
        self.source_stamp = None
        self.clock_offset = None
        self.error = "LiDARの点群を待機中です。"

    def update(self, message, received, pose, calibration):
        try:
            stamp = message.header.stamp
            if stamp.sec < 0 or not (0 <= stamp.nanosec < 1_000_000_000):
                raise ValueError("LiDARの発行時刻が無効です。")
            source = stamp.sec * 1_000_000_000 + stamp.nanosec
            if source <= 0 or (self.source_stamp is not None and source <= self.source_stamp):
                raise ValueError("LiDARの発行時刻が進んでいません。")
            offset = received - source * 1e-9
            if self.clock_offset is not None and abs(offset - self.clock_offset) > SCAN_MAX_AGE:
                raise ValueError("LiDARの発行時刻または受信遅延が急変しました。")
            if not 0 <= received - pose.received <= POSE_MAX_AGE:
                raise ValueError("点群受信時の機体姿勢がありません。")
            if self.frames and received - self.frames[-1].received <= MAP_MAX_AGE:
                if not pose_matches(self.frames[-1].pose, pose):
                    raise ValueError("機体の位置または向きが急変したため、点群の履歴を破棄しました。")
            observation = pointcloud(message, received, pose, calibration)
        except (ValueError, OverflowError) as exc:
            self.frames.clear()
            self.error = str(exc)
            return
        self.source_stamp = source
        self.clock_offset = offset if self.clock_offset is None else min(offset, self.clock_offset)
        self.frames.append(observation)
        self.error = None

    def snapshot(self, pose, now):
        while self.frames and now - self.frames[0].received > MAP_MAX_AGE:
            self.frames.popleft()
        if not self.frames:
            raise SafetyError("LiDARの点群を更新できません。" if self.error is None else self.error)
        fusion = self.frames[-1]
        if not 0 <= now - fusion.received < SCAN_MAX_AGE:
            raise SafetyError("LiDARの更新が途切れました。")
        if not pose_matches(fusion.pose, pose):
            self.frames.clear()
            self.error = "機体の位置または向きが急変したため、点群の履歴を破棄しました。"
            raise SafetyError(self.error)
        occupied = frozenset().union(*(frame.occupied for frame in self.frames))
        floor = frozenset().union(*(frame.floor for frame in self.frames)) - occupied
        return WorldModel(pose, fusion, occupied, floor, self.frames[0].received)


def safety_monitor(model, velocity, origin, now):
    """機体の外形が停止までに通る領域を点群と照合する。戻り値が空なら許可。"""
    x, y, yaw = velocity
    if velocity == ZERO:
        return None
    if (not all(math.isfinite(v) for v in velocity) or
            sum(v != 0 for v in velocity) != 1 or
            math.hypot(x, y) > LINEAR_SPEED or abs(yaw) > ANGULAR_SPEED):
        return "速度指令が許可範囲を超えました。"

    pose = model.pose
    px, py = pose.xy
    c, s = math.cos(pose.rpy[2]), math.sin(pose.rpy[2])
    dx = math.copysign(max(abs(x), pose.speed) * STOP_HORIZON, x) if x else 0.0
    dy = math.copysign(max(abs(y), pose.speed) * STOP_HORIZON, y) if y else 0.0
    turn = math.copysign(max(abs(yaw), pose.yaw_speed) * STOP_HORIZON, yaw) if yaw else 0.0
    if math.hypot(px + c*dx - s*dy - origin[0], py + s*dx + c*dy - origin[1]) >= SAFE_RANGE_M:
        return "停止見込み位置が開始位置から25cmの範囲を超えます。"

    # 取付・測距誤差、位置合わせとスキャン中のずれ、格子の離散化を外周に加算する。
    margin = MIN_CLEARANCE + LIDAR_ERROR + POSE_ERROR + CELL_RADIUS
    margin += (now - model.oldest + 0.18) * (LINEAR_SPEED + ANGULAR_SPEED * 0.5)
    if yaw:
        margin += pose.speed * STOP_HORIZON
        # サンプル間で回転する角の移動量も余裕に含める。
        margin += abs(turn) * math.hypot(HALF_LENGTH + margin, HALF_WIDTH + margin) / 8
    else:
        margin += pose.yaw_speed * STOP_HORIZON * math.hypot(HALF_LENGTH + margin, HALF_WIDTH + margin)
    rotations = [(math.cos(turn*i/8), math.sin(turn*i/8)) for i in range(9)] if yaw else []
    xmin = -HALF_LENGTH + min(0, dx) - (CELL_RADIUS if x > 0 else margin)
    xmax = HALF_LENGTH + max(0, dx) + (CELL_RADIUS if x < 0 else margin)
    ymin = -HALF_WIDTH + min(0, dy) - (CELL_RADIUS if y > 0 else margin)
    ymax = HALF_WIDTH + max(0, dy) + (CELL_RADIUS if y < 0 else margin)
    radius = math.hypot(max(abs(xmin), abs(xmax)), max(abs(ymin), abs(ymax)))
    first_x, first_y = cell(px - radius, py - radius)
    last_x, last_y = cell(px + radius, py + radius)
    unknown = False
    for i in range(first_x, last_x + 1):
        for j in range(first_y, last_y + 1):
            qx, qy = (i + 0.5) * GRID_M - px, (j + 0.5) * GRID_M - py
            bx, by = c*qx + s*qy, -s*qx + c*qy
            if rotations:
                in_path = any(abs(ca*bx + sa*by) <= HALF_LENGTH + margin and
                              abs(-sa*bx + ca*by) <= HALF_WIDTH + margin for ca, sa in rotations)
            else:
                in_path = xmin <= bx <= xmax and ymin <= by <= ymax
            if not in_path:
                continue
            if (i, j) in model.fusion.occupied or (i, j) in model.occupied:
                return "停止までに機体が通る範囲に障害物があります。"
            if (abs(bx) > HALF_LENGTH or abs(by) > HALF_WIDTH) and (i, j) not in model.floor:
                unknown = True
    if unknown:
        return "停止までに機体が通る範囲に未観測の床面があります。"
    return None
