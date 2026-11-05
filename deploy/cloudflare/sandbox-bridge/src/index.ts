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
  override defaultPort = AGENT_PORT;
  override enableInternet = false;
  // 建环境时按 idle_seconds 改成秒数，所以类型要与基类一样宽。
  override sleepAfter: string | number = "10m";

  // 建环境：记下作用域与参数，起容器，等 agent 的端口可连。
  async open(envId: string, owner: string, idleSeconds: number, allowHosts: string[]): Promise<void> {
    if (await this.ctx.storage.get<string>("owner")) {
      throw new Error("environment exists");
    }
    await this.ctx.storage.put({ envId, owner, idleSeconds, allowHosts });
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
    await this.ctx.storage.deleteAll();
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
    if (hosts.length > 0) {
      await this.setAllowedHosts(hosts);
    }
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
