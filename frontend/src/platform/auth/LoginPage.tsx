/**
 * Sign-in page (/login) and first-time setup (v0.5.0).
 *
 * Two modes, chosen by GET /api/auth/setup-status:
 *   * Sign in — username + password. On success the user goes back to the page
 *     they originally asked for when their role allows it, otherwise to the
 *     server-provided landing page (guests: /isld Declaration Analysis).
 *   * First-time setup — only while NO account exists. Creates the first Super
 *     Admin with the one-time token the backend wrote to a file on the server
 *     machine (the page shows the file's path, never the token).
 *
 * Rendered outside AppShell (no navigation before sign-in) but inside the
 * same .app-root theme scope so it uses the platform design tokens.
 */
import { useEffect, useState, type FormEvent } from 'react';
import { Navigate, useLocation, useNavigate } from 'react-router-dom';
import { KeyRound, LogIn, ShieldCheck, UserPlus } from 'lucide-react';
import cdotLogo from '../../gitas/assets/cdot-logo-circle.png';
import { useTheme } from '../theme';
import { Banner } from '../ui';
import { PLATFORM_NAME, PLATFORM_SUBTITLE, PLATFORM_VERSION, isPathAllowed } from '../products';
import { useAuth } from './AuthContext';
import { authErrorMessage, completeSetup, getSetupStatus, type AuthUser, type SetupStatus } from './authApi';

/** Where to go after signing in: the requested page if permitted, else the role's landing page. */
export function postLoginPath(user: AuthUser, requested?: string | null): string {
  // "/" is only allowed with bulk.use, so a Guest who asked for the home page lands on ISLD.
  if (requested && requested !== '/login' && isPathAllowed(requested, p => user.permissions.includes(p))) return requested;
  return user.landing_path;
}

