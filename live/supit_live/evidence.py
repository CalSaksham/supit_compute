from __future__ import annotations

import base64
import json
from pathlib import Path

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey, Ed25519PublicKey

from .util import canonical, iso, sha256

GENESIS = "0" * 64


class EvidenceLog:
    """Append-only SHA-256 hash chain; every entry is signed with a persistent Ed25519 key."""

    def __init__(self, data_dir: Path):
        data_dir.mkdir(parents=True, exist_ok=True)
        key_path = data_dir / "signing_key.pem"
        if key_path.exists():
            self.key = serialization.load_pem_private_key(key_path.read_bytes(), password=None)
        else:
            self.key = Ed25519PrivateKey.generate()
            key_path.write_bytes(self.key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()))
            key_path.chmod(0o600)
        raw = self.key.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
        self.public_key = base64.b64encode(raw).decode()
        self.path = data_dir / "evidence.jsonl"
        self.entries: list[dict] = []
        if self.path.exists():
            self.entries = [json.loads(line) for line in self.path.read_text().splitlines() if line.strip()]

    def sign(self, doc: dict) -> dict:
        body = {**doc, "signing_algorithm": "Ed25519", "public_key": self.public_key}
        signature = self.key.sign(canonical(body).encode())
        return {**body, "signature": base64.b64encode(signature).decode()}

    def verify_doc(self, doc: dict) -> bool:
        body = {k: v for k, v in doc.items() if k != "signature"}
        try:
            key = Ed25519PublicKey.from_public_bytes(base64.b64decode(doc["public_key"]))
            key.verify(base64.b64decode(doc["signature"]), canonical(body).encode())
            return doc["public_key"] == self.public_key
        except (InvalidSignature, KeyError, ValueError):
            return False

    def record(self, type_: str, payload: dict) -> dict:
        body = {
            "id": len(self.entries) + 1,
            "timestamp": iso(),
            "type": type_,
            "payload": payload,
            "payload_hash": sha256(payload),
            "previous_hash": self.entries[-1]["hash"] if self.entries else GENESIS,
        }
        body["hash"] = sha256(body)
        entry = self.sign(body)
        self.entries.append(entry)
        with self.path.open("a") as f:
            f.write(canonical(entry) + "\n")
        return entry

    def verify(self) -> dict:
        previous = GENESIS
        for e in self.entries:
            body = {k: e[k] for k in ("id", "timestamp", "type", "payload", "payload_hash", "previous_hash")}
            if e["previous_hash"] != previous or e["payload_hash"] != sha256(e["payload"]) or e["hash"] != sha256(body) or not self.verify_doc(e):
                return {"intact": False, "entries": len(self.entries), "broken_at": e["id"]}
            previous = e["hash"]
        return {"intact": True, "entries": len(self.entries), "broken_at": None}
