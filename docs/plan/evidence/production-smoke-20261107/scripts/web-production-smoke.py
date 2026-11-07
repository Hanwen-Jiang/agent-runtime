#!/usr/bin/env python3
"""Zero-model acceptance for a deployed coding-web + Runtime.

Use two pre-authenticated isolated test users. Credentials are read from 0600
files and never logged. This script never submits Tasks, pushes Git or calls a
model. Public repository clone, private config versions and owned workspaces are
real. Created projects are removed and the new personal default is retired.
Immutable config revisions and operation audit records intentionally remain.
"""
from __future__ import annotations
import argparse
import http.cookiejar
import json
import os
from pathlib import Path
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid


class Client:
    def __init__(self, base: str, cookie_file: Path):
        if cookie_file.stat().st_mode & 0o077:
            raise ValueError("cookie file must be private (chmod 600)")
        value = cookie_file.read_text().strip()
        if value.startswith("cw_token="):
            value = value.removeprefix("cw_token=").split(";", 1)[0]
        if not value or any(c.isspace() for c in value):
            raise ValueError("expected raw cw_token cookie value")
        parsed = urllib.parse.urlsplit(base)
        if parsed.scheme != "https" or not parsed.hostname:
            raise ValueError("production base must be https")
        self.base = base.rstrip("/")
        self.operation_event = None
        jar = http.cookiejar.CookieJar()
        jar.set_cookie(http.cookiejar.Cookie(0, "cw_token", value, None, False,
            parsed.hostname, False, False, "/", True, True, None, True, None, None, {}, False))
        self.opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar))

    def request(self, method: str, path: str, body=None, key=None):
        headers = {"X-Coding-Web": "1", "Accept": "application/json", "User-Agent": "agentruntime-goal-acceptance/1.0"}
        if key:
            headers.update({"X-Coding-Async": "1", "Idempotency-Key": key})
        data = None if body is None else json.dumps(body).encode()
        if data is not None:
            headers["Content-Type"] = "application/json"
        request = urllib.request.Request(self.base + path, data=data, headers=headers, method=method)
        start = time.monotonic()
        try:
            response = self.opener.open(request, timeout=25)
        except urllib.error.HTTPError as error:
            response = error
        with response:
            payload = response.read()
            try:
                parsed = json.loads(payload) if payload else None
            except json.JSONDecodeError:
                raise RuntimeError(f"non-JSON response for {method} {path} ({response.status})") from None
            return response.status, parsed, dict(response.headers), time.monotonic() - start

    def get(self, path):
        status, data, _, _ = self.request("GET", path)
        if status != 200:
            raise RuntimeError(f"GET {path}: {status}")
        return data

    def mutation(self, method, path, body, *, repeated=False, other=None):
        key = uuid.uuid4().hex
        status, started, _, elapsed = self.request(method, path, body, key)
        if status != 202 or not isinstance(started, dict) or not started.get("operation_id"):
            raise RuntimeError(f"async admission {method} {path}: {status}")
        if elapsed >= 10:
            raise RuntimeError(f"async admission exceeded 10 seconds: {elapsed:.2f}")
        if repeated:
            again_status, again, _, _ = self.request(method, path, body, key)
            assert again_status == 202 and again["operation_id"] == started["operation_id"], "duplicate request changed operation"
        url = started["status_url"]
        if self.operation_event:
            self.operation_event({"method":method,"path":path,"operation_id":started["operation_id"],"status":"RUNNING","admission_seconds":elapsed})
        if other is not None:
            assert other.request("GET", url)[0] == 404, "operation exposed to another owner"
        deadline = time.monotonic() + 1800
        while time.monotonic() < deadline:
            result = self.get(url)
            if result["status"] in ("COMPLETED", "FAILED"):
                # Terminal query must remain stable independently of the original connection.
                assert self.get(url) == result
                if self.operation_event:
                    self.operation_event({"method":method,"path":path,"operation_id":started["operation_id"],"status":result["status"],"http_status":result["http_status"]})
                return result["http_status"], result["result"], started["operation_id"], elapsed
            assert result["status"] == "RUNNING"
            time.sleep(.5)
        raise TimeoutError("operation did not reach terminal status within 30 minutes")


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--base", required=True)
    p.add_argument("--tenant", required=True)
    p.add_argument("--user", required=True)
    p.add_argument("--other-user", required=True)
    p.add_argument("--cookie-file", type=Path, required=True)
    p.add_argument("--other-cookie-file", type=Path, required=True)
    p.add_argument("--repo-url", required=True, help="Small, public, read-only test repository; no credentials in URL")
    p.add_argument("--dirty-fixture", type=Path, help="Owned helper that writes one test file through Sandbox Service; no Task submission")
    p.add_argument("--expected-worker-image")
    p.add_argument("--out", type=Path, required=True)
    args = p.parse_args()
    if not args.tenant.startswith(("goal-", "eval-", "gh-goalaccept-")) or args.user == args.other_user:
        raise ValueError("use an isolated goal-/eval- tenant and two distinct test users")
    remote = urllib.parse.urlsplit(args.repo_url)
    if remote.scheme != "https" or remote.username or remote.password or remote.query or remote.fragment:
        raise ValueError("repository URL must be credential-free HTTPS")
    if args.out.exists():
        raise ValueError("new evidence output path required")
    if args.dirty_fixture:
        if args.dirty_fixture.resolve() != Path('/root/proj/agent-projs/.goal-tools/web-workspace-dirty-fixture.py') or not args.expected_worker_image:
            raise ValueError('only the prepared isolated dirty-fixture helper is supported')
    owner = Client(args.base, args.cookie_file)
    other = Client(args.base, args.other_cookie_file)
    for client, user in ((owner, args.user), (other, args.other_user)):
        identity = client.get("/api/me")
        assert identity["tenant"] == args.tenant and identity["user"] == user, "unexpected authenticated owner"
    baseline = owner.get("/api/personal-config")
    assert baseline.get("available", True) and not baseline.get("config"), "test owner must have no existing personal package"
    other_baseline = other.get("/api/personal-config")
    assert other_baseline.get("available", True) and not other_baseline.get("config"), "second test owner must be empty"
    results = {"base": args.base, "tenant": args.tenant, "started_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "model_cost_usd": 0, "status":"running", "checks": [], "cleanup": [], "operations":[]}
    projects = []
    personal_created = False
    def checkpoint():
        args.out.parent.mkdir(parents=True,exist_ok=True)
        raw=json.dumps(results,indent=2)+'\n'
        fd=os.open(args.out,os.O_WRONLY|os.O_CREAT|os.O_TRUNC,0o600)
        with os.fdopen(fd,'w') as stream:
            stream.write(raw);stream.flush();os.fsync(stream.fileno())
    def operation_event(event):
        results['operations'].append(event);checkpoint()
    owner.operation_event=operation_event
    def check(label, **data):
        results["checks"].append({"name": label, **data})
        checkpoint()
    try:
        document = {"instructions": "Goal acceptance: use concise output.", "files": [{"path": ".config/goal-acceptance/preferences.toml", "content": "theme='light'\n"}], "defaults": {}}
        status, first, _, _ = owner.request("PUT", "/api/personal-config", {"document": document})
        assert status == 200 and first["config"]["version"].startswith("user/v")
        personal_created = True
        v1, digest1 = first["config"]["version"], first["config"]["digest"]
        document["instructions"] += " Preserve explicit empty tool selections."
        status, second, _, _ = owner.request("PUT", "/api/personal-config", {"document": document, "expected_version": v1})
        assert status == 200 and second["config"]["version"] != v1 and second["config"]["digest"] != digest1
        status, old, headers, _ = owner.request("GET", "/api/personal-config?version=" + urllib.parse.quote(v1, safe=""))
        assert status == 200 and old["config"]["digest"] == digest1 and headers.get("Cache-Control") == "no-store"
        assert not other.get("/api/personal-config").get("config")
        check("personal_versions_private", versions=[v1, second["config"]["version"]], digests=[digest1, second["config"]["digest"]])
        for content in ("//registry.example/:_auth=ZmFrZTpwYXNz", "Authorization: Bearer fake-goal-secret", "token=github_pat_" + "a"*40):
            status, _, _, _ = owner.request("PUT", "/api/personal-config", {"document": {"files": [{"path": ".npmrc", "content": content}]}, "expected_version": second["config"]["version"]})
            assert status == 400, "plaintext credential format accepted"
        check("known_credential_formats_rejected")
        status, project, operation, seconds = owner.mutation("POST", "/api/projects", {"name": "Goal multi acceptance " + uuid.uuid4().hex[:8], "layout": "WORKSPACE_LAYOUT_MULTI"}, repeated=True, other=other)
        assert status == 201
        pid = project["id"]
        projects.append(pid)
        base = "/api/projects/" + pid
        check("async_create_and_reconnect", project_id=pid, operation_id=operation, admission_seconds=seconds)
        for name, access in (("source", "REPO_ACCESS_WRITE"), ("reference", "REPO_ACCESS_READ")):
            status, attached, _, _ = owner.mutation("POST", base + "/repos", {"repo": {"name": name, "path": name, "url": args.repo_url, "access": access}})
            assert status == 201, ("attach failed", name, status, attached)
        repos = owner.get(base + "/repos")["repos"]
        assert [r["name"] for r in repos] == ["reference", "source"]
        status, _, _, _ = owner.mutation("POST", base + "/repos", {"repo": {"name": "bad", "path": "../escape", "url": args.repo_url}})
        assert status == 400
        assert len(owner.get(base + "/repos")["repos"]) == 2
        check("attach_list_and_invalid_path")
        status, session, _, _ = owner.mutation("POST", base + "/sessions", {"title": "Zero-model acceptance", "personal_config": {"skill_names": [], "mcp_connection_ids": []}})
        assert status == 201
        sid = session["id"]
        sbase = base + "/sessions/" + sid
        assert session["personal_config"] == {"skill_names": [], "mcp_connection_ids": []}
        assert [r["name"] for r in owner.get(sbase + "/repos")["repos"]] == ["reference", "source"]
        bob_sessions = other.get("/api/sessions")
        assert "personal_config" not in next(s for s in bob_sessions if s["id"] == sid)
        status, _, _, _ = owner.mutation("POST", sbase + "/commit?repo=reference", {"message": "must be rejected"})
        assert status == 403
        check("copied_layout_read_only_and_private_selection", session_id=sid, workspace_id=session["workspace_id"])
        status, copied, _, _ = owner.mutation("POST", sbase + "/duplicate", {"title": "Acceptance copy"})
        assert status == 201 and copied["personal_config"] == session["personal_config"]
        assert len(owner.get(base + "/sessions/" + copied["id"] + "/repos")["repos"]) == 2
        check("session_copy")
        status, _, _, _ = owner.mutation("POST", sbase + "/repos/reference/detach", {"discard_changes": False})
        assert status == 200
        assert len(owner.get(sbase + "/repos")["repos"]) == 1
        assert len(owner.get(base + "/repos")["repos"]) == 2
        check("clean_detach_preserves_baseline")
        if args.dirty_fixture:
            fixture_output=args.out.with_suffix('.dirty-fixture.json')
            subprocess.run([sys.executable,str(args.dirty_fixture),'--tenant',args.tenant,'--workspace',session['workspace_id'],
                '--relative-path','source/goal-acceptance-'+uuid.uuid4().hex[:16]+'.txt','--expected-worker-image',args.expected_worker_image,'--out',str(fixture_output)],check=True,capture_output=True,timeout=210)
            status, denied, _, _ = owner.mutation("POST", sbase + "/repos/source/detach", {})
            assert status == 409, ("dirty detach was not refused", status, denied)
            assert len(owner.get(sbase + "/repos")["repos"]) == 1
            status, discarded, _, _ = owner.mutation("POST", sbase + "/repos/source/detach", {"discard_changes":True})
            assert status == 200, ("explicit discard failed", status, discarded)
            assert len(owner.get(sbase + "/repos")["repos"]) == 0
            assert len(owner.get(base + "/repos")["repos"]) == 2
            assert len(owner.get(base + "/sessions/" + copied["id"] + "/repos")["repos"]) == 2
            check("dirty_detach_requires_explicit_discard",fixture_evidence=str(fixture_output),default_status=409,discard_status=200)
        results["status"] = "passed"
    except BaseException as error:
        results["status"] = "failed"
        results["error"] = type(error).__name__ + ": " + str(error)
        raise
    finally:
        for pid in projects:
            try:
                status, _, operation, _ = owner.mutation("DELETE", "/api/projects/" + pid, {})
                results["cleanup"].append({"project_id": pid, "status": status, "operation_id": operation})
                if status != 200:
                    results["status"] = "cleanup_failed"
            except BaseException as error:
                results["cleanup"].append({"project_id": pid, "error": type(error).__name__})
                results["status"] = "cleanup_failed"
        if personal_created:
            try:
                status, _, _, _ = owner.request("DELETE", "/api/personal-config")
                results["cleanup"].append({"personal_default_retired": status == 200, "immutable_versions_retained": True})
                if status != 200:
                    results["status"] = "cleanup_failed"
            except BaseException as error:
                results["cleanup"].append({"personal_default_retired": False, "error": type(error).__name__})
                results["status"] = "cleanup_failed"
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(json.dumps(results, indent=2) + "\n")
        os.chmod(args.out, 0o600)
    print(json.dumps({"status": results["status"], "evidence": str(args.out), "model_cost_usd": 0}))
    if results["status"] != "passed":
        raise SystemExit(1)

if __name__ == "__main__":
    main()
