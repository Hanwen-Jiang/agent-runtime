// Sandbox 的 Cloudflare 桥（docs/plan/cloud-deploy.md D-07）。Runtime 的 Python 适配器
// （agentruntime_core.adapters.cloudflare_sandbox）经 HTTPS 调这个 Worker；每个沙箱环境是一个 Durable Object 加一台
// Cloudflare Container（Firecracker microVM），容器里跑 sandbox-agent（services/rs-sandbox-agent，D-28）。
//
//   GET    /healthz                                   不要令牌
//   GET    /v1/sandboxes                              本部署在跑的环境（X-Sandbox-Owner 要是 _platform）→ {"environments": […]}
//   POST   /v1/sandboxes                              {"idle_seconds", "allow_hosts"} → {"env_id"}
//   DELETE /v1/sandboxes/<env_id>                     销毁；已经没了也是 204
//   *      /v1/sandboxes/<env_id>/agent/<agent 路径>   原样转给容器里的 agent（/v1/exec、/v1/files/…）
//
// 认证：Authorization: Bearer <BRIDGE_TOKEN>（wrangler secret）。作用域：X-Sandbox-Owner（租户 ID 或 _platform），
// 建环境时记在 Durable Object 里，之后每个请求都核对；不对就是 404，看不出环境存在与否。
// agent 的令牌由 AGENT_KEY（wrangler secret）对 Durable Object 的 id 做 HMAC 得到，只在桥里用，不回给调用方。
// 网络：类上 enableInternet = false；allow_hosts 非空时用 setAllowedHosts 只放行这些主机（只有 80、443 与 DNS）。
// 磁盘是临时的：空闲超过 idle_seconds 后容器停掉，环境一并忘掉（之后访问是 404）。
// 列表：一个名为 directory 的 Directory（Durable Object）记着每个环境的作用域与创建时间，建时登记、删时去掉，
// 给 Runtime 的沙箱环境表对账（D-11）。

import { DurableObject } from "cloudflare:workers";
import { Container, getContainer } from "@cloudflare/containers";
export { ContainerProxy } from "@cloudflare/containers";
import {
  AGENT_DISPATCH_HOST,
  MAX_BATCH_GRANTS,
  TRANSFER_HOST,
  TransferTracker,
  r2BindingKey,
  validTransferSpec,
  type TransferGrant,
  type TransferSpec,
} from "./transfer-policy";

export interface Env {
  SANDBOX: DurableObjectNamespace<Sandbox>;
  DIRECTORY: DurableObjectNamespace<Directory>;
  BRIDGE_TOKEN: string;
  AGENT_KEY: string;
  ARCHIVE_BUCKET?: R2Bucket;
  ARCHIVE_BUCKET_NAME?: string;
}

interface Listed {
  env_id: string;
  owner: string;
  created_at: string;
}

// 本部署全部环境的目录（只有一个实例，按名字 directory 取）。
export class Directory extends DurableObject<Env> {
  async add(envId: string, owner: string, createdAt: string): Promise<void> {
    await this.ctx.storage.put(`env:${envId}`, { env_id: envId, owner, created_at: createdAt } satisfies Listed);
  }

  async remove(envId: string): Promise<void> {
    await this.ctx.storage.delete(`env:${envId}`);
  }

  async list(): Promise<Listed[]> {
    const entries = await this.ctx.storage.list<Listed>({ prefix: "env:" });
    return [...entries.values()];
  }
}

function directory(env: Env): DurableObjectStub<Directory> {
  return env.DIRECTORY.get(env.DIRECTORY.idFromName("directory"));
}

