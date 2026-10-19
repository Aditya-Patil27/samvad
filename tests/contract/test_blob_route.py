"""Contract: GET /blob/{hash}. Seam between P3's store and P1's app.

The route is P3's, mounted in P1's server.py. It is the other half of
"artifacts travel as refs": the ref and its price ride on the message, and the
bytes cross the wire only when a receiver decides to pay for them.
tests/contract/test_store.py covers the store; this file calls the route over
HTTP, through the same ASGI client test_transport.py uses.
"""
import hashlib

import pytest

from tests.fakes import RecordingInbox
from tests.support import has_message, implemented

pytestmark = pytest.mark.skipif(
    not has_message(),
    reason="week 1 joint task: write protocol.py together first",
)

#: Not valid UTF-8, with a NUL and a CRLF in it: a route that decoded,
#: re-encoded or translated newlines on the way out would change these bytes.
RAW = bytes(range(256)) + b"\r\n\x00\xff"


def _store():
    from samvad.store.blobs import BlobStore

    return BlobStore()


def _app(blobs):
    from samvad.server import make_app

    inbox = RecordingInbox()
    if not implemented(make_app, inbox, blobs=blobs):
        pytest.skip("server.make_app not implemented yet")
    return make_app(inbox, blobs=blobs)


async def _client(app):
    """ASGI client that never touches a real socket."""
    import httpx

    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://testserver",
    )


def _hex(ref: str) -> str:
    return ref.removeprefix("sha256:")


async def test_get_blob_returns_the_stored_bytes_byte_identical():
    store = _store()
    ref = store.put(RAW, "text").ref

    async with await _client(_app(store)) as client:
        response = await client.get(f"/blob/{_hex(ref)}")

    assert response.status_code == 200
    assert response.headers["content-type"] == "application/octet-stream"
    assert response.content == RAW
    assert "sha256:" + hashlib.sha256(response.content).hexdigest() == ref


async def test_get_blob_accepts_the_ref_exactly_as_a_message_carries_it():
    """An ArtifactRef's `ref` is `sha256:<hex>`. A receiver can put it in the
    URL verbatim instead of stripping the prefix first."""
    store = _store()
    ref = store.put(RAW, "text").ref

    async with await _client(_app(store)) as client:
        prefixed = await client.get(f"/blob/{ref}")
        bare = await client.get(f"/blob/{_hex(ref)}")

    assert prefixed.status_code == 200
    assert prefixed.content == bare.content == RAW


@pytest.mark.parametrize("prefix", ["", "sha256:"])
async def test_an_unknown_hash_returns_404_naming_the_ref_it_looked_up(prefix):
    unknown = hashlib.sha256(b"never stored").hexdigest()

    async with await _client(_app(_store())) as client:
        response = await client.get(f"/blob/{prefix}{unknown}")

    assert response.status_code == 404
    assert response.json() == {"error": "unknown blob", "ref": f"sha256:{unknown}"}


@pytest.mark.parametrize("digest", ["not-a-hash", "sha256:"])
async def test_a_malformed_hash_is_a_404_not_a_server_error(digest):
    async with await _client(_app(_store())) as client:
        response = await client.get(f"/blob/{digest}")

    assert response.status_code == 404
    expected = digest if digest.startswith("sha256:") else f"sha256:{digest}"
    assert response.json() == {"error": "unknown blob", "ref": expected}


async def test_a_truncated_hash_does_not_resolve_like_a_git_short_hash():
    """The lookup is exact. A prefix that happens to match a stored blob must
    not hand back bytes the requester never named in full."""
    store = _store()
    ref = store.put(RAW, "text").ref

    async with await _client(_app(store)) as client:
        response = await client.get(f"/blob/{_hex(ref)[:12]}")

    assert response.status_code == 404


async def test_the_same_bytes_put_twice_give_one_ref_and_fetch_once():
    store = _store()
    first = store.put(RAW, "text")
    second = store.put(RAW, "text")
    assert first == second

    async with await _client(_app(store)) as client:
        response = await client.get(f"/blob/{first.ref}")

    assert response.status_code == 200
    assert response.content == RAW
