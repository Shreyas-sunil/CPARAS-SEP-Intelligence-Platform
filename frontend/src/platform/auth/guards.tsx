/**
 * Route guards (v0.5.0).
 *
 *   <RequireAuth>            wraps the whole app shell: no session -> /login
 *                            (remembering the requested page); backend
 *                            unreachable -> a retry screen; a user whose
 *                            password was set by an administrator must choose
 *                            their own before continuing.
 *   <RequirePermission p>    wraps one route: without permission `p` the page
 *                            is replaced by a "no access" notice (direct URL
 *                            access to e.g. /bulk as a Guest). The API refuses
 *                            the same calls with 403 regardless.
 *   <HomeRoute>              "/" — the platform home for users who may use
 *                            Bulk; everyone else goes to their landing page
 *                            (Guests: ISLD Declaration Analysis).
 */
import type { ReactNode } from 'react';
import { Link, Navigate, useLocation } from 'react-router-dom';
import { Lock, RefreshCw } from 'lucide-react';
import { useTheme } from '../theme';
import { Banner, EmptyState, LoadingState } from '../ui';
import { useAuth } from './AuthContext';
import type { Permission } from './authApi';
import ChangePasswordDialog from './ChangePasswordDialog';

/** Signed-in users only; everyone else is sent to /login. */
export function RequireAuth({ children }: { children: ReactNode }) {
  const { status, user, error, refresh } = useAuth();
  const { theme } = useTheme();
  const location = useLocation();

  if (status === 'loading') {
    return <div className="app-root" data-theme={theme}><div className="auth-screen"><LoadingState label="Checking your sign-in…" /></div></div>;
  }
  if (status === 'error') {
    return (
      <div className="app-root" data-theme={theme}>
        <div className="auth-screen">
          <div className="auth-card">
            <Banner tone="err" title="Cannot reach the CPARAS backend">{error}</Banner>
            <button className="btn btn-block" style={{ marginTop: 12 }} onClick={() => void refresh()}><RefreshCw size={14} /> Try again</button>
          </div>
        </div>
      </div>
    );
  }
  if (status !== 'authenticated' || !user) {
    return <Navigate to="/login" replace state={{ from: location.pathname + location.search }} />;
  }
  return (
    <>
      {children}
      {user.must_change_password && <ChangePasswordDialog forced />}
    </>
  );
}

/** Render the page only with `permission`; otherwise a 'no access' notice. */
export function RequirePermission({ permission, children }: { permission: Permission; children: ReactNode }) {
  const { can, user } = useAuth();
  if (can(permission)) return <>{children}</>;
  return (
    <div className="page">
      <EmptyState icon={<Lock size={22} />} title="You don’t have access to this page"
        body={`Your role (${user?.role_label ?? 'unknown'}) cannot open this part of CPARAS. Ask a Super Admin if you need access.`}
        action={<Link className="btn btn-primary" to={user?.landing_path ?? '/'}>Go to my start page</Link>} />
    </div>
  );
}

/** Platform home for Bulk users, landing page for everyone else. */
export function HomeRoute({ children }: { children: ReactNode }) {
  const { can, user } = useAuth();
  if (!can('bulk.use')) return <Navigate to={user?.landing_path ?? '/isld'} replace />;
  return <>{children}</>;
}
