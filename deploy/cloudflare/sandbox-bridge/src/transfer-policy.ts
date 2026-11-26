export const TRANSFER_HOST = "ar-transfer.internal";
export const AGENT_DISPATCH_HOST = "ar-agent.internal";
export const CHECKPOINT_BYTES = 4 * 1024 * 1024;
export const MAX_BATCH_GRANTS = 64;

export interface TransferSpec {
  task_id: string; method: "GET"|"PUT"; size_bytes: number; expires_at_ms: number;
  target: {url: string; headers?: Record<string,string>; proxy?: string};
}
export interface TransferGrant {spec:TransferSpec; bytes:number; attempts:number; revoked:boolean;}

export interface ActiveTransferStream {
  controller: AbortController;
  unflushed: number;
}

export class TransferTracker {
  private streams = new Map<string, Set<ActiveTransferStream>>();
  private persisted = new Map<string, number>();
  private flushing = new Map<string, number>();

  seedPersisted(id: string, bytes: number): void {
    this.persisted.set(id, bytes);
  }

  deletePersisted(id: string): void {
    this.persisted.delete(id);
  }

  register(id: string, controller: AbortController): ActiveTransferStream {
    const stream: ActiveTransferStream = { controller, unflushed: 0 };
    let set = this.streams.get(id);
    if (!set) {
      set = new Set();
      this.streams.set(id, set);
    }
    set.add(stream);
    return stream;
  }

  unregister(id: string, stream: ActiveTransferStream): void {
    const set = this.streams.get(id);
    if (!set) return;
    set.delete(stream);
    if (set.size === 0) this.streams.delete(id);
  }

  activeCount(id: string): number {
    return this.streams.get(id)?.size ?? 0;
  }

  unflushedBytes(id: string): number {
    let sum = 0;
    for (const s of this.streams.get(id) ?? []) sum += s.unflushed;
    return sum;
  }

  inFlightBytes(id: string): number {
    return this.unflushedBytes(id) + (this.flushing.get(id) ?? 0);
  }

  canRecordChunk(id: string, maxTotalBytes: number, chunkBytes: number): boolean {
    const base = this.persisted.get(id) ?? 0;
    return base + this.inFlightBytes(id) + chunkBytes <= maxTotalBytes;
  }

  beginFlush(id: string, delta: number): void {
    if (delta <= 0) return;
    this.flushing.set(id, (this.flushing.get(id) ?? 0) + delta);
  }

  endFlush(id: string, delta: number, committedBytes?: number): void {
    if (delta > 0) {
      const next = Math.max(0, (this.flushing.get(id) ?? 0) - delta);
      if (next === 0) this.flushing.delete(id);
      else this.flushing.set(id, next);
    }
    if (committedBytes !== undefined) {
      this.persisted.set(id, committedBytes);
    }
  }

  recordChunk(id: string, stream: ActiveTransferStream, chunkBytes: number): number {
    stream.unflushed += chunkBytes;
    if (stream.unflushed >= CHECKPOINT_BYTES) {
      return this.takeStreamDelta(id, stream);
    }
    return 0;
  }

  takeStreamDelta(id: string, stream: ActiveTransferStream): number {
    const d = stream.unflushed;
    stream.unflushed = 0;
    this.beginFlush(id, d);
    return d;
  }

  takeAllUnflushed(id: string): { delta: number; controllers: AbortController[] } {
    let delta = 0;
    const controllers: AbortController[] = [];
    for (const s of this.streams.get(id) ?? []) {
      delta += s.unflushed;
      s.unflushed = 0;
      controllers.push(s.controller);
    }
    return { delta, controllers };
  }
}

export function r2BindingKey(targetUrl: string, bucketName?: string): string | null {
  if (!bucketName) return null;
  let u: URL;
  try {
    u = new URL(targetUrl);
  } catch {
    return null;
  }
  const afterScheme = targetUrl.slice(u.protocol.length + 2).split(/[?#]/, 1)[0] ?? "";
  const slashIdx = afterScheme.indexOf("/");
  const rawPathname = slashIdx >= 0 ? afterScheme.slice(slashIdx) : "/";
  const prefix = `/${bucketName}/`;
  if (!u.pathname.startsWith(prefix) || !rawPathname.startsWith(prefix)) return null;
  const rawKey = rawPathname.slice(prefix.length);
  if (!rawKey) return null;
  try {
    const decoded = rawKey.split("/").map((seg) => decodeURIComponent(seg));
    if (decoded.some((seg) => !seg || seg === "." || seg === ".." || seg.includes("/") || seg.includes("\\") || seg.includes("\0"))) {
      return null;
    }
    return decoded.join("/");
  } catch {
    return null;
  }
}

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
