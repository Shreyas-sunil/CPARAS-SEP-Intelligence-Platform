/**
 * Authentication & user-administration API client (v0.5.0).
 *
 * Talks to the backend's /api/auth/* endpoints (backend/api/routes/shared/auth.py
 * and users.py). The session itself is an HttpOnly cookie the browser stores
 * and sends automatically (same origin via the Vite / nginx /api proxy), so
 * nothing here ever touches or stores the session token.
 *
 * Permission strings mirror backend/auth/roles.py; the frontend never decides
 * access on its own — it only hides what the server already refuses.
 */
import axios from 'axios';
import { installAuthInterceptor } from './http';

const api = axios.create({ baseURL: '/api/auth', timeout: 30_000 });
installAuthInterceptor(api, { ignore401For: ['/login', '/me', '/setup', '/setup-status'] });

/** The three role identifiers (backend/auth/roles.py Role). */
export type RoleId = 'guest' | 'data_entry' | 'super_admin';

/** Stable permission identifiers (backend/auth/roles.py). */
export type Permission =
  | 'isld.use' | 'bulk.use' | 'reference.view' | 'reference.manage'
  | 'models.pull' | 'users.manage';

/** A user as returned by the backend (never contains password data). */
export interface AuthUser {
  id: number;
  username: string;
  display_name: string;
  role: RoleId;
  role_label: string;
  permissions: Permission[];
  /** Where to go after sign-in: '/isld' for guests, '/' otherwise. */
  landing_path: string;
  is_active: boolean;
  must_change_password: boolean;
  created_at: string | null;
  last_login_at: string | null;
  created_by: string | null;
}

/** Role catalogue entry (GET /api/auth/roles). */
export interface RoleInfo { id: RoleId; label: string; description: string; permissions: Permission[] }

/** GET /api/auth/setup-status (public). */
export interface SetupStatus {
  needs_setup: boolean;
  password_min_length: number;
  setup_token_file?: string | null;
  cli_command?: string;
}

/** One audit-trail entry (GET /api/auth/events). */
export interface AuthEvent {
  id: number; at: string | null; event: string; username: string | null;
  actor: string | null; client_ip: string | null; detail: string | null;
}

// ── Session ──────────────────────────────────────────────────────────────────

/** Sign in; the backend sets the session cookie. */
export const login = (username: string, password: string): Promise<AuthUser> =>
  api.post('/login', { username, password }).then(r => r.data.user);

/** End this session and clear the cookie. */
export const logout = (): Promise<void> => api.post('/logout').then(() => undefined);

/** The signed-in user, or null when there is no valid session (HTTP 401). */
export async function fetchMe(): Promise<AuthUser | null> {
  try {
    return (await api.get('/me')).data.user;
  } catch (e: any) {
    if (e?.response?.status === 401) return null;
    throw e;
  }
}

/** Change your own password (other sessions are signed out). */
export const changePassword = (current_password: string, new_password: string): Promise<AuthUser> =>
  api.post('/password', { current_password, new_password }).then(r => r.data.user);

// ── First-run setup ──────────────────────────────────────────────────────────

export const getSetupStatus = (): Promise<SetupStatus> => api.get('/setup-status').then(r => r.data);

/** Create the first Super Admin with the one-time token from the server's token file. */
export const completeSetup = (body: { setup_token: string; username: string; password: string; display_name?: string }): Promise<AuthUser> =>
  api.post('/setup', body).then(r => r.data.user);

// ── User administration (Super Admin) ────────────────────────────────────────

export const listRoles = (): Promise<RoleInfo[]> => api.get('/roles').then(r => r.data.roles);
/** GET /api/auth/users. */
export const listUsers = (): Promise<AuthUser[]> => api.get('/users').then(r => r.data.users);

/** Create a user; omit `password` to get a generated one back (shown once). */
export const createUser = (body: { username: string; role: RoleId; password?: string; display_name?: string; must_change_password?: boolean }):
  Promise<{ user: AuthUser; generated_password?: string }> => api.post('/users', body).then(r => r.data);

/** PATCH /api/auth/users/{id} - role / active flag / display name. */
export const updateUser = (id: number, body: { role?: RoleId; is_active?: boolean; display_name?: string }):
  Promise<{ user: AuthUser; changes: string[] }> => api.patch(`/users/${id}`, body).then(r => r.data);

/** Administrator password reset; omit `password` to generate one (shown once). */
export const resetUserPassword = (id: number, body: { password?: string; must_change_password?: boolean } = {}):
  Promise<{ user: AuthUser; generated_password?: string }> => api.post(`/users/${id}/password`, body).then(r => r.data);

/** DELETE /api/auth/users/{id}. */
export const deleteUser = (id: number): Promise<void> => api.delete(`/users/${id}`).then(() => undefined);

/** GET /api/auth/events - newest first. */
export const listAuthEvents = (limit = 100): Promise<AuthEvent[]> =>
  api.get('/events', { params: { limit } }).then(r => r.data.events);

/** Human-readable message from an axios error (backend `detail` is always a string for auth errors). */
export function authErrorMessage(e: any, fallback = 'Request failed.'): string {
  const d = e?.response?.data?.detail;
  if (typeof d === 'string' && d.trim()) return d;
  if (Array.isArray(d) && d.length) return d.map((x: any) => x?.msg ?? String(x)).join('; ');
  if (!e?.response) return `${fallback} The backend could not be reached.`;
  return `${fallback} (HTTP ${e.response.status})`;
}
