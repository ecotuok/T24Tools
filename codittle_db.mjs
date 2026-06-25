/**
 * codittle_db.mjs — node side of codittle_db.py (run with Codittle's own node.exe).
 *
 *   node codittle_db.mjs status
 *   node codittle_db.mjs connections <pgdata>
 *   node codittle_db.mjs restore <srcPgdata> <dstPgdata> <dry|apply>
 *
 * Reads are done on a temp COPY (never touch the source / a live DB). The restore
 * 'apply' path writes directly into <dstPgdata> — the caller (codittle_db.py) must
 * ensure Codittle is closed first.
 */
import { cpSync, rmSync, existsSync, readdirSync, statSync } from 'fs';
import { tmpdir } from 'os';
import { join } from 'path';

const execBase  = 'file:///' + process.execPath.replace(/\\/g, '/');
const pgliteUrl = new URL('./sidecar/node_modules/@electric-sql/pglite/dist/index.js', execBase).href;
const { PGlite } = await import(pgliteUrl);

const WORKSPACES = join(process.env['LOCALAPPDATA'] ?? '', 'Codittle', 'workspaces');

function openCopy(pgdata) {
  const copyDir = join(tmpdir(), 'cdb-' + Date.now() + '-' + Math.random().toString(36).slice(2, 7));
  cpSync(pgdata, copyDir, { recursive: true });
  const pid = join(copyDir, 'postmaster.pid'); if (existsSync(pid)) rmSync(pid);
  return { db: new PGlite('file://' + copyDir.replace(/\\/g, '/')), copyDir };
}

async function countWs(pgdata) {
  const { db, copyDir } = openCopy(pgdata);
  let envs = -1, projects = -1;
  try {
    envs = (await db.query('SELECT count(*)::int n FROM t24_stream_envs')).rows[0].n;
    projects = (await db.query('SELECT count(*)::int n FROM projects')).rows[0].n;
  } catch { /* corrupt / unreadable */ }
  await db.close(); rmSync(copyDir, { recursive: true, force: true });
  return { envs, projects };
}

const action = process.argv[2];

if (action === 'status') {
  const out = [];
  for (const name of (existsSync(WORKSPACES) ? readdirSync(WORKSPACES) : [])) {
    const pg = join(WORKSPACES, name, 'pgdata');
    if (!existsSync(pg)) continue;
    const corrupt = readdirSync(join(WORKSPACES, name)).filter(n => n.startsWith('pgdata.corrupt'));
    const { envs, projects } = await countWs(pg);
    out.push({ workspace: name, pgdata: pg, mtime: statSync(pg).mtimeMs,
               connections: envs, projects, corrupt_snapshots: corrupt });
  }
  out.sort((a, b) => b.mtime - a.mtime);
  if (out.length) out[0].active = true;          // Codittle uses the most-recent pgdata
  console.log(JSON.stringify(out, null, 2));

} else if (action === 'connections') {
  const { db, copyDir } = openCopy(process.argv[3]);
  const rows = (await db.query('SELECT name, host, port, username, ssh_home FROM t24_stream_envs ORDER BY created_at')).rows;
  await db.close(); rmSync(copyDir, { recursive: true, force: true });
  console.log(JSON.stringify(rows, null, 2));

} else if (action === 'restore') {
  const [src, dst, mode] = process.argv.slice(3);
  const { db: srcDb, copyDir } = openCopy(src);
  const envs = (await srcDb.query('SELECT * FROM t24_stream_envs ORDER BY created_at')).rows;
  await srcDb.close(); rmSync(copyDir, { recursive: true, force: true });

  if (mode !== 'apply') {
    console.log(JSON.stringify({ dry_run: true, would_restore: envs.length, names: envs.map(e => e.name) }, null, 2));
    process.exit(0);
  }
  const dpid = join(dst, 'postmaster.pid'); if (existsSync(dpid)) rmSync(dpid);
  const dstDb = new PGlite('file://' + dst.replace(/\\/g, '/'));
  const stream = (await dstDb.query('SELECT id FROM t24_release_streams ORDER BY created_at LIMIT 1')).rows[0];
  if (!stream) { console.error('no release stream in destination'); process.exit(1); }
  const existing = (await dstDb.query('SELECT host FROM t24_stream_envs')).rows.map(r => r.host);
  let added = 0; const names = [];
  for (const e of envs) {
    if (existing.includes(e.host)) continue;     // don't duplicate
    const id = 'env_' + Date.now().toString(36) + Math.random().toString(36).slice(2, 8);
    const now = new Date().toISOString();
    await dstDb.query(
      `INSERT INTO t24_stream_envs
        (id, stream_id, env_class, host, port, username, password_enc, remote_roots, ssh_home, exec_setup, name, last_viewed_at, created_at, updated_at)
       VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13,$14)`,
      [id, stream.id, e.env_class, e.host, e.port, e.username, e.password_enc ?? null,
       e.remote_roots ?? null, e.ssh_home ?? null, e.exec_setup ?? null, e.name, null, now, now]);
    added++; names.push(e.name);
  }
  const total = (await dstDb.query('SELECT count(*)::int n FROM t24_stream_envs')).rows[0].n;
  await dstDb.close();
  if (existsSync(dpid)) rmSync(dpid);            // leave it unlocked for Codittle
  console.log(JSON.stringify({ applied: true, added, total, names }, null, 2));

} else {
  console.error('usage: status | connections <pgdata> | restore <src> <dst> <dry|apply>');
  process.exit(2);
}