const AGENT_PORT = 7070;
const ENV_ID = /^cfs-[0-9a-f]{16}$/;
const GRANT_ID = /^sbg-[a-f0-9]{32}$/;
const OWNER = /^(_platform|[a-z0-9][a-z0-9_-]{0,62})$/;
const HOST = /^(\*\.)?[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?(\.[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?)+$/;
const MAX_HOSTS = 50;

export class Sandbox extends Container<Env> {
  private tracker = new TransferTracker();
  private grantCount: number | null = null;

  // Only this virtual HTTP host reaches trusted code. The signed HTTPS target
  // stays outside the guest; arbitrary outbound hosts remain denied.

  async transferOwner(): Promise<string | undefined> {
    return (await this.owner()) ?? this.ctx.storage.get<string>("transferOwner");
  }

  private async countStoredGrants(tx: DurableObjectTransaction): Promise<number> {
    if (this.grantCount === null) {
      this.grantCount = (await tx.list({ prefix: "transfer:" })).size;
    }
    return this.grantCount;
  }

  async grantTransfer(id: string, spec: TransferSpec): Promise<Response> {
    if (!validTransferSpec(spec, Date.now())) return problem(400, "invalid_transfer", "invalid scoped object transfer");
    if (await this.ctx.storage.get(`transfer-revoked:${id}`)) return problem(410, "transfer_revoked", "transfer revoked");
    const old = await this.ctx.storage.get<TransferGrant>(`transfer:${id}`);
    if (old) {
      if (JSON.stringify(old.spec) !== JSON.stringify(spec)) return problem(409, "transfer_conflict", "transfer identity changed");
      if (old.revoked || old.spec.expires_at_ms <= Date.now()) return problem(410, "transfer_expired", "transfer is no longer active");
      this.tracker.seedPersisted(id, old.bytes);
      return Response.json({ url: `http://${TRANSFER_HOST}/${id}` });
    }
    if (!(await this.owner())) return problem(404, "unknown_env", "environment missing");
    // Install durable expiry cleanup before admitting authority. If the process
    // dies at either boundary, authorization still checks the stored timestamp.
    await this.schedule(new Date(spec.expires_at_ms), "expireTransfer", { id });
    const result = await this.ctx.storage.transaction<"ok" | "conflict" | "capacity">(async (tx) => {
      if (await tx.get(`transfer-revoked:${id}`)) return "conflict";
      const concurrent = await tx.get<TransferGrant>(`transfer:${id}`);
      if (concurrent) {
        return !concurrent.revoked && JSON.stringify(concurrent.spec) === JSON.stringify(spec) ? "ok" : "conflict";
      }
      const count = await this.countStoredGrants(tx);
      if (count >= 256) return "capacity";
      await tx.put(`transfer:${id}`, { spec, bytes: 0, attempts: 0, revoked: false } satisfies TransferGrant);
      this.grantCount = count + 1;
      this.tracker.seedPersisted(id, 0);
      return "ok";
    });
    if (result === "capacity") return problem(429, "transfer_capacity", "too many transfer records");
    if (result === "conflict") return problem(409, "transfer_conflict", "transfer revoked or changed");
    return Response.json({ url: `http://${TRANSFER_HOST}/${id}` });
  }

  async grantTransfersBatch(items: Array<{ id: string; spec?: TransferSpec } & Partial<TransferSpec>>): Promise<Response> {
    if (!Array.isArray(items) || items.length < 1 || items.length > MAX_BATCH_GRANTS) {
      return problem(400, "invalid_transfer", "invalid transfer batch");
    }
    const now = Date.now();
    const seen = new Set<string>();
    const normalized: Array<{ id: string; spec: TransferSpec }> = [];
    let maxExpiry = 0;
    for (const item of items) {
      const spec = (item && typeof item === "object" && item.spec ? item.spec : item) as TransferSpec;
      if (!item || typeof item.id !== "string" || !GRANT_ID.test(item.id) || seen.has(item.id) || !validTransferSpec(spec, now)) {
        return problem(400, "invalid_transfer", "invalid scoped object transfer");
      }
      seen.add(item.id);
      if (spec.expires_at_ms > maxExpiry) maxExpiry = spec.expires_at_ms;
      normalized.push({ id: item.id, spec });
    }
    if (!(await this.owner())) return problem(404, "unknown_env", "environment missing");
    await this.schedule(new Date(maxExpiry), "expireTransfersBatch", { ids: normalized.map((it) => it.id) });
    const errCode = await this.ctx.storage.transaction<"ok" | "revoked" | "conflict" | "expired" | "capacity">(async (tx) => {
      const count = await this.countStoredGrants(tx);
      const toPut: Array<{ id: string; spec: TransferSpec }> = [];
      for (const item of normalized) {
        if (await tx.get(`transfer-revoked:${item.id}`)) return "revoked";
        const existing = await tx.get<TransferGrant>(`transfer:${item.id}`);
        if (existing) {
          if (JSON.stringify(existing.spec) !== JSON.stringify(item.spec)) return "conflict";
          if (existing.revoked || existing.spec.expires_at_ms <= now) return "expired";
          this.tracker.seedPersisted(item.id, existing.bytes);
          continue;
        }
        if (count + toPut.length >= 256) return "capacity";
        toPut.push(item);
      }
      for (const item of toPut) {
        await tx.put(`transfer:${item.id}`, { spec: item.spec, bytes: 0, attempts: 0, revoked: false } satisfies TransferGrant);
        this.tracker.seedPersisted(item.id, 0);
      }
      this.grantCount = count + toPut.length;
      return "ok";
    });
    if (errCode === "revoked") return problem(410, "transfer_revoked", "transfer revoked");
    if (errCode === "expired") return problem(410, "transfer_expired", "transfer is no longer active");
    if (errCode === "capacity") return problem(429, "transfer_capacity", "too many transfer records");
    if (errCode === "conflict") return problem(409, "transfer_conflict", "transfer identity changed");
    const urls: Record<string, string> = {};
    for (const item of normalized) urls[item.id] = `http://${TRANSFER_HOST}/${item.id}`;
    return Response.json({ urls });
  }

  async transferStatus(id: string): Promise<Response> {
    const grant = await this.ctx.storage.get<TransferGrant>(`transfer:${id}`);
    if (!grant) return problem(404, "transfer_missing", "transfer missing");
    return Response.json({
      bytes: grant.bytes + this.tracker.unflushedBytes(id),
      revoked: grant.revoked || Date.now() >= grant.spec.expires_at_ms,
      active: this.tracker.activeCount(id),
    });
  }

  async expireTransfer({ id }: { id: string }): Promise<void> {
    await this.revokeTransfer(id);
  }

  async expireTransfersBatch({ ids }: { ids: string[] }): Promise<void> {
    if (Array.isArray(ids) && ids.length > 0) {
      await this.revokeTransfersBatch(ids);
    }
  }

  async revokeTransfer(id: string): Promise<Response> {
    // Retain terminal accounting until the control plane acknowledges it. A
    // missing mint is an explicit zero only under the original environment owner.
    const { delta, controllers } = this.tracker.takeAllUnflushed(id);
    let used = 0;
    const now = Date.now();
    await this.ctx.storage.transaction(async (tx) => {
      await tx.put(`transfer-revoked:${id}`, now);
      const grant = await tx.get<TransferGrant>(`transfer:${id}`);
      if (!grant) return;
      grant.revoked = true;
      if (delta > 0) {
        grant.bytes = Math.min(3 * grant.spec.size_bytes, grant.bytes + delta);
      }
      used = grant.bytes;
      await tx.put(`transfer:${id}`, grant);
    });
    this.tracker.deletePersisted(id);
    for (const controller of controllers) controller.abort();
    return Response.json({ bytes: used, revoked: true });
  }

  async revokeTransfersBatch(ids: string[]): Promise<Response> {
    if (!Array.isArray(ids) || ids.length > MAX_BATCH_GRANTS || !ids.every((id) => typeof id === "string" && GRANT_ID.test(id))) {
      return problem(400, "invalid_transfer", "invalid transfer revoke batch");
    }
    const swept = new Map<string, { delta: number; controllers: AbortController[] }>();
    for (const id of ids) {
      swept.set(id, this.tracker.takeAllUnflushed(id));
    }
    const bytes: Record<string, number> = {};
    const now = Date.now();
    await this.ctx.storage.transaction(async (tx) => {
      for (const id of ids) {
        await tx.put(`transfer-revoked:${id}`, now);
        const grant = await tx.get<TransferGrant>(`transfer:${id}`);
        if (!grant) {
          bytes[id] = 0;
          continue;
        }
        grant.revoked = true;
        const d = swept.get(id)?.delta ?? 0;
        if (d > 0) {
          grant.bytes = Math.min(3 * grant.spec.size_bytes, grant.bytes + d);
        }
        bytes[id] = grant.bytes;
        await tx.put(`transfer:${id}`, grant);
      }
    });
    for (const id of ids) {
      this.tracker.deletePersisted(id);
      for (const c of swept.get(id)?.controllers ?? []) c.abort();
    }
    return Response.json({ bytes, revoked: true });
  }

  async forgetTransfer(id: string): Promise<void> {
    await this.ctx.storage.transaction(async (tx) => {
      const grant = await tx.get<TransferGrant>(`transfer:${id}`);
      if (grant && !grant.revoked) return;
      const deleted = await tx.delete(`transfer:${id}`);
      if (deleted && this.grantCount !== null && this.grantCount > 0) {
        this.grantCount--;
      }
    });
    this.tracker.deletePersisted(id);
    // A delayed mint cannot resurrect a revoked capability after accounting.
    await this.schedule(600, "forgetTransferTombstone", { id });
  }

  async forgetTransfersBatch(ids: string[]): Promise<void> {
    if (!Array.isArray(ids) || ids.length > MAX_BATCH_GRANTS || !ids.every((id) => typeof id === "string" && GRANT_ID.test(id))) {
      throw new Error("invalid transfer ack batch");
    }
    if (ids.length === 0) return;
    await this.ctx.storage.transaction(async (tx) => {
      for (const id of ids) {
        const grant = await tx.get<TransferGrant>(`transfer:${id}`);
        if (grant && !grant.revoked) continue;
        const deleted = await tx.delete(`transfer:${id}`);
        if (deleted && this.grantCount !== null && this.grantCount > 0) {
          this.grantCount--;
        }
      }
    });
    for (const id of ids) this.tracker.deletePersisted(id);
    await this.schedule(600, "forgetTransferTombstones", { ids });
  }

  async forgetTransferTombstone({ id }: { id: string }): Promise<void> {
    await this.ctx.storage.delete(`transfer-revoked:${id}`);
  }

  async forgetTransferTombstones({ ids }: { ids: string[] }): Promise<void> {
    if (!Array.isArray(ids) || ids.length === 0) return;
    await this.ctx.storage.delete(ids.map((id) => `transfer-revoked:${id}`));
  }

  private async flushTransferDelta(id: string, delta: number): Promise<void> {
    if (delta <= 0) return;
    let committedBytes: number | undefined;
    try {
      await this.ctx.storage.transaction(async (tx) => {
        const g = await tx.get<TransferGrant>(`transfer:${id}`);
        if (!g || g.revoked || Date.now() >= g.spec.expires_at_ms || g.bytes + delta > 3 * g.spec.size_bytes) {
          throw new Error("transfer denied");
        }
        g.bytes += delta;
        committedBytes = g.bytes;
        await tx.put(`transfer:${id}`, g);
      });
    } finally {
      this.tracker.endFlush(id, delta, committedBytes);
    }
  }

  override async fetch(request: Request): Promise<Response> {
    const url = new URL(request.url);
    if (url.hostname === AGENT_DISPATCH_HOST && url.protocol === "http:") {
      const envId = (await this.ctx.storage.get<string>("envId")) ?? "";
      const expected = envId ? await hmacHex(this.env.AGENT_KEY, "bridge-dispatch:" + envId) : "";
      const got = request.headers.get("X-AR-Bridge-Dispatch") ?? "";
      if (!envId || got.length !== expected.length || !timingSafeStringEqual(got, expected)) {
        return problem(403, "transfer_denied", "forbidden");
      }
      const { status } = await this.getState();
      if (status !== "running" && status !== "healthy") {
        await this.boot();
      }
      this.renewActivityTimeout();
      const headers = new Headers({ "X-Sandbox-Token": await this.agentToken() });
      const ct = request.headers.get("Content-Type");
      if (ct) headers.set("Content-Type", ct);
      const cl = request.headers.get("Content-Length");
      if (cl) headers.set("Content-Length", cl);
      const hasBody = request.method !== "GET" && request.method !== "HEAD";
      return this.containerFetch(new Request(`http://sandbox${url.pathname}${url.search}`, {
        method: request.method,
        headers,
        body: hasBody ? request.body : null,
      }), AGENT_PORT);
    }

    const id = url.pathname.slice(1);
    if (url.hostname !== TRANSFER_HOST || url.protocol !== "http:" || url.port || url.search || !GRANT_ID.test(id)) {
      return problem(403, "transfer_denied", "transfer denied");
    }
    let grant: TransferGrant | undefined;
    await this.ctx.storage.transaction(async (tx) => {
      const g = await tx.get<TransferGrant>(`transfer:${id}`);
      if (!g || g.revoked || Date.now() >= g.spec.expires_at_ms || g.attempts >= 3 || request.method !== g.spec.method) return;
      if (request.method === "PUT" && request.headers.get("Content-Length") !== String(g.spec.size_bytes)) return;
      if (request.method === "GET" && request.body) return;
      g.attempts++;
      await tx.put(`transfer:${id}`, g);
      grant = g;
    });
    if (!grant) return problem(403, "transfer_denied", "transfer denied");
    this.tracker.seedPersisted(id, grant.bytes);
    const spec = (grant as TransferGrant).spec;
    const abort = new AbortController();
    const streamState = this.tracker.register(id, abort);
    const timer = setTimeout(() => abort.abort(), Math.max(1, spec.expires_at_ms - Date.now()));
    let finished = false;
    const finish = async (): Promise<void> => {
      if (finished) return;
      finished = true;
      clearTimeout(timer);
      const remaining = this.tracker.takeStreamDelta(id, streamState);
      this.tracker.unregister(id, streamState);
      if (remaining > 0) {
        await this.flushTransferDelta(id, remaining);
      }
    };
    let bytes = 0;
    const counter = () => new TransformStream<Uint8Array, Uint8Array>({
      transform: async (chunk, controller) => {
        if (abort.signal.aborted || bytes + chunk.byteLength > spec.size_bytes) throw new Error("transfer bound exceeded");
        if (!this.tracker.canRecordChunk(id, 3 * spec.size_bytes, chunk.byteLength)) throw new Error("transfer denied");
        bytes += chunk.byteLength;
        const flushDelta = this.tracker.recordChunk(id, streamState, chunk.byteLength);
        if (flushDelta > 0) {
          await this.flushTransferDelta(id, flushDelta);
        }
        controller.enqueue(chunk);
      },
      flush: async () => {
        const d = this.tracker.takeStreamDelta(id, streamState);
        if (d > 0) await this.flushTransferDelta(id, d);
        if (bytes !== spec.size_bytes) throw new Error("incomplete transfer");
      },
    });
    try {
      const r2Key = this.env.ARCHIVE_BUCKET ? r2BindingKey(spec.target.url, this.env.ARCHIVE_BUCKET_NAME) : null;
      if (spec.method === "PUT") {
        const fixed = new FixedLengthStream(spec.size_bytes);
        const body = fixed.readable;
        const input = request.body ?? new ReadableStream<Uint8Array>({ start: (c) => c.close() });
        const uploaded = input.pipeThrough(counter()).pipeTo(fixed.writable, { signal: abort.signal });
        this.ctx.waitUntil(uploaded.catch(() => { abort.abort(); }));
        if (r2Key && this.env.ARCHIVE_BUCKET) {
          const b64 = Object.entries(spec.target.headers ?? {}).find(([k]) => k.toLowerCase() === "x-amz-checksum-sha256")?.[1] ?? "";
          const putObj = await this.env.ARCHIVE_BUCKET.put(r2Key, body, {
            sha256: base64ToBytes(b64),
            onlyIf: { etagDoesNotMatch: "*" },
          });
          await uploaded;
          if (!putObj) {
            await finish().catch(() => undefined);
            return problem(412, "transfer_upstream", "object transfer rejected");
          }
          await finish();
          return new Response(null, { status: 200 });
        }
        const response = await fetch(spec.target.url, {
          method: spec.method,
          headers: spec.target.headers,
          body,
          redirect: "manual",
          signal: abort.signal,
        });
        await uploaded;
        if (response.status < 200 || response.status >= 300) {
          await response.body?.cancel();
          await finish().catch(() => undefined);
          return problem(response.status === 412 ? 412 : 502, "transfer_upstream", "object transfer rejected");
        }
        await response.body?.cancel();
        await finish();
        return new Response(null, { status: 200 });
      }

      let rawBody: ReadableStream<Uint8Array> | null = null;
      if (r2Key && this.env.ARCHIVE_BUCKET) {
        const obj = await this.env.ARCHIVE_BUCKET.get(r2Key);
        rawBody = obj?.body ?? null;
      } else {
        const response = await fetch(spec.target.url, {
          method: spec.method,
          headers: spec.target.headers,
          body: null,
          redirect: "manual",
          signal: abort.signal,
        });
        if (response.status < 200 || response.status >= 300) {
          await response.body?.cancel();
          await finish().catch(() => undefined);
          return problem(response.status === 412 ? 412 : 502, "transfer_upstream", "object transfer rejected");
        }
        rawBody = response.body;
      }
      if (!rawBody) {
        await finish().catch(() => undefined);
        return problem(502, "transfer_empty", "empty object response");
      }
      const reader = rawBody.pipeThrough(counter()).getReader();
      const stream = new ReadableStream<Uint8Array>({
        pull: async (controller) => {
          try {
            const next = await reader.read();
            if (next.done) {
              await finish();
              controller.close();
            } else {
              controller.enqueue(next.value);
            }
          } catch (e) {
            abort.abort();
            await finish().catch(() => undefined);
            controller.error(e);
          }
        },
        cancel: async (reason) => {
          abort.abort();
          await finish().catch(() => undefined);
          await reader.cancel(reason);
        },
      });
      return new Response(stream, {
        headers: { "Content-Length": String(spec.size_bytes), "Content-Type": "application/octet-stream", "Cache-Control": "no-store" },
      });
    } catch {
      abort.abort();
      await finish().catch(() => undefined);
      return problem(502, "transfer_interrupted", "object transfer interrupted");
    }
  }

  override defaultPort = AGENT_PORT;
  override enableInternet = false;
  // 建环境时按 idle_seconds 改成秒数，所以类型要与基类一样宽。
  override sleepAfter: string | number = "10m";

  // 建环境：记下作用域与参数，起容器，等 agent 的端口可连。
  async open(envId: string, owner: string, idleSeconds: number, allowHosts: string[]): Promise<void> {
    if (await this.ctx.storage.get<string>("owner")) {
      throw new Error("environment exists");
    }
    await this.ctx.storage.put({ envId, owner, transferOwner: owner, idleSeconds, allowHosts });
    await directory(this.env).add(envId, owner, new Date().toISOString());
    await this.boot();
  }

  async owner(): Promise<string | undefined> {
    return this.ctx.storage.get<string>("owner");
  }

  // 把一个请求转给容器里的 agent，带上 agent 的令牌。容器停了（例如重启过）就按记下的参数重新起。
  async agent(method: string, path: string, body: ArrayBuffer | null, contentType: string): Promise<Response> {
    const { status } = await this.getState();
    if (status !== "running" && status !== "healthy") {
      await this.boot();
    }
    this.renewActivityTimeout();
    const headers = new Headers({ "X-Sandbox-Token": await this.agentToken() });
    if (contentType) headers.set("Content-Type", contentType);
    return this.containerFetch(new Request(`http://sandbox${path}`, { method, headers, body }), AGENT_PORT);
  }

  async close(): Promise<void> {
    const envId = await this.ctx.storage.get<string>("envId");
    const owner = await this.owner();
    if (owner) await this.ctx.storage.put("transferOwner",owner);
    const grants = await this.ctx.storage.list({prefix:"transfer:"});
    for (const key of grants.keys()) await this.revokeTransfer(key.slice("transfer:".length));
    await this.ctx.storage.delete(["envId","owner","idleSeconds","allowHosts"]);
    if (envId) await directory(this.env).remove(envId);
    await this.destroy().catch(() => undefined);
  }

  // 空闲到期：停掉容器并忘掉这个环境（磁盘本来就是临时的）。
  override async onActivityExpired(): Promise<void> {
    await this.close();
  }

  private async boot(): Promise<void> {
    const idle = (await this.ctx.storage.get<number>("idleSeconds")) ?? 600;
    const hosts = (await this.ctx.storage.get<string[]>("allowHosts")) ?? [];
    this.sleepAfter = idle;
    await this.setAllowedHosts([...new Set([...hosts, TRANSFER_HOST])]);
    await this.startAndWaitForPorts({
      startOptions: {
        envVars: { AGENT_TOKEN: await this.agentToken(), AGENT_WORKDIR: "/workspace", AGENT_ADDR: `:${AGENT_PORT}`, HOME: "/tmp" },
      },
      ports: AGENT_PORT,
    });
  }

  private async agentToken(): Promise<string> {
    return hmacHex(this.env.AGENT_KEY, this.ctx.id.toString());
  }
}

// Use the SDK setter so ContainerProxy's class registry receives the handler.
Sandbox.outboundByHost = {
  [TRANSFER_HOST]: async (request: Request, env: Env, ctx: {containerId:string}) =>
    env.SANDBOX.get(env.SANDBOX.idFromString(ctx.containerId)).fetch(request),
};

export default {
  async fetch(request: Request, env: Env): Promise<Response> {
    const url = new URL(request.url);
    if (url.pathname === "/healthz") return new Response("ok");
    if (!(await authorized(request, env))) return problem(401, "unauthorized", "桥的令牌不对");
    const owner = request.headers.get("X-Sandbox-Owner") ?? "";
    if (!OWNER.test(owner)) return problem(400, "invalid_owner", "X-Sandbox-Owner 的格式不对");

    if (request.method === "GET" && url.pathname === "/v1/sandboxes") {
      // 列出全部环境是部署级的操作，只给平台作用域。
      if (owner !== "_platform") return problem(403, "platform_only", "列出环境只给平台作用域");
      return Response.json({ environments: await directory(env).list() });
    }

    if (request.method === "POST" && url.pathname === "/v1/sandboxes") {
      let spec: { idle_seconds?: unknown; allow_hosts?: unknown };
      try {
        spec = await request.json();
      } catch {
        return problem(400, "invalid_request", "请求体不是合法的 JSON");
      }
      const idle = Number(spec.idle_seconds ?? 600);
      const hosts = Array.isArray(spec.allow_hosts) ? spec.allow_hosts : [];
      if (!Number.isInteger(idle) || idle < 1 || idle > 86400) return problem(400, "invalid_argument", "idle_seconds 要在 1 到 86400 之间");
      if (hosts.length > MAX_HOSTS || !hosts.every((h) => typeof h === "string" && HOST.test(h))) {
        return problem(400, "invalid_argument", "allow_hosts 要是主机名（可以用 *. 开头），最多 50 个");
      }
      const envId = "cfs-" + randomHex(8);
      await getContainer(env.SANDBOX, envId).open(envId, owner, idle, hosts as string[]);
      return Response.json({ env_id: envId }, { status: 201 });
    }

    const batchTransfer = url.pathname.match(/^\/v1\/sandboxes\/(cfs-[a-f0-9]{16})\/transfer-grants:batch$/);
    if (batchTransfer) {
      if (request.method !== "POST") return problem(405, "invalid_method", "invalid transfer batch method");
      const stub = getContainer(env.SANDBOX, batchTransfer[1]);
      if ((await stub.transferOwner()) !== owner) return problem(404, "unknown_env", "environment missing");
      if (Number(request.headers.get("Content-Length") || 0) > 512 * 1024) {
        return problem(413, "transfer_large", "transfer batch metadata too large");
      }
      let payload: { action?: unknown; items?: unknown; ids?: unknown };
      try {
        const raw = await request.text();
        if (raw.length > 512 * 1024) return problem(413, "transfer_large", "transfer batch metadata too large");
        payload = JSON.parse(raw);
      } catch {
        return problem(400, "invalid_transfer", "invalid batch metadata");
      }
      if (payload.action === "grant") {
        return stub.grantTransfersBatch(payload.items as Array<{ id: string; spec?: TransferSpec } & Partial<TransferSpec>>);
      }
      if (payload.action === "revoke") {
        return stub.revokeTransfersBatch(payload.ids as string[]);
      }
      if (payload.action === "ack") {
        try {
          await stub.forgetTransfersBatch(payload.ids as string[]);
        } catch {
          return problem(400, "invalid_transfer", "invalid transfer ack batch");
        }
        return new Response(null, { status: 204 });
      }
      return problem(400, "invalid_transfer", "invalid transfer batch action");
    }

    const transfer = url.pathname.match(/^\/v1\/sandboxes\/(cfs-[a-f0-9]{16})\/transfer-grants\/(sbg-[a-f0-9]{32})(\/ack)?$/);
    if (transfer) {
      const stub=getContainer(env.SANDBOX,transfer[1]);
      if ((await stub.transferOwner())!==owner) return problem(404,"unknown_env","environment missing");
      if (request.method==="GET" && !transfer[3]) return stub.transferStatus(transfer[2]);
      if (request.method==="DELETE" && !transfer[3]) return stub.revokeTransfer(transfer[2]);
      if (request.method==="POST" && transfer[3]) {await stub.forgetTransfer(transfer[2]);return new Response(null,{status:204});}
      if(request.method!=="PUT"||transfer[3]) return problem(405,"invalid_method","invalid transfer method");
      if(Number(request.headers.get("Content-Length")||0)>32768)return problem(413,"transfer_large","transfer metadata too large");
      let spec:TransferSpec;try {const raw=await request.text();if(raw.length>32768)return problem(413,"transfer_large","transfer metadata too large");spec=JSON.parse(raw);}catch{return problem(400,"invalid_transfer","invalid metadata");}
      return stub.grantTransfer(transfer[2],spec);
    }

    const m = url.pathname.match(/^\/v1\/sandboxes\/([^/]+)(\/agent(\/.*))?$/);
    if (!m || !ENV_ID.test(m[1])) return problem(404, "unknown_env", "环境不存在");
    const stub = getContainer(env.SANDBOX, m[1]);
    const stored = await stub.owner();
    if (request.method === "DELETE" && !m[2]) {
      if (!stored) return new Response(null, { status: 204 });
      if (stored !== owner) return problem(404, "unknown_env", "环境不存在");
      await stub.close();
      return new Response(null, { status: 204 });
    }
    if (!stored || stored !== owner || !m[3]) return problem(404, "unknown_env", "环境不存在");
    const dispatch = await hmacHex(env.AGENT_KEY, "bridge-dispatch:" + m[1]);
    const headers = new Headers();
    headers.set("X-AR-Bridge-Dispatch", dispatch);
    const ct = request.headers.get("Content-Type");
    if (ct) headers.set("Content-Type", ct);
    const cl = request.headers.get("Content-Length");
    if (cl) headers.set("Content-Length", cl);
    const hasBody = request.method !== "GET" && request.method !== "HEAD";
    return stub.fetch(new Request(`http://${AGENT_DISPATCH_HOST}${m[3]}${url.search}`, {
      method: request.method,
      headers,
      body: hasBody ? request.body : null,
    }));
  },
};

function timingSafeStringEqual(a: string, b: string): boolean {
  if (a.length !== b.length) return false;
  const enc = new TextEncoder();
  return crypto.subtle.timingSafeEqual(enc.encode(a), enc.encode(b));
}

function base64ToBytes(b64: string): Uint8Array {
  const bin = atob(b64);
  const out = new Uint8Array(bin.length);
  for (let i = 0; i < bin.length; i++) out[i] = bin.charCodeAt(i);
  return out;
}

async function authorized(request: Request, env: Env): Promise<boolean> {
  const got = request.headers.get("Authorization") ?? "";
  const want = `Bearer ${env.BRIDGE_TOKEN}`;
  if (!env.BRIDGE_TOKEN) return false;
  return timingSafeStringEqual(got, want);
}

function problem(status: number, code: string, message: string): Response {
  return Response.json({ code, message }, { status });
}

function randomHex(bytes: number): string {
  const buf = new Uint8Array(bytes);
  crypto.getRandomValues(buf);
  return [...buf].map((b) => b.toString(16).padStart(2, "0")).join("");
}

async function hmacHex(key: string, message: string): Promise<string> {
  const enc = new TextEncoder();
  const k = await crypto.subtle.importKey("raw", enc.encode(key), { name: "HMAC", hash: "SHA-256" }, false, ["sign"]);
  const sig = new Uint8Array(await crypto.subtle.sign("HMAC", k, enc.encode(message)));
  return [...sig].map((b) => b.toString(16).padStart(2, "0")).join("");
}
