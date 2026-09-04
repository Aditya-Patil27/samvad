# OWNER: P3 -- see docs/WORK.md. Do not edit if you are not P3.
"""Content-addressed artifact store -- how git works.

Four agents with 200K windows is not an 800K pool. Naive history-passing
means all four hold the SAME tokens, so the effective pool is 200K duplicated
four times. Dedup is the entire win: partition, do not broadcast.

Messages carry refs and summaries. The receiver fetches GET /blob/{hash} only
if it decides it needs the bytes -- and `tokens` tells it the price first.
"""


class BlobStore:
    def put(self, data: bytes, kind: str):
        """-> ArtifactRef with sha256 ref, summary and token count."""
        raise NotImplementedError

    def get(self, ref: str) -> bytes | None:
        raise NotImplementedError

    def has(self, ref: str) -> bool:
        raise NotImplementedError
