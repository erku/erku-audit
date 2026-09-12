"""Run only fixed modules. Deploy inside the isolated audit container.

This entry point is NOT an OS sandbox by itself. No shell, eval, imports from
input, network fetches, or subprocess execution are supported.
"""
import argparse
import json
from pathlib import Path
from f916.skills import run


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--input', required=True)
    parser.add_argument('--output', required=True)
    args = parser.parse_args()
    path = Path(args.input)
    if path.stat().st_size>256000: raise ValueError('Input exceeds limit')
    data = json.loads(path.read_text(encoding='utf-8'))
    payload = data.get('input',data)
    print(json.dumps(run(data['skill'],payload.get('target',{}),payload.get('params',{}),Path(args.output))))


if __name__ == '__main__': main()
