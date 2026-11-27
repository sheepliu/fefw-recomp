// SPDX-License-Identifier: GPL-3.0-or-later
// Cloudflare Worker for anonymized fefw-recomp crash & performance reports.
// Stores zstd-compressed report bundles in R2 and queryable metadata in D1.
// Raw client IP addresses are never logged or stored (only a daily salted SHA-256 hash is used for rate limiting).

const MAX_BODY_BYTES = 1_572_864; // 1.5 MiB compressed
const MAX_REPORTS_PER_IP_PER_DAY = 10;
const MAX_REPORTS_GLOBAL_PER_DAY = 500;
const MAX_TOTAL_REPORTS = 20_000;
const MAX_TOTAL_STORAGE_BYTES = 8 * 1024 * 1024 * 1024; // 8 GiB cap (stays under 10 GiB R2 free tier)
const RETENTION_DAYS = 90;
const ALLOWED_TRIGGERS = new Set(['crash', 'freeze', 'fatal', 'exit_code', 'hitch', 'low_fps']);

function json(data, status = 200) {
  return new Response(JSON.stringify(data), {
    status,
    headers: { 'content-type': 'application/json; charset=utf-8' },
  });
}

function cleanStr(val, maxLen = 128) {
  if (typeof val !== 'string') return '';
  return val.replace(/[\x00-\x1f\x7f-\x9f]/g, ' ').trim().slice(0, maxLen);
}

function cleanNum(val) {
  return typeof val === 'number' && Number.isFinite(val) ? val : null;
}

function cleanInt(val) {
  return typeof val === 'number' && Number.isInteger(val) ? val : null;
}

function safePathSegment(val, fallback = 'unknown') {
  const s = cleanStr(val, 64).replace(/[^A-Za-z0-9._-]/g, '_');
  return s || fallback;
}

function decodeMetaHeader(raw) {
  if (!raw || raw.length > 8192) return null;
  try {
    const bin = atob(raw.trim());
    const bytes = new Uint8Array(bin.length);
    for (let i = 0; i < bin.length; i++) bytes[i] = bin.charCodeAt(i);
    const text = new TextDecoder().decode(bytes);
    const parsed = JSON.parse(text);
    return parsed && typeof parsed === 'object' ? parsed : null;
  } catch {
    return null;
  }
}

function timingSafeEqual(a, b) {
  if (typeof a !== 'string' || typeof b !== 'string') return false;
  const enc = new TextEncoder();
  const ab = enc.encode(a);
  const bb = enc.encode(b);
  if (ab.length !== bb.length) return false;
  let diff = 0;
  for (let i = 0; i < ab.length; i++) diff |= ab[i] ^ bb[i];
  return diff === 0;
}

function isAdmin(request, env) {
  const secret = env.ADMIN_TOKEN;
  if (!secret || typeof secret !== 'string' || secret.length < 16) return false;
  const auth = request.headers.get('authorization') || '';
  if (!auth.startsWith('Bearer ')) return false;
  return timingSafeEqual(auth.slice(7), secret);
}

async function dailyIpBucket(request, env, day) {
  const ip = request.headers.get('cf-connecting-ip') || 'unknown';
  const salt = typeof env.ADMIN_TOKEN === 'string' && env.ADMIN_TOKEN ? env.ADMIN_TOKEN : 'fefw-default-salt';
  const data = new TextEncoder().encode(`${day}:${salt}:${ip}`);
  const digest = await crypto.subtle.digest('SHA-256', data);
  const hex = Array.from(new Uint8Array(digest).slice(0, 12))
    .map((b) => b.toString(16).padStart(2, '0'))
    .join('');
  return `ip:${day}:${hex}`;
}

