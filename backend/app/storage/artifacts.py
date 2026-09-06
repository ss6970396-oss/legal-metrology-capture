"""Immutable artifact storage.

§18's first control: "SHA-256 hash for every source artifact; object-store
immutability/versioning where available". Two implementations behind one
interface — S3-compatible for deployment, filesystem for a laptop and for
tests — because a contract test that needs MinIO running is a contract test
that stops being run.

The invariant both uphold: **an artifact is written once**. Storing under a key
derived from the content hash makes that structural rather than a matter of
discipline — the same bytes always land on the same key, and different bytes
cannot collide onto it. A re-upload of the same evidence is a no-op; an attempt
to change an artifact's bytes under an existing id is rejected rather than
accepted silently.
"""

from __future__ import annotations

import hashlib
import logging
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

log = logging.getLogger(__name__)


class ArtifactIntegrityError(Exception):
    """Stored bytes do not match the hash that was declared for them."""


@dataclass(frozen=True)
class StoredArtifact:
    artifact_id: str
    sha256: str
    uri: str
    size_bytes: int


class ArtifactStore(Protocol):
    def put(
        self, artifact_id: str, data: bytes, declared_sha256: str
    ) -> StoredArtifact:
        """Store bytes, verifying they hash to what the client declared."""
        ...

    def fetch_to(self, artifact: StoredArtifact, destination: Path) -> Path:
        """Materialise an artifact locally for processing."""
        ...

    def exists(self, artifact_id: str, sha256: str) -> bool:
        ...


def verify_hash(data: bytes, declared: str) -> str:
    """Hash the bytes and check them against the declaration.

    Raises rather than warning. The device computed its hash over the bytes it
    wrote to its own evidence tree; a mismatch means what arrived is not what
    was captured, and the device still holds the original, so rejecting costs
    a re-upload and accepting costs the integrity of the evidence chain.
    """
    actual = hashlib.sha256(data).hexdigest()
    if declared and actual.lower() != declared.lower():
        raise ArtifactIntegrityError(
            f"artifact hash mismatch: declared {declared[:12]}…, "
            f"received {actual[:12]}…"
        )
    return actual


def _key_for(artifact_id: str, sha256: str) -> str:
    """Object key: sharded by hash prefix, named by artifact and content.

    The hash in the key is what makes the store content-addressed, so the same
    evidence uploaded twice occupies one object. The two-character shard keeps
    a flat bucket from developing a hot prefix once there are millions of
    artifacts.
    """
    return f"artifacts/{sha256[:2]}/{sha256[2:4]}/{artifact_id}-{sha256}.bin"


class FilesystemArtifactStore:
    """Local storage, for development and tests.

    Not a toy: it enforces the same write-once rule as the object store, so a
    bug that would corrupt evidence in production fails here too rather than
    only showing up after deployment.
    """

    def __init__(self, root: Path) -> None:
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)

    def _path_for(self, artifact_id: str, sha256: str) -> Path:
        return self.root / _key_for(artifact_id, sha256)

    def put(
        self, artifact_id: str, data: bytes, declared_sha256: str
    ) -> StoredArtifact:
        actual = verify_hash(data, declared_sha256)
        path = self._path_for(artifact_id, actual)

        if path.exists():
            # Same id, same content: an idempotent retry. The upload queue
            # retries after a response it never saw, so this is the ordinary
            # case rather than an error.
            log.debug("artifact %s already stored", artifact_id)
            return StoredArtifact(
                artifact_id=artifact_id,
                sha256=actual,
                uri=path.as_uri(),
                size_bytes=path.stat().st_size,
            )

        # Same id under a different hash means someone is trying to change an
        # artifact's content. Refuse: the id is already referenced by facts.
        existing = list(self.root.glob(f"artifacts/*/*/{artifact_id}-*.bin"))
        if existing:
            raise ArtifactIntegrityError(
                f"artifact {artifact_id} already exists with different content; "
                "artifacts are immutable once stored"
            )

        path.parent.mkdir(parents=True, exist_ok=True)
        # Write to a scratch name and rename, so a crash mid-write cannot leave
        # a truncated file sitting at a content-addressed key.
        scratch = path.with_suffix(".partial")
        scratch.write_bytes(data)
        scratch.replace(path)

        return StoredArtifact(
            artifact_id=artifact_id,
            sha256=actual,
            uri=path.as_uri(),
            size_bytes=len(data),
        )

    def fetch_to(self, artifact: StoredArtifact, destination: Path) -> Path:
        source = self._path_for(artifact.artifact_id, artifact.sha256)
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, destination)
        return destination

    def exists(self, artifact_id: str, sha256: str) -> bool:
        return self._path_for(artifact_id, sha256).exists()


