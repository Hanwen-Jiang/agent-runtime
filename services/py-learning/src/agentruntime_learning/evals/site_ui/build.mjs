import { fileURLToPath } from "node:url";
import { dirname, resolve } from "node:path";
import { readFile, writeFile, mkdir, rm, copyFile } from "node:fs/promises";
import { execFileSync } from "node:child_process";
import { createHash } from "node:crypto";
const root = dirname(fileURLToPath(import.meta.url));
const modules = process.env.HEROUI_MODULES || resolve(root, "node_modules");
const buildModules = process.env.BUILD_MODULES || modules;
const { build } = await import(resolve(buildModules, "esbuild/lib/main.js"));
const out = resolve(root, "../site_pages/assets");
await mkdir(out, { recursive: true });
await build({
  entryPoints: [resolve(root, "src/app.jsx")],
  outfile: resolve(out, "app.js"),
  bundle: true,
  minify: true,
  jsx: "automatic",
  target: "es2022",
  format: "iife",
  nodePaths: [modules, buildModules],
  alias: {
    react: resolve(modules, "react"),
    "react-dom": resolve(modules, "react-dom"),
  },
  define: { "process.env.NODE_ENV": '"production"' },
  legalComments: "eof",
});
const source = (await readFile(resolve(root, "src/style.css"), "utf8"))
  .replace(
    '"tailwindcss"',
    JSON.stringify(resolve(buildModules, "tailwindcss/index.css")),
  )
  .replace(
    '"@heroui/styles"',
    JSON.stringify(resolve(modules, "@heroui/styles/dist/index.css")),
  )
  .replace(
    '"@heroui-pro/react/css/components/native-select.css"',
    JSON.stringify(
      resolve(
        modules,
        "@heroui-pro/react/dist/css/components/native-select.css",
      ),
    ),
  );
await writeFile(resolve(root, ".build.css"), source);
execFileSync(
  process.execPath,
  [
    resolve(buildModules, "@tailwindcss/cli/dist/index.mjs"),
    "-i",
    resolve(root, ".build.css"),
    "-o",
    resolve(out, "app.css"),
    "--minify",
  ],
  { cwd: root, stdio: "inherit" },
);
await rm(resolve(root, ".build.css"));

// The production CDN caches JavaScript and CSS even when the origin sends no-cache.
// Version both references together so a new shell never reuses a previous bundle.
const version = createHash("sha256")
  .update(await readFile(resolve(out, "app.js")))
  .update(await readFile(resolve(out, "app.css")))
  .digest("hex").slice(0, 16);
const shellPath = resolve(root, "../site_pages/app.html");
const shell = await readFile(shellPath, "utf8");
await writeFile(shellPath, shell.replace(/\/assets\/app\.(js|css)(?:\?v=[a-z0-9]+)?/g, `/assets/app.$1?v=${version}`));

const fontModules = process.env.FONT_MODULES || modules;
for (const family of ["sans", "mono"])
  for (const weight of family === "sans" ? [400, 500, 600] : [400]) {
    const name = `ibm-plex-${family}-latin-${weight}-normal.woff2`;
    await copyFile(
      resolve(fontModules, `@fontsource/ibm-plex-${family}/files/${name}`),
      resolve(out, name),
    );
  }
