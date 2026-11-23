"""Download public, revision-pinned GGUF files; never execute model repository code."""
import json
import os
import sys

from huggingface_hub import snapshot_download

os.environ['HF_HUB_DISABLE_PROGRESS_BARS'] = '1'
request = json.load(sys.stdin)
snapshot_download(repo_id=request['repo'], revision=request['revision'],
    allow_patterns=request['files'], local_dir=request['directory'], token=False)
