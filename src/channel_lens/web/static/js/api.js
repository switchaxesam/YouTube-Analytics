/* API client.
 *
 * The whole point of this module is that a failed request arrives as an
 * ApiError carrying `message` and `hint` — the same two fields the backend's
 * typed errors produce. Every caller can therefore show a real explanation
 * without knowing which endpoint failed or why.
 */

export class ApiError extends Error {
  constructor(status, payload) {
    super(payload?.message || payload?.detail || `Request failed (${status})`);
    this.name = 'ApiError';
    this.status = status;
    this.hint = payload?.hint || '';
    this.kind = payload?.error || '';
    this.payload = payload || {};
  }

  /** Quota failures get their own affordance: waiting is the fix, not retrying. */
  get isQuota() {
    return this.status === 429 || this.kind === 'QuotaExceeded' ||
           this.kind === 'UpstreamQuotaExceeded' || this.kind === 'OperationTooExpensive';
  }

  get isUnconfigured() {
    return this.status === 409 || this.kind === 'NotConfigured';
  }

  get isAuth() {
    return this.status === 401 || this.kind === 'NotAuthorised';
  }
}

async function request(method, path, { body, params, signal } = {}) {
  const url = new URL(path, window.location.origin);
  if (params) {
    for (const [key, value] of Object.entries(params)) {
      if (value === undefined || value === null || value === '') continue;
      if (Array.isArray(value)) value.forEach((v) => url.searchParams.append(key, v));
      else url.searchParams.set(key, value);
    }
  }

  let response;
  try {
    response = await fetch(url, {
      method,
      headers: body ? { 'Content-Type': 'application/json' } : undefined,
      body: body ? JSON.stringify(body) : undefined,
      signal,
    });
  } catch (err) {
    if (err.name === 'AbortError') throw err;
    // The server is on localhost, so this almost always means it stopped.
    throw new ApiError(0, {
      message: 'Lost contact with Channel Lens.',
      hint: 'The local server may have stopped. Check the terminal you started it from.',
    });
  }

  if (response.status === 204) return null;

  let payload = null;
  const type = response.headers.get('content-type') || '';
  if (type.includes('application/json')) {
    try { payload = await response.json(); } catch { payload = null; }
  }

  if (!response.ok) throw new ApiError(response.status, payload);
  return payload;
}

export const api = {
  get:  (path, params, opts)      => request('GET', path, { params, ...opts }),
  post: (path, body, opts)        => request('POST', path, { body, ...opts }),
  put:  (path, body, opts)        => request('PUT', path, { body, ...opts }),
  del:  (path, params, opts)      => request('DELETE', path, { params, ...opts }),
};
