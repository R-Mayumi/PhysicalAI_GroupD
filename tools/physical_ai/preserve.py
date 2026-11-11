#!/usr/bin/env python3
"""Hash existing code/config/logs; verify them after isolated environment setup."""
import argparse
import hashlib
import json
from pathlib import Path
from run_logged import ROOT

DEST = ROOT / "docs/environment_audit/preserved_files.json"
HOME_DIR = Path('/home/scg-hp2')


def digest(path):
    result = hashlib.sha256()
    with path.open('rb') as stream:
        for block in iter(lambda:stream.read(1024*1024), b''):
            result.update(block)
    return result.hexdigest()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--verify', action='store_true')
    args = parser.parse_args()
    if args.verify:
        original = json.loads(DEST.read_text())
        changed = [name for name, sha in original.items()
                   if not Path(name).is_file() or digest(Path(name)) != sha]
        out = {'checked': len(original), 'changed_or_missing': changed,
               'status': 'PASS' if not changed else 'FAIL'}
        (DEST.parent / 'preservation_result.json').write_text(json.dumps(out, indent=2)+'\n')
        print(json.dumps(out))
        return int(bool(changed))
    if DEST.exists():
        raise SystemExit('Baseline exists; refusing to replace it.')
    files = set()
    for folder in [ROOT, HOME_DIR/'ros_ai_workspace']:
        files.update(p for p in folder.iterdir() if p.is_file())
    for folder in [ROOT/'.venv', ROOT/'vendor', ROOT/'docs/research', ROOT/'make_code',
                   HOME_DIR/'ros_ai_workspace/unitree_sdk2_python',
                   HOME_DIR/'miniforge3/envs/ros_ai/conda-meta',
                   HOME_DIR/'miniforge3/envs/ros_ai/etc/conda']:
        files.update(p for p in folder.rglob('*') if p.is_file() and '.git' not in p.parts
                     and '__pycache__' not in p.parts)
    files.add(ROOT/'docs/Go2_外部プログラム利用機能調査.md')
    files.update(HOME_DIR/p for p in ['.bashrc', '.profile'])
    DEST.parent.mkdir(parents=True, exist_ok=True)
    out = {str(p): digest(p) for p in sorted(files)}
    DEST.write_text(json.dumps(out, ensure_ascii=False, indent=2)+'\n')
    print(json.dumps({'hashed_files':len(out), 'destination':str(DEST)}))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
