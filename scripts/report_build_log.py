#!/usr/bin/env python3
"""Expose the final Buildah error when the action's annotation truncates its output."""
import os
from pathlib import Path
import secrets

path = Path(os.environ['COREOS_BUILD_LOG'])
if not path.is_file():
    print('Buildah did not create its log file; inspect the build action error.')
else:
    with path.open('rb') as stream:
        stream.seek(max(0, path.stat().st_size - 16384))
        tail = stream.read().decode(errors='replace')
    # Build output is data, including any strings resembling workflow commands.
    token = secrets.token_hex(24)
    print(f'::stop-commands::{token}')
    print(tail)
    print(f'::{token}::')
    if os.environ.get('BUILD_FAILED') == 'true':
        message = tail[-3000:].replace('%', '%25').replace('\r', '%0D').replace('\n', '%0A')
        print(f'::error title=Final CoreOS build error::{message}')
