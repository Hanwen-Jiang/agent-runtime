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
import { TRANSFER_HOST, validTransferSpec, type TransferSpec, type TransferGrant } from "./transfer-policy";

export interface Env {
  SANDBOX: DurableObjectNamespace<Sandbox>;
  DIRECTORY: DurableObjectNamespace<Directory>;
  BRIDGE_TOKEN: string;
  AGENT_KEY: string;
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
const OWNER = /^(_platform|[a-z0-9][a-z0-9_-]{0,62})$/;
const HOST = /^(\*\.)?[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?(\.[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?)+$/;
const MAX_HOSTS = 50;

export class Sandbox extends Container<Env> {
  private transferControllers = new Map<string, Set<AbortController>>();

  // Only this virtual HTTP host reaches trusted code. The signed HTTPS target
  // stays outside the guest; arbitrary outbound hosts remain denied.

  async transferOwner(): Promise<string | undefined> {
    return (await this.owner()) ?? this.ctx.storage.get<string>("transferOwner");
  }

  async grantTransfer(id: string, spec: TransferSpec): Promise<Response> {
    if (!validTransferSpec(spec, Date.now())) return problem(400, "invalid_transfer", "invalid scoped object transfer");
    if (await this.ctx.storage.get(`transfer-revoked:${id}`)) return problem(410,"transfer_revoked","transfer revoked");
    const old = await this.ctx.storage.get<TransferGrant>(`transfer:${id}`);
    if (old) {
      if (JSON.stringify(old.spec) !== JSON.stringify(spec)) return problem(409, "transfer_conflict", "transfer identity changed");
      if (old.revoked || old.spec.expires_at_ms <= Date.now()) return problem(410, "transfer_expired", "transfer is no longer active");
      return Response.json({ url: `http://${TRANSFER_HOST}/${id}` });
    }
    if (!(await this.owner())) return problem(404,"unknown_env","environment missing");
    const all = await this.ctx.storage.list({prefix:"transfer:"});
    if (all.size >= 256) return problem(429,"transfer_capacity","too many transfer records");
    // Install durable expiry cleanup before admitting authority. If the process
    // dies at either boundary, authorization still checks the stored timestamp.
    await this.schedule(new Date(spec.expires_at_ms), "expireTransfer", { id });
    const admitted = await this.ctx.storage.transaction(async tx => {
      if (await tx.get(`transfer-revoked:${id}`)) return false;
      const concurrent = await tx.get<TransferGrant>(`transfer:${id}`);
      if (!concurrent && (await tx.list({prefix:"transfer:"})).size>=256) return false;
      if (concurrent) return !concurrent.revoked && JSON.stringify(concurrent.spec)===JSON.stringify(spec);
      await tx.put(`transfer:${id}`, { spec, bytes: 0, attempts: 0, revoked: false } satisfies TransferGrant);
      return true;
    });
    if (!admitted) return problem(409,"transfer_conflict","transfer revoked or changed");
    return Response.json({ url: `http://${TRANSFER_HOST}/${id}` });
  }

  async transferStatus(id:string):Promise<Response> {
    const grant=await this.ctx.storage.get<TransferGrant>(`transfer:${id}`);
    if(!grant)return problem(404,"transfer_missing","transfer missing");
    return Response.json({bytes:grant.bytes,revoked:grant.revoked||Date.now()>=grant.spec.expires_at_ms,active:this.transferControllers.get(id)?.size??0});
  }

  async expireTransfer({ id }: { id: string }): Promise<void> { await this.revokeTransfer(id); }

  async revokeTransfer(id: string): Promise<Response> {
    // Retain terminal accounting until the control plane acknowledges it. A
    // missing mint is an explicit zero only under the original environment owner.
    let used = 0;
    await this.ctx.storage.transaction(async tx => {
      await tx.put(`transfer-revoked:${id}`,Date.now());
      const grant = await tx.get<TransferGrant>(`transfer:${id}`);
      if (!grant) return;
      grant.revoked = true; used = grant.bytes;
      await tx.put(`transfer:${id}`, grant);
    });
    for (const controller of this.transferControllers.get(id) ?? []) controller.abort();
    return Response.json({ bytes: used, revoked: true });
  }

  async forgetTransfer(id: string): Promise<void> {
    const grant = await this.ctx.storage.get<TransferGrant>(`transfer:${id}`);
    if (grant && !grant.revoked) return;
    await this.ctx.storage.delete(`transfer:${id}`);
    // A delayed mint cannot resurrect a revoked capability after accounting.
    await this.schedule(600,"forgetTransferTombstone",{id});
  }

  async forgetTransferTombstone({id}:{id:string}):Promise<void> {
    await this.ctx.storage.delete(`transfer-revoked:${id}`);
  }

  private async countTransfer(id: string, bytes: number): Promise<void> {
    await this.ctx.storage.transaction(async tx => {
      const g = await tx.get<TransferGrant>(`transfer:${id}`);
      if (!g || g.revoked || Date.now() >= g.spec.expires_at_ms || g.bytes + bytes > 3 * g.spec.size_bytes) throw new Error("transfer denied");
      g.bytes += bytes;
      await tx.put(`transfer:${id}`, g);
    });
  }

  override async fetch(request: Request): Promise<Response> {
    const url = new URL(request.url);
    const id = url.pathname.slice(1);
    if (url.hostname !== TRANSFER_HOST || url.protocol !== "http:" || url.port || url.search || !/^sbg-[a-f0-9]{32}$/.test(id)) return problem(403,"transfer_denied","transfer denied");
    let grant: TransferGrant | undefined;
    await this.ctx.storage.transaction(async tx => {
      const g = await tx.get<TransferGrant>(`transfer:${id}`);
      if (!g || g.revoked || Date.now() >= g.spec.expires_at_ms || g.attempts >= 3 || request.method !== g.spec.method) return;
      if (request.method === "PUT" && request.headers.get("Content-Length") !== String(g.spec.size_bytes)) return;
      if (request.method === "GET" && request.body) return;
      g.attempts++; await tx.put(`transfer:${id}`,g); grant=g;
    });
    if (!grant) return problem(403,"transfer_denied","transfer denied");
    const spec = (grant as TransferGrant).spec;
    const abort = new AbortController();
    const active = this.transferControllers.get(id) ?? new Set<AbortController>(); active.add(abort); this.transferControllers.set(id,active);
    const timer = setTimeout(() => abort.abort(), Math.max(1,spec.expires_at_ms-Date.now()));
    const finish = () => { clearTimeout(timer); active.delete(abort); if (!active.size) this.transferControllers.delete(id); };
    let bytes = 0;
    const counter = () => new TransformStream<Uint8Array,Uint8Array>({
      transform: async (chunk, controller) => {
        if (abort.signal.aborted || bytes + chunk.byteLength > spec.size_bytes) throw new Error("transfer bound exceeded");
        await this.countTransfer(id,chunk.byteLength); bytes += chunk.byteLength; controller.enqueue(chunk);
      },
      flush: () => { if(bytes!==spec.size_bytes) throw new Error("incomplete transfer"); },
    });
    try {
      let body: ReadableStream<Uint8Array> | null = null;
      let uploaded: Promise<void> = Promise.resolve();
      if (spec.method === "PUT") {
        const fixed = new FixedLengthStream(spec.size_bytes);
        body = fixed.readable;
        const input = request.body ?? new ReadableStream<Uint8Array>({start: c => c.close()});
        uploaded = input.pipeThrough(counter()).pipeTo(fixed.writable, {signal:abort.signal});
        this.ctx.waitUntil(uploaded.catch(() => {abort.abort();}));
      }
      const response = await fetch(spec.target.url, {method:spec.method,headers:spec.target.headers,
        body,redirect:"manual",signal:abort.signal});
      await uploaded;
      if (response.status < 200 || response.status >= 300) { await response.body?.cancel(); finish(); return problem(response.status===412?412:502,"transfer_upstream","object transfer rejected"); }
      if (spec.method === "PUT") { await response.body?.cancel(); finish(); return new Response(null,{status:200}); }
      if (!response.body) { finish(); return problem(502,"transfer_empty","empty object response"); }
      const reader = response.body.pipeThrough(counter()).getReader();
      const stream = new ReadableStream<Uint8Array>({
        pull: async controller => { try {const next=await reader.read();if(next.done){finish();controller.close();}else controller.enqueue(next.value);}catch(e){finish();abort.abort();controller.error(e);} },
        cancel: async reason => { abort.abort();finish();await reader.cancel(reason); },
      });
      return new Response(stream,{headers:{"Content-Length":String(spec.size_bytes),"Content-Type":"application/octet-stream","Cache-Control":"no-store"}});
    } catch { abort.abort();finish();return problem(502,"transfer_interrupted","object transfer interrupted"); }
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
    const body = request.method === "GET" || request.method === "HEAD" ? null : await request.arrayBuffer();
    return stub.agent(request.method, m[3] + url.search, body, request.headers.get("Content-Type") ?? "");
  },
};

async function authorized(request: Request, env: Env): Promise<boolean> {
  const got = request.headers.get("Authorization") ?? "";
  const want = `Bearer ${env.BRIDGE_TOKEN}`;
  if (!env.BRIDGE_TOKEN || got.length !== want.length) return false;
  const enc = new TextEncoder();
  return crypto.subtle.timingSafeEqual(enc.encode(got), enc.encode(want));
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
