import hashlib
import json
import pytest
from agentruntime_core.dev.relational_store import SQLiteRelationalStoreProvider
from agentruntime_core.providers.relational_store import Migration
from agentruntime_core.tenancy import TenantContext
from agentruntime_core.errors import InvalidError
from agentruntime_worker.personal_config import TaskPersonalConfig, preference
from agentruntime_worker.context.partitions import Partitions
from agentruntime.modelgateway.v1 import model_gateway_pb2 as mg

async def test_personal_config_uses_pinned_submitter_version_and_tenant(tmp_path):
    store=SQLiteRelationalStoreProvider(tmp_path)
    await store.migrate((Migration(1,"personal_test",("CREATE TABLE tasks(task_id TEXT PRIMARY KEY,submitted_by TEXT,personal_config_pin TEXT)","CREATE TABLE registry_entries(kind INTEGER,name TEXT,version INTEGER,document TEXT)")),))
    raw=json.dumps({"instructions":"prefer concise","files":[]})
    digest=hashlib.sha256(raw.encode()).hexdigest()
    pin=json.dumps({"version":"user/v1","digest":digest})
    db=store.bind(TenantContext(tenant_id="t1"))
    async with db.transaction() as tx:
        for task_id,pinned in [("t",pin),("none","none")]:
            await tx.execute("INSERT INTO tasks VALUES ($1,'alice',$2)",(task_id,pinned))
        await tx.execute("INSERT INTO registry_entries VALUES (8,'~alice/personal-config',1,$1)",(raw,))
        await tx.execute("INSERT INTO registry_entries VALUES (8,'~alice/personal-config',2,$1)",(json.dumps({"instructions":"new"}),))
        await tx.execute("INSERT INTO registry_entries VALUES (8,'~bob/personal-config',1,$1)",(json.dumps({"instructions":"private bob"}),))
    loader=TaskPersonalConfig(store)
    got=await loader.load("t1","t")
    assert got.instructions=="prefer concise" and got.digest==digest
    assert (await loader.load("t1","none")).instructions==""
    assert (await loader.load("t2","t")).instructions==""
    messages=Partitions(system=["platform"],profile=[preference(got.instructions)]).to_messages()
    assert got.instructions not in messages[0].text
    assert messages[1].role==mg.CHAT_ROLE_USER and got.instructions in messages[1].text
    async with db.transaction() as tx:
        await tx.execute("UPDATE registry_entries SET document='{}' WHERE name='~alice/personal-config' AND version=1")
    with pytest.raises(InvalidError,match="personal_config_digest_mismatch"):
        await loader.load("t1","t")
    await store.aclose()
