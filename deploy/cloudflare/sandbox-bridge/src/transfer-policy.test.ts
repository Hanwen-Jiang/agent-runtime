import { CHECKPOINT_BYTES, MAX_BATCH_GRANTS, TransferTracker, r2BindingKey, validTransferSpec } from "./transfer-policy";
const now=1_800_000_000_000;
const base={task_id:"task-1",method:"PUT" as const,size_bytes:4,expires_at_ms:now+120000,target:{url:"https://0123456789abcdef0123456789abcdef.r2.cloudflarestorage.com/bucket/key?signature=opaque",headers:{"Content-Length":"4","If-None-Match":"*","x-amz-checksum-sha256":"a".repeat(43)+"="}}};
function assert(value:boolean,message:string){if(!value)throw Error(message);}
assert(validTransferSpec(base,now),"valid fixed R2 PUT");
assert(validTransferSpec({...base,method:"GET",target:{url:base.target.url}},now),"valid fixed R2 GET");
for(const url of ["http://0123456789abcdef0123456789abcdef.r2.cloudflarestorage.com/b/key","https://r2.cloudflarestorage.com/b/key","https://evil.test/b/key","https://0123456789abcdef0123456789abcdef.r2.cloudflarestorage.com.evil.test/b/key","https://user:secret@0123456789abcdef0123456789abcdef.r2.cloudflarestorage.com/b/key","https://0123456789abcdef0123456789abcdef.r2.cloudflarestorage.com:8443/b/key","https://127.0.0.1/b/key"])
 assert(!validTransferSpec({...base,target:{...base.target,url}},now),"unsafe target accepted");
for(const delta of [{expires_at_ms:now},{expires_at_ms:now+126000},{size_bytes:65*1024*1024},{size_bytes:-1},{method:"POST"}])
 assert(!validTransferSpec({...base,...delta} as typeof base,now),"invalid cap accepted");
for(const key of Object.keys(base.target.headers)){
 const headers={...base.target.headers} as Record<string,string>;delete headers[key];assert(!validTransferSpec({...base,target:{...base.target,headers}},now),"missing signed constraint");
}

// Verify R2 direct binding key parsing & traversal defense.
assert(r2BindingKey("https://0123456789abcdef0123456789abcdef.r2.cloudflarestorage.com/my-bucket/t1/workspaces/w1/blobs/abc?sig=1","my-bucket")==="t1/workspaces/w1/blobs/abc","valid R2 key extracted");
assert(r2BindingKey("https://0123456789abcdef0123456789abcdef.r2.cloudflarestorage.com/other/key","my-bucket")===null,"wrong bucket rejected");
assert(r2BindingKey("https://0123456789abcdef0123456789abcdef.r2.cloudflarestorage.com/my-bucket/../escape","my-bucket")===null,"dotdot segment rejected");
assert(r2BindingKey("https://0123456789abcdef0123456789abcdef.r2.cloudflarestorage.com/my-bucket/a/%2e%2e/b","my-bucket")===null,"encoded dotdot segment rejected");
assert(r2BindingKey(base.target.url,undefined)===null,"unbound bucket returns null");

// Verify in-memory transfer byte tracker checkpointing and sweep accounting.
const tracker = new TransferTracker();
const ac1 = new AbortController();
const id = "sbg-" + "0".repeat(32);
tracker.seedPersisted(id, 0);
const s1 = tracker.register(id, ac1);
assert(tracker.canRecordChunk(id, 3 * (6 << 20), CHECKPOINT_BYTES - 10), "under limit allowed");
assert(tracker.recordChunk(id, s1, 1024) === 0, "small chunk does not trigger checkpoint flush");
assert(tracker.unflushedBytes(id) === 1024 && tracker.inFlightBytes(id) === 1024, "unflushed bytes tracked in memory");
const flushDelta = tracker.recordChunk(id, s1, CHECKPOINT_BYTES);
assert(flushDelta === CHECKPOINT_BYTES + 1024, "crossing threshold triggers single checkpoint flush");
assert(tracker.unflushedBytes(id) === 0 && tracker.inFlightBytes(id) === CHECKPOINT_BYTES + 1024, "flushing bytes still counted against 3x limit");
tracker.endFlush(id, flushDelta, flushDelta);
tracker.recordChunk(id, s1, 512);
const swept = tracker.takeAllUnflushed(id);
assert(swept.delta === 512 && swept.controllers.length === 1 && tracker.unflushedBytes(id) === 0, "revoke sweeps remaining in-memory bytes and controllers");
assert(MAX_BATCH_GRANTS === 64, "batch cap matches Go adapter limit");

console.log("transfer policy and accounting constraints passed");
