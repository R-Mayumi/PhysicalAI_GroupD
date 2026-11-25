# 資料01〜11をこのPCで実行するための関数。sourceしただけでは接続・動作しない。
# 変更承認済み: 06は内蔵キーボードを使用し、対象デバイスだけ一時的に読取可能にする。
# 02の現在の結果: 状態コード2010。コード0、03の実測校正、04〜05の判定確認まで実走保留。

GO2_TEST_NIC=enx9c69d351a4cf
GO2_TEST_CAL=/home/scg-hp2/pyhai_go2_workspace/go2_lidar_calibration.json
GO2_TEST_LOG=/home/scg-hp2/pyhai_go2_workspace/go2-test-20261125-165702-_9zftm4_
GO2_TEST_KBD=/dev/input/by-path/platform-i8042-serio-0-event-kbd

go2_check() {
  local label="$1" rc log
  log=$(mktemp --suffix=.log "$GO2_TEST_LOG/$label-XXXXXX") || return
  printf '保存先 %s\n' "$log"
  /home/scg-hp2/pyhai_go2_workspace/.venv/bin/python -u \
    /home/scg-hp2/pyhai_go2_workspace/go2_teleop.py \
    --check --net-if "$GO2_TEST_NIC" --calibration "$GO2_TEST_CAL" \
    2>&1 | tee "$log"
  rc=${PIPESTATUS[0]}
  printf '終了コード %s\n' "$rc" | tee -a "$log"
  return "$rc"
}

# 追加承認済み: 状態コードを運動許可に変換しない、購読専用の静止幾何診断。
go2_geometry() {
  local label="$1" rc output log
  output=$(mktemp --suffix=.json "$GO2_TEST_LOG/$label-geometry-XXXXXX") || return
  log="${output%.json}.log"
  printf '診断保存先 %s\n' "$output"
  /home/scg-hp2/pyhai_go2_workspace/.venv/bin/python -u \
    /home/scg-hp2/pyhai_go2_workspace/go2_lidar_check.py \
    --net-if "$GO2_TEST_NIC" --calibration "$GO2_TEST_CAL" \
    --seconds 3 --output "$output" 2>&1 | tee "$log"
  rc=${PIPESTATUS[0]}
  printf '終了コード %s（幾何解析の成否。運動許可ではありません）\n' "$rc" | tee -a "$log"
  return "$rc"
}

go2_drive() {
  local label="$1" rc log
  log=$(mktemp --suffix=.log "$GO2_TEST_LOG/$label-XXXXXX") || return
  printf '保存先 %s\n' "$log"
  /home/scg-hp2/pyhai_go2_workspace/.venv/bin/python -u \
    /home/scg-hp2/pyhai_go2_workspace/go2_teleop.py \
    --drive --net-if "$GO2_TEST_NIC" --calibration "$GO2_TEST_CAL" \
    --keyboard "$GO2_TEST_KBD" 2>&1 | tee "$log"
  rc=${PIPESTATUS[0]}
  printf '終了コード %s\n' "$rc" | tee -a "$log"
  return "$rc"
}

go2_keyboard_enable() {
  local device snapshot
  device=$(readlink -f -- "$GO2_TEST_KBD") || return
  if [[ "$device" != /dev/input/event* || ! -c "$device" ]]; then
    printf 'キーボードのデバイスを確認してください: %s\n' "$GO2_TEST_KBD" >&2
    return 1
  fi
  snapshot="$GO2_TEST_LOG/keyboard-acl-before.txt"
  if [[ ! -e "$snapshot" ]]; then
    getfacl --absolute-names "$device" > "$snapshot" || return
  fi
  # パスワードは利用者の端末でsudoに入力する。保存しない。
  sudo setfacl -m "u:$(id -un):r" "$device" || return
  if [[ -r "$GO2_TEST_KBD" ]]; then
    printf 'キーボード読取可: %s\n' "$GO2_TEST_KBD"
    printf '試験終了後に go2_keyboard_restore を実行してください。\n'
  else
    printf 'キーボードの読取権限を確認できません。\n' >&2
    return 1
  fi
}

go2_keyboard_restore() {
  local snapshot="$GO2_TEST_LOG/keyboard-acl-before.txt"
  if [[ ! -f "$snapshot" ]]; then
    printf '権限の保存ファイルがありません: %s\n' "$snapshot" >&2
    return 1
  fi
  sudo setfacl --restore="$snapshot" || return
  printf 'キーボードの権限を変更前に戻しました。\n'
}
