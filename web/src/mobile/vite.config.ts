/**
 * W6's own Vite config for the phone page (web/mobile.html): W5's web/vite.config.ts (the same /api middleware and
 * settings) plus (1) mobile.html as a second build entry and (2) the tailnet host name, so a phone can open the page
 * through `tailscale serve` (Vite blocks unknown Host headers). web/vite.config.ts is not changed.
 *   npx vite --config src/mobile/vite.config.ts --port 8131            (dev, hot reload)
 *   npx vite build --config src/mobile/vite.config.ts --outDir <dir>   (stable build: index.html + mobile.html)
 */
import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import { defineConfig, mergeConfig, type PluginOption } from "vite";
import base from "../../vite.config";
import { phoneApi } from "./phoneapi";

const web = path.resolve(__dirname, "../..");
const hosts = [".ts.net", "localhost", "127.0.0.1"];
// node_modules may be a symlink (to W5's checkout): let the dev server serve files from its real folder too (fonts)
const nm = fs.existsSync(path.join(web, "node_modules")) ? fs.realpathSync(path.join(web, "node_modules")) : path.join(web, "node_modules");

/** True when the base config already loads the phone page's plugin (W5's config may add it): never register it twice. */
const hasPhoneApi = (ps: PluginOption[] | undefined): boolean =>
  (ps ?? []).some((p) => (Array.isArray(p) ? hasPhoneApi(p) : !!p && typeof p === "object" && "name" in p && p.name === "w6-phone-api"));

export default mergeConfig(base, defineConfig({
  plugins: hasPhoneApi(base.plugins) ? [] : [phoneApi()],
  root: web,
  // node_modules is often a symlink to W5's checkout: keep this server's pre-bundled deps out of W5's cache
  cacheDir: path.join(os.tmpdir(), "vite-cache-w6"),
  server: { allowedHosts: hosts, fs: { allow: [web, nm] } },
  preview: { allowedHosts: hosts },
  build: { rollupOptions: { input: { main: path.join(web, "index.html"), mobile: path.join(web, "mobile.html") } } },
}));
