"""Build a private runtime artifact; never put its output under static/ or in Git."""
import argparse
import hashlib
import json
from pathlib import Path
import re

from learning_library import FILES, LibraryUnavailable, MAX_BYTES, parse, validate


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source', type=Path, required=True, help='Private knowledge-base/data directory')
    parser.add_argument('--output', type=Path, required=True, help='New private runtime directory')
    parser.add_argument('--commit', required=True, help='Exact reviewed source commit, 40 lowercase hex characters')
    args = parser.parse_args()
    if not re.fullmatch('[0-9a-f]{40}', args.commit):
        parser.error('A full reviewed commit is required.')
    app_root = Path(__file__).resolve().parent
    output = args.output.resolve()
    if output == app_root or app_root in output.parents:
        parser.error('Private output must be outside the application checkout and static assets.')
    if args.output.exists():
        parser.error('Output must be a new directory.')
    raws = {}
    for filename in FILES:
        with (args.source / filename).open('rb') as handle:
            raw = handle.read(MAX_BYTES + 1)
        parse(raw)
        raws[filename] = raw
    validate({key: parse(raws[filename]) for filename, key in FILES.items()})
    args.output.mkdir(parents=True, mode=0o700)
    hashes = {}
    for filename, raw in raws.items():
        target = args.output / filename
        target.write_bytes(raw); target.chmod(0o600)
        hashes[filename] = hashlib.sha256(raw).hexdigest()
    manifest = args.output / 'manifest.json'
    manifest.write_text(json.dumps({'commit': args.commit, 'files': hashes}, indent=2) + '\n')
    manifest.chmod(0o600)
    print('Private runtime artifact created and validated. Keep it outside public source and static assets.')


if __name__ == '__main__':
    try:
        main()
    except (OSError, LibraryUnavailable):
        raise SystemExit('Could not package the private library; check its source and destination.') from None
