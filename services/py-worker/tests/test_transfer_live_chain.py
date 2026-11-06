"""Worker → real Service → real Rust agent → HTTP object storage fault recovery."""
import base64
import hashlib
import threading
from datetime import timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlsplit

import pytest
from agentruntime_core.adapters.s3_object_store import S3ObjectStoreProvider
from agentruntime_core.dev.tool_driver import LocalToolDriverProvider
from agentruntime_core.errors import TransientError
from agentruntime_core.testing import FakeClock
from agentruntime_worker.tools.deliver import register_delivery_tools
from agentruntime_worker.tools.platform import PlatformToolsConfig, workspace_files, register_platform_tools
from .service_setup import Setup


@pytest.fixture
def object_transport():
    state={"data":{},"puts":0,"gets":0,"fail":False}
    class Handler(BaseHTTPRequestHandler):
        def log_message(self,*args):pass
        def do_PUT(self):
            if state["fail"]:self.send_error(503);return
            path=urlsplit(self.path).path
            body=self.rfile.read(int(self.headers["Content-Length"]))
            checksum=self.headers.get("x-amz-checksum-sha256")
            if checksum and checksum!=base64.b64encode(hashlib.sha256(body).digest()).decode():self.send_error(400);return
            if path in state["data"] and self.headers.get("If-None-Match")=="*":self.send_error(412);return
            state["data"][path]=body;state["puts"]+=1
            self.send_response(200);self.send_header("Content-Length","0");self.end_headers()
        def do_HEAD(self):self.get(head=True)
        def do_GET(self):self.get(head=False)
        def get(self,*,head):
            path=urlsplit(self.path).path
            if path not in state["data"]:self.send_error(404);return
            data=state["data"][path]
            self.send_response(200);self.send_header("Content-Length",str(len(data)));self.end_headers()
            if not head:state["gets"]+=1;self.wfile.write(data)
        def do_DELETE(self):
            state["data"].pop(urlsplit(self.path).path,None)
            self.send_response(204);self.end_headers()
    server=ThreadingHTTPServer(("127.0.0.1",0),Handler)
    thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
    endpoint=f"http://127.0.0.1:{server.server_port}"
    yield endpoint,state
    server.shutdown();server.server_close();thread.join()


def setup(tmp_path,object_transport):
    endpoint,state=object_transport
    env={"AR_S3_ENDPOINT":endpoint,"AR_S3_BUCKET":"objects","AR_S3_ACCESS_KEY_ID":"test-access","AR_S3_SECRET_ACCESS_KEY":"test-secret"}
    s=Setup(tmp_path,worker_mount=False,env=env)
    objects=S3ObjectStoreProvider(endpoint=endpoint,bucket="objects",access_key_id="test-access",secret_access_key="test-secret")
    config=PlatformToolsConfig(shell_sandbox="sandbox",workspace_tools=True,session_sandboxes=True)
    return s,objects,config,state


async def test_lost_upload_rpc_then_worker_and_service_restart_deliver_same_bytes(tmp_path,object_transport):
    s,objects,config,state=setup(tmp_path,object_transport)
    root=s.host/"t1"/"w1";root.mkdir(parents=True,exist_ok=True)
    (root/"out.bin").write_bytes(b"original bytes")
    clock=FakeClock()
    def tools():
        driver=LocalToolDriverProvider()
        reader=workspace_files(config,sandboxes=s.broker)
        register_delivery_tools(driver,objects=objects,store=s.store,reader=reader,clock=clock)
        return driver
    call=s.broker.call
    async def lost(method,*args,**kwargs):
        result=await call(method,*args,**kwargs)
        if method=="CommitTransfer":raise TransientError("rpc_response_lost","commit response lost")
        return result
    s.broker.call=lost
    tenant=s.tenant()
    with pytest.raises(TransientError,match="保留同一传输"):
        await tools().bind(tenant).invoke("deliver_file","v1",{"path":"out.bin"},task_id="task-durable",idempotency_key="stable-call")
    assert state["puts"]==1 and state["gets"]==0,"bytes must bypass Worker even when every response is lost"
    async with s.store.bind(tenant).transaction(read_only=True) as tx:
        assert len(await tx.query("SELECT * FROM task_artifact_reservations"))==1
        assert await tx.query("SELECT * FROM task_artifacts")==[]
    s.broker.call=call
    s.svc.restart()
    (root/"out.bin").write_bytes(b"changed after lost response")
    clock.advance(timedelta(minutes=6))
    result=await tools().bind(tenant).invoke("deliver_file","v1",{"path":"out.bin"},task_id="task-durable",idempotency_key="stable-call")
    assert result.output["sha256"]==hashlib.sha256(b"original bytes").hexdigest()
    assert state["puts"]==1 and state["gets"]==0
    assert list(state["data"].values())==[b"original bytes"]
    async with s.store.bind(tenant).transaction(read_only=True) as tx:
        assert await tx.query("SELECT * FROM task_artifact_reservations")==[]
        assert len(await tx.query("SELECT * FROM task_artifacts"))==1


async def test_failed_output_upload_stays_bounded_and_context_read_resumes_without_exec(tmp_path,object_transport):
    s,objects,config,state=setup(tmp_path,object_transport)
    driver=LocalToolDriverProvider()
    register_platform_tools(driver,config,objects=objects,sandboxes=s.broker)
    state["fail"]=True
    tenant=s.tenant()
    result=await driver.bind(tenant).invoke("shell_run","v1",{"argv":["sh","-c","echo once >> calls; head -c 30000 /dev/zero | tr '\\0' X"]},task_id="task-output",idempotency_key="one-command")
    out=result.output
    assert len(out["stdout"])<9000 and out["stdout_pending"]["size_bytes"]==30000
    assert state["gets"]==0
    state["fail"]=False
    recovered=await driver.bind(tenant).invoke("context_read","v1",{"path":out["stdout_pending"]["path"],"offset":4,"max_chars":100},task_id="task-output")
    assert recovered.output["content"]=="X"*100 and recovered.output["total_chars"]==30000
    assert (s.host/"t1"/"w1"/"calls").read_text()=="once\n"
    assert state["puts"]==1 and state["gets"]==1,"only explicit context_read fetches bytes through Worker"
