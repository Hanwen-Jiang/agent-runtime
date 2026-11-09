#!/usr/bin/env python3
"""Explicit build-time model download and byte-exact verification; never run by jobs."""
import argparse
import hashlib
import json
from pathlib import Path
import shutil

parser=argparse.ArgumentParser()
parser.add_argument('--manifest',type=Path,required=True)
parser.add_argument('--output',type=Path,required=True)
parser.add_argument('--verify-only',action='store_true')
args=parser.parse_args()
manifest=json.loads(args.manifest.read_text())
if not args.verify_only:
    import rapidocr
    from huggingface_hub import snapshot_download
    args.output.mkdir(parents=True,exist_ok=True)
    rapid=args.output/'rapidocr';rapid.mkdir(exist_ok=True)
    bundled=Path(rapidocr.__file__).parent/'models'
    for kind in ('det','cls','rec'):
        candidates=list(bundled.glob(f'*{kind}*.onnx'))
        if len(candidates)!=1:raise SystemExit(f'expected exactly one packaged {kind} model')
        shutil.copyfile(candidates[0],rapid/f'{kind}.onnx')
    for repo in manifest['repositories']:
        prefix=repo['directory']+'/'
        patterns=[entry['path'][len(prefix):] for entry in manifest['files'] if entry['path'].startswith(prefix)]
        snapshot_download(repo_id=repo['repo_id'],revision=repo['revision'],local_dir=args.output/repo['directory'],allow_patterns=patterns)
for entry in manifest['files']:
    path=args.output/entry['path']
    if not path.is_file() or path.stat().st_size!=entry['size_bytes'] or hashlib.sha256(path.read_bytes()).hexdigest()!=entry['sha256']:
        raise SystemExit(f'model integrity failed: {entry["path"]}')
shutil.copyfile(args.manifest, args.output/'manifest.json')
print(f'Verified {len(manifest["files"])} immutable model files')
