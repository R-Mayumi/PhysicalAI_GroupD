#!/usr/bin/env python3
"""Read-only inventory; does not import SDKs, alter networking or install packages."""
from run_logged import run

commands = [
    ["bash", "-c", "cat /etc/os-release; uname -r; lscpu; free -b; swapon --show --bytes; df -B1 .; lsblk -o NAME,SIZE,TYPE,FSTYPE,MOUNTPOINTS,MODEL; uptime; cat /proc/uptime"],
    ["lspci", "-nnk"],
    ["bash", "-c", "for p in /sys/class/drm/card*/device/mem_info*; do if [ -r \"$p\" ]; then echo \"$p\"; cat \"$p\"; fi; done; ls -l /dev/dri /dev/accel; lsmod | rg 'amdgpu|amdxdna'; modinfo amdgpu | head -15"],
    ["bash", "-c", "command -v python3 ollama ros2 colcon git code nvidia-smi rocminfo vulkaninfo; python3 --version; git --version; ollama --version; dpkg-query -W 'ros-*' '*cyclone*' '*mujoco*' '*mesa*' '*colcon*' 2>/dev/null"],
    ["bash", "-c", "ls -la .venv/bin; ls -la /home/scg-hp2/miniforge3/envs/ros_ai/bin/ros2 /home/scg-hp2/miniforge3/envs/ros_ai/bin/colcon; cat .venv/pyvenv.cfg; cat /home/scg-hp2/ros_ai_workspace/.ros_ai_rc; rg --files --hidden /home/scg-hp2/ros_ai_workspace /home/scg-hp2/.local /opt /usr/local -g '*cyclonedds*.xml' -g '*unitree_ros2*' -g 'pyvenv.cfg' -g '*mujoco*' -g 'ros2' -g 'colcon' -g '!node_modules/**' -g '!.git/**'"],
    ["bash", "-c", "du -sb /home/scg-hp2/.ollama /usr/share/ollama /home/scg-hp2/.cache/huggingface /home/scg-hp2/.cache/whisper /home/scg-hp2/miniforge3/envs/ros_ai .venv vendor 2>/dev/null; ps -eo pid,comm,rss --sort=-rss | head -20"],
]
for command in commands:
    run(command)
probe = '''import importlib.metadata as m, importlib.util as u, json, sys
names={'rclpy':'rclpy','ros2cli':'ros2cli','ros2topic':'ros2topic','ros2node':'ros2node','ros2launch':'ros2launch','colcon-core':'colcon','cyclonedds':'cyclonedds','unitree_sdk2py':'unitree_sdk2py','torch':'torch','opencv-python':'cv2','numpy':'numpy','faster-whisper':'faster_whisper','openai-whisper':'whisper','pyttsx3':'pyttsx3','TTS':'TTS','jupyter':'jupyter','jupyterlab':'jupyterlab','notebook':'notebook','mujoco':'mujoco','ollama':'ollama','onnxruntime':'onnxruntime','piper-tts':'piper'}
out={'python':sys.version,'executable':sys.executable,'prefix':sys.prefix,'base_prefix':sys.base_prefix,'sys_path':sys.path,'packages':{}}
for dist,module in names.items():
 try: spec=u.find_spec(module)
 except (ImportError,ValueError): spec=None
 try: version=m.version(dist)
 except m.PackageNotFoundError: version=None
 out['packages'][dist]={'version':version,'module':spec.origin if spec else None}
print(json.dumps(out,ensure_ascii=False,indent=2))'''
for python in ["/usr/bin/python3", str(__import__('pathlib').Path('.venv/bin/python').resolve()), "/home/scg-hp2/miniforge3/envs/ros_ai/bin/python", ".venv/bin/python"]:
    run([python, "-B", "-c", probe])
run(["/usr/bin/python3", "-B", "-c", "import json,pathlib; print(json.dumps([json.loads(p.read_text()) for p in pathlib.Path('/home/scg-hp2/miniforge3/envs/ros_ai/conda-meta').glob('*.json')],ensure_ascii=False))"])
