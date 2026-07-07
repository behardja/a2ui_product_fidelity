import { defineConfig } from 'vite';

// The @a2ui packages ship untranspiled ESM under src/ referencing bare subpath
// specifiers (e.g. '@a2ui/web_core/types/types'); Vite resolves these via the
// packages' exports maps.
//
// `host: true` binds 0.0.0.0 so the server is reachable from outside the VM.
// The `/a2a` proxy forwards the browser's A2A JSON-RPC calls to the agent on
// :10002, so the browser only ever talks to ONE origin — no CORS needed.
// The agent port is overridable via the AGENT_PORT env var.
//
// `allowedHosts`: Vite rejects requests whose Host header it doesn't know
// (DNS-rebinding protection). The Workbench authenticated proxy fronts us as
// <id>-dot-<region>.notebooks.googleusercontent.com, so allow that domain —
// required for BOTH dev (`server`) and the built app (`preview`).
const AGENT_PORT = process.env.AGENT_PORT || '10002';
const ALLOWED_HOSTS = ['.notebooks.googleusercontent.com', 'localhost'];

const proxy = {
  '/a2a': {
    target: `http://127.0.0.1:${AGENT_PORT}`,
    changeOrigin: true,
    rewrite: (p) => p.replace(/^\/a2a/, '') || '/',
  },
};

export default defineConfig({
  server: {
    host: true,
    port: 5173,
    strictPort: true,
    allowedHosts: ALLOWED_HOSTS,
    proxy,
  },
  preview: {
    host: true,
    port: 5173,
    strictPort: true,
    allowedHosts: ALLOWED_HOSTS,
    proxy,
  },
});