async function checkAndConsumeLimits(request, env, day, incomingBytes) {
  // Prune previous days' rate-limit counters and expired D1 rows (matching the 90-day R2 lifecycle rule).
  const cutoffIso = new Date(Date.now() - RETENTION_DAYS * 86_400_000).toISOString();
  await env.REPORTS_DB.prepare('DELETE FROM rate_limits WHERE day < ?').bind(day).run();
  await env.REPORTS_DB.prepare('DELETE FROM reports WHERE created_at < ?').bind(cutoffIso).run();

  // Enforce total storage and row budget before accepting a new upload.
  const totals = await env.REPORTS_DB.prepare(
    'SELECT COUNT(*) AS cnt, COALESCE(SUM(size_bytes), 0) AS bytes FROM reports'
  ).first();
  if (
    totals &&
    (Number(totals.cnt) >= MAX_TOTAL_REPORTS || Number(totals.bytes) + incomingBytes > MAX_TOTAL_STORAGE_BYTES)
  ) {
    return json({ error: 'storage budget reached' }, 507);
  }

  // Enforce per-IP daily limit first so a rate-limited caller cannot burn the shared global allowance.
  const ipKey = await dailyIpBucket(request, env, day);
  const ipRes = await env.REPORTS_DB.prepare(
    `INSERT INTO rate_limits (bucket_key, day, count) VALUES (?, ?, 1)
     ON CONFLICT(bucket_key) DO UPDATE SET count = count + 1
     WHERE count < ?`
  )
    .bind(ipKey, day, MAX_REPORTS_PER_IP_PER_DAY)
    .run();
  if (!ipRes.meta || ipRes.meta.changes === 0) {
    return json({ error: 'rate limit exceeded' }, 429);
  }

  // Enforce global daily limit atomically.
  const globalKey = `global:${day}`;
  const globalRes = await env.REPORTS_DB.prepare(
    `INSERT INTO rate_limits (bucket_key, day, count) VALUES (?, ?, 1)
     ON CONFLICT(bucket_key) DO UPDATE SET count = count + 1
     WHERE count < ?`
  )
    .bind(globalKey, day, MAX_REPORTS_GLOBAL_PER_DAY)
    .run();
  if (!globalRes.meta || globalRes.meta.changes === 0) {
    return json({ error: 'daily global limit reached' }, 429);
  }

  return null;
}

async function readBoundedBody(request, maxBytes) {
  if (!request.body) return new Uint8Array(0);
  const reader = request.body.getReader();
  const chunks = [];
  let total = 0;
  try {
    while (true) {
      const { done, value } = await reader.read();
      if (done) break;
      total += value.byteLength;
      if (total > maxBytes) {
        await reader.cancel().catch(() => {});
        return null;
      }
      chunks.push(value);
    }
  } finally {
    reader.releaseLock();
  }
  const out = new Uint8Array(total);
  let offset = 0;
  for (const c of chunks) {
    out.set(c, offset);
    offset += c.byteLength;
  }
  return out;
}

async function handleSubmit(request, env) {
  const contentLen = Number(request.headers.get('content-length') || '0');
  if (contentLen > MAX_BODY_BYTES) {
    return json({ error: 'payload too large' }, 413);
  }

  const meta = decodeMetaHeader(request.headers.get('x-report-meta'));
  if (!meta) {
    return json({ error: 'invalid or missing X-Report-Meta header' }, 400);
  }

  const trigger = cleanStr(meta.trigger, 32);
  if (!ALLOWED_TRIGGERS.has(trigger)) {
    return json({ error: 'invalid trigger' }, 400);
  }

  const body = await readBoundedBody(request, MAX_BODY_BYTES);
  if (body === null) {
    return json({ error: 'payload too large' }, 413);
  }
  if (body.byteLength === 0) {
    return json({ error: 'invalid payload size' }, 400);
  }

  // Zstd magic number: 28 B5 2F FD
  if (body.byteLength < 4 || body[0] !== 0x28 || body[1] !== 0xb5 || body[2] !== 0x2f || body[3] !== 0xfd) {
    return json({ error: 'expected zstd-compressed body' }, 400);
  }

  const createdAt = new Date().toISOString();
  const datePrefix = createdAt.slice(0, 10);

  const limitErr = await checkAndConsumeLimits(request, env, datePrefix, body.byteLength);
  if (limitErr) return limitErr;

  const id = crypto.randomUUID();
  const buildId = cleanStr(meta.build_id, 64) || 'unknown';
  const launcherVersion = cleanStr(meta.launcher_version, 32) || 'unknown';
  const os = cleanStr(meta.os, 32) || 'unknown';
  const gpu = cleanStr(meta.gpu, 128) || 'unknown';
  const exitCode = cleanInt(meta.exit_code);
  const meanFps = cleanNum(meta.mean_fps);
  const low1Fps = cleanNum(meta.low1_fps);
  const worstMs = cleanNum(meta.worst_ms);
  const hitchCount = cleanInt(meta.hitch_count);
  const crashCause = cleanStr(meta.crash_cause, 256) || null;

  const r2Key = `${datePrefix}/${trigger}/${safePathSegment(buildId)}/${id}.zst`;

  await env.REPORTS_BUCKET.put(r2Key, body, {
    httpMetadata: { contentType: 'application/zstd' },
    customMetadata: { id, trigger, build_id: buildId, os },
  });

  try {
    await env.REPORTS_DB.prepare(
      `INSERT INTO reports (
         id, created_at, trigger, build_id, launcher_version, os, gpu,
         exit_code, mean_fps, low1_fps, worst_ms, hitch_count, crash_cause,
         r2_key, size_bytes
       ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)`
    )
      .bind(
        id,
        createdAt,
        trigger,
        buildId,
        launcherVersion,
        os,
        gpu,
        exitCode,
        meanFps,
        low1Fps,
        worstMs,
        hitchCount,
        crashCause,
        r2Key,
        body.byteLength
      )
      .run();
  } catch {
    // Avoid leaving an unlisted R2 object if the D1 insert fails.
    await env.REPORTS_BUCKET.delete(r2Key).catch(() => {});
    return json({ error: 'failed to index report' }, 500);
  }

  return json({ ok: true, id }, 202);
}

