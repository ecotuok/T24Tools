/**
 * codittle_connections.mjs
 *
 * Reads Codittle's embedded PGlite database and prints all configured
 * SSH stream connections as JSON to stdout.
 *
 * Must be run with Codittle's own node.exe so PGlite can be resolved
 * relative to the sidecar:
 *
 *   <codittle-dir>\node.exe codittle_connections.mjs
 *
 * Called automatically by codittle_connections.py — no need to run directly.
 */

import { cpSync, rmSync, existsSync, readdirSync, statSync } from 'fs';
import { tmpdir } from 'os';
import { join } from 'path';

// Derive PGlite path from Codittle's own node.exe location
const execBase  = 'file:///' + process.execPath.replace(/\\/g, '/');
const pgliteUrl = new URL('./sidecar/node_modules/@electric-sql/pglite/dist/index.js', execBase).href;
const { PGlite } = await import(pgliteUrl);

// ── Locate the active pgdata directory ────────────────────────────────────
const workspacesDir = join(
  process.env['LOCALAPPDATA'] ?? '',
  'Codittle', 'workspaces'
);

function findPgdata(wsDir) {
  if (!existsSync(wsDir)) return null;
  const candidates = readdirSync(wsDir)
    .map(name => ({ name, path: join(wsDir, name, 'pgdata') }))
    .filter(c => existsSync(c.path));
  if (candidates.length === 0) return null;
  // Prefer the most recently modified workspace
  candidates.sort((a, b) =>
    statSync(b.path).mtimeMs - statSync(a.path).mtimeMs
  );
  return candidates[0].path;
}

const pgdata = findPgdata(workspacesDir);
if (!pgdata) {
  process.stderr.write('ERROR: no Codittle pgdata found under ' + workspacesDir + '\n');
  process.exit(1);
}

// ── Copy to temp so we don't conflict with the running sidecar ────────────
const copyDir = join(tmpdir(), 'codittle-db-snapshot-' + Date.now());
cpSync(pgdata, copyDir, { recursive: true });

const pidFile = join(copyDir, 'postmaster.pid');
if (existsSync(pidFile)) rmSync(pidFile);

// ── Query ─────────────────────────────────────────────────────────────────
const db = new PGlite('file://' + copyDir.replace(/\\/g, '/'));

const { rows } = await db.query(`
  SELECT
    e.id,
    e.name,
    e.env_class,
    e.host,
    e.port,
    e.username,
    e.ssh_home,
    e.last_viewed_at,
    s.version  AS stream_version,
    s.runtime  AS stream_runtime,
    b.name     AS bank_name
  FROM t24_stream_envs e
  JOIN t24_release_streams s ON s.id = e.stream_id
  JOIN t24_banks           b ON b.id = s.bank_id
  ORDER BY e.last_viewed_at DESC NULLS LAST, e.created_at
`);

const projects = await db.query(`
  SELECT p.name, p.id, p.release_stream_id
  FROM projects p
  ORDER BY p.name
`);

await db.close();
rmSync(copyDir, { recursive: true, force: true });

// ── Output ────────────────────────────────────────────────────────────────
process.stdout.write(JSON.stringify({
  connections: rows,
  projects: projects.rows,
}, null, 2) + '\n');
