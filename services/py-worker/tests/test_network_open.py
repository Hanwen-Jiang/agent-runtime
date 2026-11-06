from dataclasses import replace
import pytest
from agentruntime_core.errors import DeniedError, InvalidError
from agentruntime_worker.sandbox.grants import NetworkPolicy, check_request, Grant

@pytest.mark.parametrize("modes,scope,hosts,ports,code", [
    (("deny", "allowlist"), "hosts", ["api.example.com"], [], None),
    (("deny", "allowlist"), "public", [], [], "network_open_not_allowed"),
    (("deny", "open"), "public", [], [], None),
    (("open",), "public", ["api.example.com"], [], "invalid_network_request"),
    (("open",), "hosts", ["api.example.com"], [], "network_allowlist_not_allowed"),
    (("open",), "unknown", [], [], "invalid_network_request"),
    (("open",), "public", [], [25], "port_25"),
    (("allowlist",), "hosts", ["api.example.com"], [25], "port_25"),
])
def test_network_scope_parity(modes, scope, hosts, ports, code):
    policy = NetworkPolicy(allowed=True, modes=modes, hosts=("api.example.com",))
    if code:
        with pytest.raises((DeniedError, InvalidError)) as caught:
            check_request(policy, scope=scope, hosts=hosts, ports=ports, minutes=15)
        assert caught.value.code == code
    else:
        req = check_request(policy, scope=scope, hosts=hosts, ports=ports, minutes=15)
        assert req.scope == scope and req.ports == (80, 443)

def test_legacy_policy_and_grant_default_to_hosts():
    policy = NetworkPolicy.from_policy({"allowed": True, "hosts": ["api.example.com"]})
    assert check_request(policy, hosts=["api.example.com"], minutes=15).scope == "hosts"
    assert Grant.from_row("tenant", dict(grant_id="g", task_id="t", hosts="[]", ports="[]", expires_at="", state="active", created_at="")).scope == "hosts"

@pytest.mark.parametrize("changes,code", [({"allowed": False}, "network_not_allowed"), ({"max_grant_minutes": 15}, "grant_too_long")])
def test_public_inherits_policy_limits(changes, code):
    policy = replace(NetworkPolicy(allowed=True, modes=("open",)), **changes)
    with pytest.raises(DeniedError) as caught:
        check_request(policy, scope="public", minutes=30)
    assert caught.value.code == code

def test_public_grant_decodes_empty_hosts_from_go():
    grant = Grant.from_row("tenant", dict(grant_id="g", task_id="t", scope="public", hosts="[]", ports="[80,443]", expires_at="", state="active", created_at=""))
    assert grant.scope == "public" and grant.hosts == ()
