#!/usr/bin/env python3
"""Complete the two official model installations and bounded CPU/ROS checks."""
from run_logged import run

for tag in ['4b-q4_K_M','8b-q4_K_M']:
    commands = [
        ['python3','tools/physical_ai/cache_registry_model.py','--tag',tag],
        ['python3','tools/physical_ai/ollama_check.py','pull','--model','qwen3:'+tag],
        ['python3','tools/physical_ai/ollama_check.py','bench','--model','qwen3:'+tag],
    ]
    for command in commands:
        code = run(command,timeout=1200)
        if code:
            raise SystemExit(code)
raise SystemExit(run(['python3','tools/physical_ai/ollama_check.py','bench','--model','qwen2.5:3b'],timeout=240))
