/**
 * AuthProvider / useAuth — the signed-in user for the whole React app (v0.5.0).
 *
 * On load it asks the backend who is signed in (GET /api/auth/me; the session
 * cookie is sent automatically). Components use:
 *
 *   const { user, can, login, logout } = useAuth();
 *   if (can('bulk.use')) …
 *
 * `can()` reads the permission list the SERVER returned for this user, so the
 * UI and the backend policy (backend/auth/policy.py) can never disagree about
 * the role table — the frontend only hides what the backend already blocks.
 */
import { createContext, useCallback, useContext, useEffect, useMemo, useRef, useState, type ReactNode } from 'react';
import * as authApi from './authApi';
import type { AuthUser, Permission } from './authApi';
import { AUTH_EXPIRED_EVENT } from './http';

type AuthStatus = 'loading' | 'anonymous' | 'authenticated' | 'error';

interface AuthState {
  status: AuthStatus;
  user: AuthUser | null;
  /** Why the user was signed out (shown on the sign-in page), e.g. session expiry. */
  notice: string | null;
  /** Backend unreachable while checking the session. */
  error: string | null;
  can: (permission: Permission) => boolean;
  login: (username: string, password: string) => Promise<AuthUser>;
  /** Adopt a user returned by another auth call (first-run setup, password change). */
  setSignedIn: (user: AuthUser) => void;
  logout: () => Promise<void>;
  refresh: () => Promise<void>;
}

const AuthContext = createContext<AuthState | null>(null);

/** Provides the auth state to the app; checks the session once on load. */
export function AuthProvider({ children }: { children: ReactNode }) {
  const [status, setStatus] = useState<AuthStatus>('loading');
  const [user, setUser] = useState<AuthUser | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  // Mirrors `user` for the session-expiry listener (registered once).
  const userRef = useRef<AuthUser | null>(null);
  useEffect(() => { userRef.current = user; }, [user]);

  const refresh = useCallback(async () => {
    try {
      const me = await authApi.fetchMe();
      setUser(me);
      setStatus(me ? 'authenticated' : 'anonymous');
      setError(null);
    } catch (e) {
      setStatus('error');
      setError(authApi.authErrorMessage(e, 'Could not check your sign-in.'));
    }
  }, []);

  useEffect(() => { void refresh(); }, [refresh]);

  // A 401 from any API call (expired/revoked session) signs the UI out.
  useEffect(() => {
    const onExpired = () => {
      if (userRef.current) setNotice('Your session ended (it expired, or an administrator changed your account). Please sign in again.');
      setUser(null);
      setStatus('anonymous');
    };
    window.addEventListener(AUTH_EXPIRED_EVENT, onExpired);
    return () => window.removeEventListener(AUTH_EXPIRED_EVENT, onExpired);
  }, []);

  const login = useCallback(async (username: string, password: string) => {
    const u = await authApi.login(username, password);
    setUser(u); setStatus('authenticated'); setNotice(null); setError(null);
    return u;
  }, []);

  const setSignedIn = useCallback((u: AuthUser) => { setUser(u); setStatus('authenticated'); setNotice(null); }, []);

  const logout = useCallback(async () => {
    try { await authApi.logout(); } catch { /* already signed out server-side */ }
    setUser(null); setStatus('anonymous'); setNotice('You have signed out.');
  }, []);

  const can = useCallback((p: Permission) => !!user?.permissions.includes(p), [user]);

  const value = useMemo<AuthState>(() => ({ status, user, notice, error, can, login, setSignedIn, logout, refresh }),
    [status, user, notice, error, can, login, setSignedIn, logout, refresh]);
  return <AuthContext.Provider value={value}>{children}</AuthContext.Provider>;
}

/** The current auth state (must be used inside <AuthProvider>). */
export function useAuth(): AuthState {
  const ctx = useContext(AuthContext);
  if (!ctx) throw new Error('useAuth() must be used inside <AuthProvider>.');
  return ctx;
}

/** Render children only when the signed-in user has `permission` (otherwise `fallback`). */
export function Gate({ permission, children, fallback = null }: { permission: Permission; children: ReactNode; fallback?: ReactNode }) {
  const { can } = useAuth();
  return <>{can(permission) ? children : fallback}</>;
}