async function handleList(request, env) {
  const url = new URL(request.url);
  const clauses = [];
  const binds = [];

  const trigger = url.searchParams.get('trigger');
  if (trigger) {
    clauses.push('trigger = ?');
    binds.push(trigger);
  }
  const buildId = url.searchParams.get('build_id');
  if (buildId) {
    clauses.push('build_id LIKE ?');
    binds.push(`%${buildId}%`);
  }
  const gpu = url.searchParams.get('gpu');
  if (gpu) {
    clauses.push('gpu LIKE ?');
    binds.push(`%${gpu}%`);
  }
  const os = url.searchParams.get('os');
  if (os) {
    clauses.push('os = ?');
    binds.push(os);
  }
  const since = url.searchParams.get('since');
  if (since) {
    clauses.push('created_at >= ?');
    binds.push(since);
  }

  const limit = Math.min(Math.max(parseInt(url.searchParams.get('limit') || '50', 10) || 50, 1), 200);
  const offset = Math.max(parseInt(url.searchParams.get('offset') || '0', 10) || 0, 0);
  const where = clauses.length ? `WHERE ${clauses.join(' AND ')}` : '';
  const sql = `SELECT * FROM reports ${where} ORDER BY created_at DESC LIMIT ? OFFSET ?`;
  binds.push(limit, offset);

  const res = await env.REPORTS_DB.prepare(sql).bind(...binds).all();
  return json({ reports: res.results || [] });
}

async function handleGetOne(id, env) {
  const row = await env.REPORTS_DB.prepare('SELECT r2_key FROM reports WHERE id = ?').bind(id).first();
  if (!row) return json({ error: 'not found' }, 404);
  const obj = await env.REPORTS_BUCKET.get(row.r2_key);
  if (!obj) return json({ error: 'blob not found' }, 404);
  return new Response(obj.body, {
    status: 200,
    headers: {
      'content-type': 'application/zstd',
      'content-disposition': `attachment; filename="${id}.json.zst"`,
    },
  });
}

async function handleDeleteOne(id, env) {
  const row = await env.REPORTS_DB.prepare('SELECT r2_key FROM reports WHERE id = ?').bind(id).first();
  if (!row) return json({ error: 'not found' }, 404);
  await env.REPORTS_BUCKET.delete(row.r2_key);
  await env.REPORTS_DB.prepare('DELETE FROM reports WHERE id = ?').bind(id).run();
  return json({ ok: true, id });
}

export default {
  async fetch(request, env) {
    const url = new URL(request.url);
    const path = url.pathname;

    if (request.method === 'GET' && path === '/health') {
      return json({ ok: true });
    }

    if (request.method === 'POST' && path === '/v1/report') {
      return handleSubmit(request, env);
    }

    if (path === '/v1/reports' && request.method === 'GET') {
      if (!isAdmin(request, env)) return json({ error: 'unauthorized' }, 401);
      return handleList(request, env);
    }

    const match = path.match(/^\/v1\/reports\/([0-9a-f-]{36})$/);
    if (match) {
      if (!isAdmin(request, env)) return json({ error: 'unauthorized' }, 401);
      if (request.method === 'GET') return handleGetOne(match[1], env);
      if (request.method === 'DELETE') return handleDeleteOne(match[1], env);
    }

    return json({ error: 'not found' }, 404);
  },
};
