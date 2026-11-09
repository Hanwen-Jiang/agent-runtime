# Unified retrieval manual acceptance

Run with the repository `uv sync --package agentruntime-memory --extra test`
environment: `.venv/bin/python docs/plan/evidence/file-engines-m2/retrieval_probe.py`
and `rpc_probe.py`. These use real LlamaIndex components, Runtime SQLite and
VectorStore adapters, actual Knowledge and ModelGateway gRPC transports. The
model itself is a deterministic synthetic embedding/scoring fixture, not a claim
of commercial model retrieval accuracy. ACL revocation, cross tenant/workspace
isolation, citations/anchors, service authentication and deletion fencing are
checked. Temporary paths in the logs contain only generated fixture evidence.