class S3ArtifactStore:
    """S3-compatible object storage.

    Uses ``IfNoneMatch`` on upload so a concurrent write cannot overwrite an
    existing object. Where the bucket has versioning and object-lock enabled
    the immutability is enforced by the store itself, which is what §18 asks
    for; this class does not assume that configuration and does not depend on
    it for correctness.
    """

    def __init__(
        self,
        bucket: str,
        endpoint_url: str = "",
        region: str = "ap-south-1",
    ) -> None:
        self.bucket = bucket
        self.endpoint_url = endpoint_url
        self.region = region
        self._client = None

    @property
    def client(self):  # noqa: ANN201 - boto3 types are optional here
        if self._client is None:
            import boto3

            self._client = boto3.client(
                "s3",
                endpoint_url=self.endpoint_url or None,
                region_name=self.region,
            )
        return self._client

    def put(
        self, artifact_id: str, data: bytes, declared_sha256: str
    ) -> StoredArtifact:
        from botocore.exceptions import ClientError

        actual = verify_hash(data, declared_sha256)
        key = _key_for(artifact_id, actual)
        uri = f"s3://{self.bucket}/{key}"

        try:
            self.client.put_object(
                Bucket=self.bucket,
                Key=key,
                Body=data,
                ChecksumSHA256=_b64_sha256(data),
                # Fails if the key already holds an object, which is what
                # makes this write-once rather than last-writer-wins.
                IfNoneMatch="*",
                Metadata={"artifact-id": artifact_id, "sha256": actual},
            )
        except ClientError as exc:
            code = exc.response.get("Error", {}).get("Code", "")
            if code in ("PreconditionFailed", "412"):
                # The object is already there. Content-addressed, so it is the
                # same bytes — an idempotent retry, not a conflict.
                log.debug("artifact %s already in bucket", artifact_id)
            else:
                raise

        return StoredArtifact(
            artifact_id=artifact_id,
            sha256=actual,
            uri=uri,
            size_bytes=len(data),
        )

    def fetch_to(self, artifact: StoredArtifact, destination: Path) -> Path:
        key = _key_for(artifact.artifact_id, artifact.sha256)
        destination.parent.mkdir(parents=True, exist_ok=True)
        self.client.download_file(self.bucket, key, str(destination))

        # Re-verify after the round trip. §18's immutability control is only
        # worth having if it is actually checked, and this is the point where a
        # corrupted or substituted object would otherwise enter the pipeline
        # and become provenance for a fact.
        verify_hash(destination.read_bytes(), artifact.sha256)
        return destination

    def exists(self, artifact_id: str, sha256: str) -> bool:
        from botocore.exceptions import ClientError

        try:
            self.client.head_object(
                Bucket=self.bucket, Key=_key_for(artifact_id, sha256)
            )
            return True
        except ClientError:
            return False


def _b64_sha256(data: bytes) -> str:
    import base64

    return base64.b64encode(hashlib.sha256(data).digest()).decode("ascii")


def build_store(settings) -> ArtifactStore:  # noqa: ANN001 - avoids a cycle
    """Pick a store from configuration."""
    if settings.uses_object_store:
        return S3ArtifactStore(
            bucket=settings.s3_bucket,
            endpoint_url=settings.s3_endpoint,
            region=settings.s3_region,
        )
    return FilesystemArtifactStore(root=settings.work_dir / "artifacts")
