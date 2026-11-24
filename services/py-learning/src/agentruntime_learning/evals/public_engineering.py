"""Serve reviewed public cards derived from paired local engineering runs."""
import hashlib
import json

# Bind public case IDs and metrics to the reviewed exports. Raw records contain
# source text and execution details and are never served by this reader.
CARD_SHA256 = {
    "engineering-20261123-workspace": "7895692ab54339b47672f070c2745e195ee2838fa42141b1c25f75c78029a62c",
    "engineering-20261123-rag-citation": "ed8f8a502276ff62a4df38b91ec3a860ebf6b6bb50375f0d4c990da4d9ae0efb",
    "engineering-20261123-rag-lookup": "458a74340befbb5ce35c35b6df106550f695ec76796e0d52e03eea8c22c43779",
    "engineering-20261107-sandbox-warmpool": "fdf74682442c0bb147842e980fd533daf1b40de43b1a07c410fbd2f341ad98e3",
    "engineering-20261125-workspace-concurrency": "2c55df128cc15202f826d2ea22955856b4db1ab5d2fec1727e8959dc10e089a9",
}


def project_engineering_card(name, read):
    expected = CARD_SHA256.get(name)
    if expected is None:
        return None
    raw = read(name + "/public-summary.json")
    if hashlib.sha256(raw).hexdigest() != expected:
        raise ValueError("Engineering card differs from reviewed evaluation export")
    card = json.loads(raw)
    if card.get("id") != name:
        raise ValueError("Engineering experiment identity mismatch")
    return card
