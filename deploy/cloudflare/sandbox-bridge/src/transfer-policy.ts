export const TRANSFER_HOST = "ar-transfer.internal";
export interface TransferSpec {
  task_id: string; method: "GET"|"PUT"; size_bytes: number; expires_at_ms: number;
  target: {url: string; headers?: Record<string,string>; proxy?: string};
}
export interface TransferGrant {spec:TransferSpec; bytes:number; attempts:number; revoked:boolean;}

export function validTransferSpec(s: TransferSpec, now:number):boolean {
  if(!s || typeof s.task_id!=="string" || !/^[A-Za-z0-9_-]{1,255}$/.test(s.task_id) || !["GET","PUT"].includes(s.method) || !Number.isSafeInteger(s.size_bytes) || s.size_bytes<0 || s.size_bytes>64*1024*1024 || !Number.isSafeInteger(s.expires_at_ms) || s.expires_at_ms<=now || s.expires_at_ms>now+125000) return false;
  if(!s.target || typeof s.target.url!=="string" || s.target.url.length>16384 || s.target.proxy) return false;
  let u:URL;try{u=new URL(s.target.url);}catch{return false;}
  // The configured production authority is an account-isolated R2 endpoint.
  if(u.protocol!=="https:" || (u.port&&u.port!=="443") || u.username || u.password || u.hash || !/^[a-f0-9]{32}(?:\.(?:eu|fedramp))?\.r2\.cloudflarestorage\.com$/.test(u.hostname)) return false;
  const headers=s.target.headers??{};
  if(Object.keys(headers).length>32)return false;
  for(const [k,v] of Object.entries(headers))if(!/^[A-Za-z0-9-]+$/.test(k)||typeof v!=="string"||/[\x00-\x1f\x7f]/.test(v)||["host","transfer-encoding","connection"].includes(k.toLowerCase()))return false;
  if(s.method==="PUT") {
    const h=Object.fromEntries(Object.entries(headers).map(([k,v])=>[k.toLowerCase(),v]));
    if(h["content-length"]!==String(s.size_bytes)||h["if-none-match"]!=="*"||!/^[A-Za-z0-9+/]{43}=$/.test(h["x-amz-checksum-sha256"]??""))return false;
  }
  return true;
}