/** Sign-in / first-time setup page. */
export default function LoginPage() {
  const { theme } = useTheme();
  const { status, user, notice, login, setSignedIn } = useAuth();
  const navigate = useNavigate();
  const location = useLocation();
  const from = (location.state as { from?: string } | null)?.from ?? null;

  const [setup, setSetup] = useState<SetupStatus | null>(null);
  const [username, setUsername] = useState('');
  const [password, setPassword] = useState('');
  const [password2, setPassword2] = useState('');
  const [displayName, setDisplayName] = useState('');
  const [token, setToken] = useState('');
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    getSetupStatus().then(setSetup).catch(e => setError(authErrorMessage(e, 'Could not reach the CPARAS backend.')));
  }, []);

  if (status === 'authenticated' && user) return <Navigate to={postLoginPath(user, from)} replace />;

  const needsSetup = !!setup?.needs_setup;

  const onLogin = async (e: FormEvent) => {
    e.preventDefault();
    setBusy(true); setError(null);
    try {
      const u = await login(username.trim(), password);
      navigate(postLoginPath(u, from), { replace: true });
    } catch (err) {
      setError(authErrorMessage(err, 'Sign-in failed.'));
      setPassword('');
    } finally { setBusy(false); }
  };

  const onSetup = async (e: FormEvent) => {
    e.preventDefault();
    if (password !== password2) { setError('The two passwords differ.'); return; }
    setBusy(true); setError(null);
    try {
      const u = await completeSetup({ setup_token: token.trim(), username: username.trim(), password, display_name: displayName.trim() || undefined });
      setSignedIn(u);
      navigate('/admin/users', { replace: true });
    } catch (err) {
      setError(authErrorMessage(err, 'Setup failed.'));
    } finally { setBusy(false); }
  };

  return (
    <div className="app-root" data-theme={theme}>
      <div className="auth-screen">
        <div className="auth-card">
          <div className="auth-brand">
            <img src={cdotLogo} alt="C-DOT" />
            <div>
              <div className="auth-brand-name">{PLATFORM_NAME}</div>
              <div className="auth-brand-sub">{PLATFORM_SUBTITLE}</div>
            </div>
          </div>

          {needsSetup ? (
            <form onSubmit={onSetup} className="stack-sm" aria-labelledby="setup-title">
              <h1 id="setup-title" className="auth-title"><ShieldCheck size={18} /> First-time setup</h1>
              <p className="auth-lead">
                No CPARAS account exists yet. Create the first <b>Super Admin</b>; they can then add Guest,
                Data Entry and further Super Admin accounts.
              </p>
              <Banner tone="info" title="One-time setup token">
                Open this file on the computer that runs the CPARAS backend and paste its contents below:
                <code className="auth-path">{setup?.setup_token_file ?? 'backend/data/FIRST_ADMIN_SETUP_TOKEN.txt'}</code>
                Or skip this page and run <code>create_admin.cmd</code> (Windows) / <code>python -m auth.cli create-superadmin</code> there.
              </Banner>
              <label className="field">
                <span className="field-label">Setup token</span>
                <input className="input" value={token} onChange={e => setToken(e.target.value)} autoComplete="off" required spellCheck={false} />
              </label>
              <label className="field">
                <span className="field-label">Username</span>
                <input className="input" value={username} onChange={e => setUsername(e.target.value)} autoComplete="username" required
                  pattern="[A-Za-z0-9][A-Za-z0-9._\-]{2,63}" title="3-64 characters: letters, digits, '.', '_' or '-'" />
              </label>
              <label className="field">
                <span className="field-label">Display name <span className="muted">(optional)</span></span>
                <input className="input" value={displayName} onChange={e => setDisplayName(e.target.value)} autoComplete="name" />
              </label>
              <div className="field">
                <label className="field-label" htmlFor="setup-password">Password</label>
                <input id="setup-password" className="input" type="password" value={password} onChange={e => setPassword(e.target.value)} autoComplete="new-password" required
                  minLength={setup?.password_min_length ?? 10} aria-describedby="setup-password-hint" />
                <span className="field-hint" id="setup-password-hint">At least {setup?.password_min_length ?? 10} characters, mixing letters with digits or symbols.</span>
              </div>
              <label className="field">
                <span className="field-label">Repeat password</span>
                <input className="input" type="password" value={password2} onChange={e => setPassword2(e.target.value)} autoComplete="new-password" required />
              </label>
              {error && <Banner tone="err">{error}</Banner>}
              <button className="btn btn-primary btn-lg btn-block" type="submit" disabled={busy}>
                <UserPlus size={15} /> {busy ? 'Creating…' : 'Create Super Admin and sign in'}
              </button>
            </form>
          ) : (
            <form onSubmit={onLogin} className="stack-sm" aria-labelledby="login-title">
              <h1 id="login-title" className="auth-title"><KeyRound size={18} /> Sign in</h1>
              {notice && <Banner tone="info">{notice}</Banner>}
              <label className="field">
                <span className="field-label">Username</span>
                <input className="input" value={username} onChange={e => setUsername(e.target.value)} autoComplete="username" autoFocus required />
              </label>
              <label className="field">
                <span className="field-label">Password</span>
                <input className="input" type="password" value={password} onChange={e => setPassword(e.target.value)} autoComplete="current-password" required />
              </label>
              {error && <Banner tone="err">{error}</Banner>}
              <button className="btn btn-primary btn-lg btn-block" type="submit" disabled={busy || setup === null}>
                <LogIn size={15} /> {busy ? 'Signing in…' : 'Sign in'}
              </button>
              <p className="field-hint" style={{ textAlign: 'center' }}>
                No account, or forgot your password? Ask your CPARAS Super Admin.
              </p>
            </form>
          )}
        </div>
        <div className="auth-foot">{PLATFORM_NAME} {PLATFORM_VERSION} · Offline / on-premise · Centre for Development of Telematics</div>
      </div>
    </div>
  );
}
