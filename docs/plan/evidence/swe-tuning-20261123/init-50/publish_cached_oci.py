"""Inside the evaluator worker: publish verified OCI objects with its configured registry client identity."""
import asyncio
import base64
from hashlib import sha256
from http.client import HTTPConnection
import json
import os
import re
import sys
from urllib.parse import urlsplit
from agentruntime_worker.config import WorkerConfig
from agentruntime_worker.tools import _platform_secret

HOST = "ar-agent-runtime-env-registry.agent-runtime.svc:5000"
REPO = "/v2/envs/swebench/env"
DIGEST = re.compile(r"sha256:[0-9a-f]{64}")
input_stream = sys.stdin.buffer

class Registry:
    def __init__(self):
        settings = WorkerConfig.from_env(os.environ)
        credentials = json.loads(asyncio.run(_platform_secret(settings, "sandbox/env-registry")))
        encoded = base64.b64encode((credentials["username"]+":"+credentials["password"]).encode())
        self.headers = {"Authorization": "Basic "+encoded.decode()}

    def request(self, method, url, expected, *, payload=None, length=0, media=None, file_bytes=False):
        connection = HTTPConnection(HOST, timeout=300)
        result_hash = sha256()
        try:
            headers = {**self.headers, "Content-Length":str(length)}
            if media:
                headers["Content-Type"] = media
            if method in ("HEAD","GET"):
                headers["Accept"] = "application/vnd.oci.image.index.v1+json,application/vnd.oci.image.manifest.v1+json,application/vnd.docker.distribution.manifest.v2+json"
            connection.putrequest(method,url)
            for name,value in headers.items():
                connection.putheader(name,value)
            connection.endheaders()
            if file_bytes:
                remaining = length
                while remaining:
                    chunk = input_stream.read(min(65536,remaining))
                    if not chunk:
                        raise EOFError("incomplete cached object")
                    connection.send(chunk)
                    result_hash.update(chunk)
                    remaining -= len(chunk)
            elif payload is not None:
                connection.send(payload)
                result_hash.update(payload)
            response = connection.getresponse()
            response.read()
            if response.status not in expected:
                raise RuntimeError(f"{method} {url.split('?')[0]} returned HTTP {response.status}")
            return response.status,dict((k.lower(),v) for k,v in response.getheaders()),result_hash.hexdigest()
        finally:
            connection.close()

def main():
    mode = sys.argv[1]
    spec = json.loads(input_stream.readline())
    client = Registry()
    if mode=="missing":
        needed=[]
        for desc in spec["objects"]:
            digest=desc["digest"]
            assert DIGEST.fullmatch(digest)
            if desc["is_manifest"]:
                needed.append(digest)
            elif client.request("HEAD",f"{REPO}/blobs/{digest}",{200,404})[0]==404:
                needed.append(digest)
        print(json.dumps(needed))
        return
    assert mode=="publish"
    tag=spec["tag"]
    assert re.fullmatch("[0-9a-f]{32}",tag) and DIGEST.fullmatch(spec["root"])
    for desc in spec["objects"]:
        digest=desc["digest"]
        assert DIGEST.fullmatch(digest)
        if desc["is_manifest"]:
            slot=tag if digest==spec["root"] else digest
            _,headers,verified = client.request("PUT",f"{REPO}/manifests/{slot}",{201},length=desc["size"],
                                              media=desc["mediaType"],file_bytes=True)
        else:
            _,started,_=client.request("POST",REPO+"/blobs/uploads/",{202})
            url=urlsplit(started["location"])
            assert url.netloc in ("",HOST) and url.path.startswith(REPO+"/blobs/uploads/")
            query=url.query+("&" if url.query else "")+"digest="+digest
            _,headers,verified=client.request("PUT",url.path+"?"+query,{201},length=desc["size"],
                                            media="application/octet-stream",file_bytes=True)
        assert headers.get("docker-content-digest")==digest and "sha256:"+verified==digest
    for ref in (tag,spec["root"]):
        _,headers,_=client.request("HEAD",f"{REPO}/manifests/{ref}",{200})
        assert headers["docker-content-digest"]==spec["root"]
    print(json.dumps({"tag":tag,"digest":spec["root"],"registry_verified":True}))
if __name__=="__main__":
    main()
