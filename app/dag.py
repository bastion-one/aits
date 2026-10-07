"""Content-addressed Merkle-DAG primitive: CIDs, named edges, structural verify.

A node is ``(content, links)``. ``content`` is any canonical-JSON-encodable
value; ``links`` is a map of **named edges** -- ``role -> CID`` for
single-valued roles, ``role -> set of CIDs`` for multi-valued ones. The CID is

    ``sha256(canonical_dumps({"content": content, "links": links}))``

so a node's identity commits to its content, its link targets, *and* the role
names of its edges. Multi-valued roles fold as sets (order-independent,
deduped); roles with no targets are omitted from the map entirely.

Tamper-evidence is structural: a node either hashes to its address and all of
its links resolve, or it does not. :func:`verify` is the single integrity
walk; there are no per-type validators.
"""

from collections.abc import Callable, Iterator, Mapping
from datetime import datetime
from typing import Any

from .canonical_json import CanonicalEncodingError, canonical_dumps
from .hashing import CHECKSUM_BYTES, sha256_hash
from .result import Err, Ok, VerificationResult

Links = dict[str, "bytes | frozenset[bytes]"]
"""Normalized named-edge map: role -> CID, or role -> frozenset of CIDs."""

Resolver = Callable[[bytes], "tuple[Any, Mapping[str, Any]] | None"]
"""Looks up a CID and returns its ``(content, links)``, or ``None`` if absent."""


def _validate_cid(value: Any, *, role: str) -> bytes:
    if not isinstance(value, bytes) or len(value) != CHECKSUM_BYTES:
        raise ValueError(f"link {role!r} target is not a {CHECKSUM_BYTES}-byte CID: {value!r}")
    return value


def normalize_links(links: Mapping[str, Any] | None) -> Links:
    """Validate and normalize a named-edge map for hashing and traversal.

    Single-valued roles map to a raw 32-byte CID; multi-valued roles to a
    ``frozenset`` of them. Roles whose value is ``None`` or an empty
    collection are dropped, so "no targets" and "role absent" are the same
    (and hash the same).
    """
    normalized: Links = {}
    for role, value in (links or {}).items():
        if not isinstance(role, str) or not role:
            raise ValueError(f"link role must be a non-empty string: {role!r}")
        if value is None:
            continue
        if isinstance(value, (set, frozenset, list, tuple)):
            targets = frozenset(_validate_cid(v, role=role) for v in value)
            if targets:
                normalized[role] = targets
        else:
            normalized[role] = _validate_cid(value, role=role)
    return normalized


def encode_node(content: Any, links: Mapping[str, Any] | None = None) -> bytes:
    """Return the node's canonical bytes: ``canonical_dumps({content, links})``."""
    return canonical_dumps({"content": content, "links": normalize_links(links)})


def compute_cid(content: Any, links: Mapping[str, Any] | None = None) -> bytes:
    """Return the node's CID: ``sha256(canonical_dumps({content, links}))``."""
    return sha256_hash(encode_node(content, links))


def iter_link_cids(links: Mapping[str, Any]) -> Iterator[bytes]:
    """Yield every target CID in a named-edge map, role-agnostically."""
    for value in links.values():
        if isinstance(value, (set, frozenset, list, tuple)):
            yield from value
        else:
            yield value


def verify(
    cid: bytes,
    resolve: Resolver,
    verified: set[bytes] | None = None,
) -> VerificationResult:
    """Structurally verify ``cid``: it and every node reachable from it must
    resolve and re-hash to their addresses.

    ``verified`` is a memoization set: CIDs in it are skipped, and every CID
    checked by a call that returns ``Ok`` is added to it. Because nodes are
    immutable and the store is append-only, the set is a durable cache -- a
    full audit is O(nodes + edges) and an append re-verifies one node.
    """
    seen: set[bytes] = verified if verified is not None else set()
    checked: set[bytes] = set()
    stack = [cid]
    while stack:
        current = stack.pop()
        if current in seen or current in checked:
            continue
        try:
            node = resolve(current)
            if node is None:
                return Err(
                    record_kind="node",
                    record_key=current.hex(),
                    property_violated="dangling_link",
                    expected=current.hex(),
                    actual=None,
                    message=f"dangling link: no node found at CID {current.hex()}",
                )
            content, links = node
            normalized = normalize_links(links)
            recomputed = compute_cid(content, normalized)
        except CanonicalEncodingError as exc:
            # Stored content the encoder rejects (for example, nested past
            # MAX_DEPTH) was altered after it was recorded. Report it like any
            # other tamper finding. The exception text can quote stored
            # values, so only its type goes into the message. This handler
            # must precede the ValueError one: CanonicalEncodingError is a
            # ValueError.
            return Err(
                record_kind="node",
                record_key=current.hex(),
                property_violated="canonical_encoding_error",
                expected="content that can be canonically encoded",
                actual=None,
                message=(
                    f"node at {current.hex()} cannot be canonically encoded "
                    f"({type(exc).__name__}), so its CID cannot be recomputed"
                ),
            )
        except (ValueError, TypeError) as exc:
            return Err(
                record_kind="node",
                record_key=current.hex(),
                property_violated="malformed_node",
                expected="a node that can be re-assembled for hashing",
                actual=str(exc),
                message=f"node {current.hex()} could not be re-assembled for hashing: {exc}",
            )
        if recomputed != current:
            return Err(
                record_kind="node",
                record_key=current.hex(),
                property_violated="cid_mismatch",
                expected=current.hex(),
                actual=recomputed.hex(),
                message=(
                    f"tampered: node at {current.hex()} no longer hashes to its address "
                    f"(recomputed {recomputed.hex()})"
                ),
            )
        checked.add(current)
        stack.extend(t for t in iter_link_cids(normalized) if t not in seen and t not in checked)
    seen.update(checked)
    return Ok()


def commit_entry_hash(cid: bytes, recorded_at: datetime, prev: bytes, principal: str) -> bytes:
    """Hash of one commit-log entry, chaining ``prev`` (the previous entry's
    hash, or ``ZERO_PREV`` at the head of the log). ``principal`` is the identity
    AITS authenticated for the request, so rewriting it breaks the chain."""
    return sha256_hash(
        canonical_dumps(
            {"cid": cid, "recorded_at": recorded_at, "prev": prev, "principal": principal}
        )
    )
