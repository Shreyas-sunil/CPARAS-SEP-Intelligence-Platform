/**
 * Shared HTTP behaviour for authentication (v0.5.0).
 *
 * Every axios instance in the app (one per backend area: /api/sep, /api, …)
 * calls `installAuthInterceptor`. When the backend answers 401 with the
 * `X-CPARAS-Auth: auth_required` header — the session expired, was revoked by an
 * administrator, or the user signed out in another tab — the interceptor
 * broadcasts `AUTH_EXPIRED_EVENT`; AuthProvider (AuthContext.tsx) listens and
 * returns the user to the sign-in page. The rejected promise still propagates,
 * so each screen's existing error handling keeps working unchanged.
 *
 * Integration point: a host platform embedding CPARAS can listen for the same
 * event to trigger its own single-sign-on flow instead.
 */
import type { AxiosInstance } from 'axios';

/** Window event fired when any API call reports an expired/revoked session. */
export const AUTH_EXPIRED_EVENT = 'cparas:auth-expired';

/** Add the session-expiry interceptor to an axios instance (returns the instance). */
export function installAuthInterceptor(instance: AxiosInstance, opts: { ignore401For?: string[] } = {}): AxiosInstance {
  instance.interceptors.response.use(undefined, (error) => {
    const res = error?.response;
    const url: string = error?.config?.url ?? '';
    const ignored = (opts.ignore401For ?? []).some(p => url === p || url.endsWith(p));
    if (res?.status === 401 && !ignored && res.headers?.['x-cparas-auth'] === 'auth_required') {
      window.dispatchEvent(new CustomEvent(AUTH_EXPIRED_EVENT));
    }
    return Promise.reject(error);
  });
  return instance;
}
