"""Where finished videos and generated clips are kept.

On Railway: an S3-compatible bucket (``VIDEO_BUCKET`` with the standard
``AWS_ENDPOINT_URL``, ``AWS_ACCESS_KEY_ID`` and ``AWS_SECRET_ACCESS_KEY``).
Preview links are pre-signed and expire after ``video.preview_days``.
Without a bucket (local runs, tests) files go to ``out/video_store``.
"""

from __future__ import annotations

import shutil
from pathlib import Path

from .config import ROOT, Settings


class LocalStore:
    def __init__(self, root: Path):
        self.root = root

    def put(self, path: Path, key: str) -> None:
        dest = self.root / key
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(path, dest)

    def get(self, key: str, path: Path) -> bool:
        src = self.root / key
        if not src.exists():
            return False
        path.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(src, path)
        return True

    def url(self, key: str) -> str:
        return (self.root / key).as_uri()


class S3Store:
    def __init__(self, settings: Settings, bucket: str, client=None):
        import boto3

        self.bucket = bucket
        self.expires = int((settings.get("video") or {}).get("preview_days", 7)) * 86400
        self.s3 = client or boto3.client(
            "s3",
            endpoint_url=settings.secret("AWS_ENDPOINT_URL", required=False),
            aws_access_key_id=settings.secret("AWS_ACCESS_KEY_ID", required=False),
            aws_secret_access_key=settings.secret("AWS_SECRET_ACCESS_KEY", required=False),
            region_name=settings.secret("AWS_DEFAULT_REGION", required=False) or "auto",
        )

    def put(self, path: Path, key: str) -> None:
        self.s3.upload_file(str(path), self.bucket, key, ExtraArgs={"ContentType": "video/mp4"})

    def get(self, key: str, path: Path) -> bool:
        try:
            self.s3.head_object(Bucket=self.bucket, Key=key)
        except Exception:
            return False
        path.parent.mkdir(parents=True, exist_ok=True)
        self.s3.download_file(self.bucket, key, str(path))
        return True

    def url(self, key: str) -> str:
        return self.s3.generate_presigned_url("get_object", Params={"Bucket": self.bucket, "Key": key},
                                              ExpiresIn=min(self.expires, 7 * 86400))


def store(settings: Settings):
    bucket = settings.secret("VIDEO_BUCKET", required=False)
    return S3Store(settings, bucket) if bucket else LocalStore(ROOT / "out" / "video_store")
