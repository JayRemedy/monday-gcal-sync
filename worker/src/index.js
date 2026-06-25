export default {
  async fetch(request, env, ctx) {
    if (request.method === 'GET') {
      return json({ ok: true, service: 'monday-gcal-webhook' });
    }

    if (request.method !== 'POST') {
      return json({ error: 'method not allowed' }, 405);
    }

    const url = new URL(request.url);

    if (env.GOOGLE_WEBHOOK_PATH && url.pathname === env.GOOGLE_WEBHOOK_PATH) {
      const expectedToken = env.GOOGLE_CHANNEL_TOKEN || '';
      const gotToken = request.headers.get('x-goog-channel-token') || url.searchParams.get('secret') || '';
      if (expectedToken && gotToken !== expectedToken) {
        return json({ error: 'unauthorized' }, 401);
      }
      ctx.waitUntil(dispatchGitHub(env, {
        event_type: 'google_calendar_webhook',
        client_payload: {
          received_at: new Date().toISOString(),
          google_resource_state: request.headers.get('x-goog-resource-state') || '',
          google_resource_id: request.headers.get('x-goog-resource-id') || '',
          google_channel_id: request.headers.get('x-goog-channel-id') || '',
        },
      }));
      return json({ ok: true, dispatched: true, source: 'google_calendar' });
    }

    if (env.WEBHOOK_PATH && url.pathname !== env.WEBHOOK_PATH) {
      return json({ error: 'not found' }, 404);
    }

    let payload;
    try {
      payload = await request.json();
    } catch (_) {
      return json({ error: 'bad json' }, 400);
    }

    // monday.com webhook verification. Must answer immediately with the same challenge.
    if (payload && Object.prototype.hasOwnProperty.call(payload, 'challenge')) {
      return json({ challenge: payload.challenge });
    }

    const event = payload?.event || {};
    const boardIds = candidateBoardIds(event);
    const allowedBoardIds = String(env.MONDAY_BOARD_IDS || env.MONDAY_BOARD_ID || '')
      .split(',')
      .map((id) => id.trim())
      .filter(Boolean);
    if (allowedBoardIds.length && boardIds.length && !boardIds.some((id) => allowedBoardIds.includes(id))) {
      return json({ ok: true, ignored: true, reason: 'different board', board_ids: boardIds });
    }

    // Basic shared-secret guard. Use a hard-to-guess path plus this header if you later
    // front the webhook yourself. monday.com itself does not sign webhooks, so path
    // entropy is still important.
    if (env.INBOUND_SECRET) {
      const got = request.headers.get('x-webhook-secret') || url.searchParams.get('secret') || '';
      if (got !== env.INBOUND_SECRET) {
        return json({ error: 'unauthorized' }, 401);
      }
    }

    ctx.waitUntil(dispatchGitHub(env, {
      event_type: 'monday_webhook',
      client_payload: {
        received_at: new Date().toISOString(),
        monday_event_type: payload?.event?.type || payload?.type || 'unknown',
        board_id: payload?.event?.boardId || payload?.event?.board_id || '',
        pulse_id: payload?.event?.pulseId || payload?.event?.itemId || payload?.event?.pulse_id || '',
      },
    }));
    return json({ ok: true, dispatched: true });
  },
};

async function dispatchGitHub(env, dispatch) {
  const owner = required(env, 'GITHUB_OWNER');
  const repo = required(env, 'GITHUB_REPO');
  const token = required(env, 'GITHUB_DISPATCH_TOKEN');
  const url = `https://api.github.com/repos/${owner}/${repo}/dispatches`;
  const res = await fetch(url, {
    method: 'POST',
    headers: {
      'Accept': 'application/vnd.github+json',
      'Authorization': `Bearer ${token}`,
      'Content-Type': 'application/json',
      'User-Agent': 'monday-gcal-cloudflare-worker',
      'X-GitHub-Api-Version': '2022-11-28',
    },
    body: JSON.stringify(dispatch),
  });
  if (!res.ok) {
    const text = await res.text();
    throw new Error(`GitHub dispatch failed: ${res.status} ${text}`);
  }
}

function candidateBoardIds(event) {
  const candidates = [
    event.boardId,
    event.board_id,
    event.parentBoardId,
    event.parent_board_id,
    event.parentItemBoardId,
    event.parent_item_board_id,
  ];
  return [...new Set(candidates.filter((v) => v !== undefined && v !== null && String(v).trim() !== '').map((v) => String(v)))];
}

function required(env, name) {
  const val = env[name];
  if (!val) throw new Error(`missing env ${name}`);
  return val;
}

function json(payload, status = 200) {
  return new Response(JSON.stringify(payload), {
    status,
    headers: { 'content-type': 'application/json; charset=utf-8' },
  });
}
