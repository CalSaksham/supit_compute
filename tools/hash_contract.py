"""Regenerate web/contract.json from web/contract.yaml.

The fingerprint is SHA-256 over the contract serialized as compact JSON with
sorted keys, matching `canonical()` in web/app.js.

Usage: python3 tools/hash_contract.py
"""
import hashlib
import json
from pathlib import Path

import yaml

WEB = Path(__file__).resolve().parent.parent / "web"


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def main():
    contract = yaml.safe_load((WEB / "contract.yaml").read_text())
    digest = hashlib.sha256(canonical(contract).encode()).hexdigest()
    (WEB / "contract.json").write_text(json.dumps({"contract": contract, "hash": digest}))
    print(digest)


if __name__ == "__main__":
    main()
