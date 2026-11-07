import { validTransferSpec } from "./transfer-policy";
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
console.log("transfer policy constraints passed");
