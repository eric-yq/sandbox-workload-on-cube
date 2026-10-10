"""Best-effort uploader: writes to local disk, then mirrors to S3.

Uses boto3's default credential chain (env, ~/.aws, IMDS instance-role).
Failures are logged but never abort the benchmark.
"""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from typing import Any

import boto3
from botocore.exceptions import BotoCoreError, ClientError

log = logging.getLogger(__name__)


class ResultStore:
    def __init__(self, results_dir: str, bucket: str, prefix: str, region: str) -> None:
        self.local_root = Path(results_dir)
        self.local_root.mkdir(parents=True, exist_ok=True)
        self.bucket = bucket
        self.prefix = prefix.strip("/")
        self.s3 = None
        if bucket:
            try:
                self.s3 = boto3.client("s3", region_name=region)
            except (BotoCoreError, ClientError) as e:
                log.warning("S3 client init failed, will only write local: %s", e)

    def write_json(self, rel_path: str, data: dict[str, Any]) -> Path:
        local = self.local_root / rel_path
        local.parent.mkdir(parents=True, exist_ok=True)
        local.write_text(json.dumps(data, indent=2, ensure_ascii=False))
        self._upload(local, rel_path)
        return local

    def write_text(self, rel_path: str, content: str) -> Path:
        local = self.local_root / rel_path
        local.parent.mkdir(parents=True, exist_ok=True)
        local.write_text(content)
        self._upload(local, rel_path)
        return local

    def _upload(self, local: Path, rel_path: str) -> None:
        if not self.s3 or not self.bucket:
            return
        key = f"{self.prefix}/{rel_path}" if self.prefix else rel_path
        try:
            self.s3.upload_file(str(local), self.bucket, key)
            log.info("uploaded s3://%s/%s", self.bucket, key)
        except (BotoCoreError, ClientError, OSError) as e:
            log.warning("S3 upload failed for %s: %s", key, e)
