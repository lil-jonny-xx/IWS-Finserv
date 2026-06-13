// SSE-over-fetch reader for POST /api/v1/assistant/chat.
//
// We use fetch + ReadableStream (not EventSource) because the endpoint is a POST
// that relies on the session cookie (credentials: 'include'). The backend writes
// frames as `data: <json>\n\n`; we buffer partial lines and yield typed events.

import type { AssistantEvent } from '../types';

const API_URL = process.env.NEXT_PUBLIC_API_URL || 'https://iwsfinserv.com';

export async function* streamChat(
  conversationId: number,
  message: string,
  signal: AbortSignal,
): AsyncGenerator<AssistantEvent> {
  const res = await fetch(`${API_URL}/api/v1/assistant/chat`, {
    method: 'POST',
    credentials: 'include',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ conversation_id: conversationId, message }),
    signal,
  });

  if (res.status === 401) {
    yield { type: 'error', message: 'Your session expired. Please sign in again.' };
    return;
  }
  if (!res.ok || !res.body) {
    let detail = `Request failed (${res.status}).`;
    try {
      const j = await res.json();
      if (j?.detail) detail = typeof j.detail === 'string' ? j.detail : detail;
    } catch {
      /* non-JSON body — keep the generic message */
    }
    yield { type: 'error', message: detail };
    return;
  }

  const reader = res.body.getReader();
  const decoder = new TextDecoder();
  let buffer = '';

  try {
    for (;;) {
      const { done, value } = await reader.read();
      if (done) break;
      buffer += decoder.decode(value, { stream: true });

      // SSE frames are separated by a blank line.
      let sep: number;
      while ((sep = buffer.indexOf('\n\n')) !== -1) {
        const frame = buffer.slice(0, sep);
        buffer = buffer.slice(sep + 2);

        // A frame may contain multiple `data:` lines; concatenate their payloads.
        const data = frame
          .split('\n')
          .filter(l => l.startsWith('data:'))
          .map(l => l.slice(5).trimStart())
          .join('');
        if (!data) continue;

        try {
          yield JSON.parse(data) as AssistantEvent;
        } catch {
          /* ignore malformed frame */
        }
      }
    }
  } finally {
    try {
      await reader.cancel();
    } catch {
      /* already closed */
    }
  }
}
