/**
 * User Management (/admin/users) — Super Admin only (v0.5.0).
 *
 * Create accounts, change roles, activate/deactivate, reset passwords, delete,
 * and read the recent authentication audit trail. Everything goes through
 * /api/auth/users and /api/auth/events (backend/api/routes/shared/users.py),
 * which enforce the rules again server-side (users.manage permission; the last
 * active Super Admin can never be demoted, deactivated or deleted).
 *
 * Generated passwords are returned by the backend exactly once and shown in a
 * dismissible banner; they are not stored anywhere in the browser.
 */
import { useCallback, useEffect, useState, type FormEvent } from 'react';
import { Copy, KeyRound, RefreshCw, Shield, Trash2, UserPlus, Users } from 'lucide-react';
import { Banner, Card, LoadingState, PageHeader, Pill, formatDateTime } from '../ui';
import { useAuth } from '../auth/AuthContext';
import {
  authErrorMessage, createUser, deleteUser, listAuthEvents, listRoles, listUsers, resetUserPassword, updateUser,
  type AuthEvent, type AuthUser, type RoleId, type RoleInfo,
} from '../auth/authApi';

const EVENT_LABELS: Record<string, string> = {
  login: 'Signed in', logout: 'Signed out', login_failed: 'Failed sign-in', login_locked: 'Sign-in locked out',
  login_inactive: 'Sign-in by deactivated account', user_created: 'User created', user_updated: 'User changed',
  user_deleted: 'User deleted', password_reset: 'Password reset by admin', password_changed: 'Password changed',
  first_admin_created: 'First Super Admin created', setup_failed: 'First-run setup refused', test_user_seeded: 'Test account seeded',
};

/** One-time display of a password the backend generated. */
function SecretBanner({ secret, onDismiss }: { secret: { username: string; password: string }; onDismiss: () => void }) {
  const [copied, setCopied] = useState(false);
  return (
    <Banner tone="warn" title={`Password for “${secret.username}” — shown only once`}
      actions={<button className="btn btn-sm" onClick={onDismiss}>Done</button>}>
      <div className="row" style={{ gap: 8, marginTop: 4 }}>
        <code className="secret-code">{secret.password}</code>
        <button className="btn btn-sm" onClick={() => { void navigator.clipboard?.writeText(secret.password).then(() => setCopied(true)); }}>
          <Copy size={13} /> {copied ? 'Copied' : 'Copy'}
        </button>
      </div>
      <div className="small" style={{ marginTop: 4 }}>Give it to the user over a private channel. They will be asked to choose their own password at first sign-in.</div>
    </Banner>
  );
}

/** User Management page. */
export default function UsersAdminPage() {
  const { user: me } = useAuth();
  const [users, setUsers] = useState<AuthUser[] | null>(null);
  const [roles, setRoles] = useState<RoleInfo[]>([]);
  const [events, setEvents] = useState<AuthEvent[]>([]);
  const [error, setError] = useState<string | null>(null);
  const [info, setInfo] = useState<string | null>(null);
  const [secret, setSecret] = useState<{ username: string; password: string } | null>(null);
  const [busyId, setBusyId] = useState<number | null>(null);

  // create form
  const [nUser, setNUser] = useState('');
  const [nName, setNName] = useState('');
  const [nRole, setNRole] = useState<RoleId>('guest');
  const [nPw, setNPw] = useState('');
  const [nMust, setNMust] = useState(true);
  const [creating, setCreating] = useState(false);

  const load = useCallback(async () => {
    try {
      const [u, r, e] = await Promise.all([listUsers(), listRoles(), listAuthEvents(60)]);
      setUsers(u); setRoles(r); setEvents(e); setError(null);
    } catch (err) { setError(authErrorMessage(err, 'Could not load users.')); }
  }, []);
  useEffect(() => { void load(); }, [load]);

  const act = async (id: number, fn: () => Promise<unknown>, okMsg?: string) => {
    setBusyId(id); setError(null); setInfo(null);
    try { await fn(); if (okMsg) setInfo(okMsg); await load(); }
    catch (err) { setError(authErrorMessage(err, 'The change was refused.')); }
    finally { setBusyId(null); }
  };

  const onCreate = async (e: FormEvent) => {
    e.preventDefault();
    setCreating(true); setError(null); setInfo(null);
    try {
      const res = await createUser({ username: nUser.trim(), role: nRole, display_name: nName.trim() || undefined,
        password: nPw || undefined, must_change_password: nMust });
      if (res.generated_password) setSecret({ username: res.user.username, password: res.generated_password });
      else setInfo(`User “${res.user.username}” created.`);
      setNUser(''); setNName(''); setNPw(''); setNRole('guest'); setNMust(true);
      await load();
    } catch (err) { setError(authErrorMessage(err, 'Could not create the user.')); }
    finally { setCreating(false); }
  };

  const roleLabel = (id: string) => roles.find(r => r.id === id)?.label ?? id;

  return (
    <div className="page">
      <PageHeader eyebrow="Administration" title="User Management"
        description="Accounts and roles for this CPARAS installation. Role or status changes and password resets sign the user out immediately."
        actions={<button className="btn btn-sm" onClick={() => void load()}><RefreshCw size={13} /> Refresh</button>} />

      <div className="stack">
        {error && <Banner tone="err">{error}</Banner>}
        {info && <Banner tone="ok">{info}</Banner>}
        {secret && <SecretBanner secret={secret} onDismiss={() => setSecret(null)} />}

        <div className="grid-3">
          {roles.map(r => (
            <div key={r.id} className="role-card">
              <div className="row" style={{ gap: 6 }}><span className={`role-badge role-${r.id}`}>{r.label}</span></div>
              <div className="small" style={{ marginTop: 6, color: 'var(--c-text-2)' }}>{r.description}</div>
            </div>
          ))}
        </div>

        <Card title="Accounts" icon={<Users size={15} />} flush
          subtitle={users ? `${users.length} account(s) · ${users.filter(u => u.is_active).length} active` : undefined}>
          {users === null ? <div style={{ padding: 14 }}><LoadingState /></div> : (
            <div className="table-scroll">
              <table className="dtable">
                <thead>
                  <tr><th>User</th><th>Role</th><th>Status</th><th>Last sign-in</th><th>Created</th><th style={{ textAlign: 'right' }}>Actions</th></tr>
                </thead>
                <tbody>
                  {users.map(u => {
                    const self = u.id === me?.id;
                    const busy = busyId === u.id;
                    return (
                      <tr key={u.id}>
                        <td>
                          <div style={{ fontWeight: 600 }}>{u.display_name}{self && <span className="muted small"> (you)</span>}</div>
                          <div className="muted small mono">{u.username}</div>
                        </td>
                        <td>
                          <select className="select" style={{ height: 28, fontSize: 12 }} value={u.role} disabled={busy}
                            aria-label={`Role of ${u.username}`}
                            onChange={e => void act(u.id, () => updateUser(u.id, { role: e.target.value as RoleId }),
                              `${u.username} is now ${roleLabel(e.target.value)}.`)}>
                            {roles.map(r => <option key={r.id} value={r.id}>{r.label}</option>)}
                          </select>
                        </td>
                        <td>
                          {u.is_active ? <Pill tone="ok" dot>Active</Pill> : <Pill tone="muted" dot>Deactivated</Pill>}
                          {u.must_change_password && <div className="small muted" style={{ marginTop: 3 }}>must change password</div>}
                        </td>
                        <td className="small">{formatDateTime(u.last_login_at)}</td>
                        <td className="small">{formatDateTime(u.created_at)}<div className="muted">{u.created_by ?? ''}</div></td>
                        <td style={{ textAlign: 'right', whiteSpace: 'nowrap' }}>
                          <button className="btn btn-sm" disabled={busy} title="Generate a new password"
                            onClick={() => void act(u.id, async () => {
                              const res = await resetUserPassword(u.id, { must_change_password: true });
                              if (res.generated_password) setSecret({ username: u.username, password: res.generated_password });
                            })}>
                            <KeyRound size={13} /> Reset password
                          </button>{' '}
                          <button className="btn btn-sm" disabled={busy || self}
                            onClick={() => void act(u.id, () => updateUser(u.id, { is_active: !u.is_active }),
                              `${u.username} ${u.is_active ? 'deactivated' : 'activated'}.`)}>
                            {u.is_active ? 'Deactivate' : 'Activate'}
                          </button>{' '}
                          <button className="btn btn-sm btn-danger btn-icon" disabled={busy || self} aria-label={`Delete ${u.username}`} title="Delete"
                            onClick={() => { if (window.confirm(`Delete the account “${u.username}”? This cannot be undone.`)) void act(u.id, () => deleteUser(u.id), `${u.username} deleted.`); }}>
                            <Trash2 size={13} />
                          </button>
                        </td>
                      </tr>
                    );
                  })}
                </tbody>
              </table>
            </div>
          )}
        </Card>

        <div className="grid-2">
          <Card title="Add a user" icon={<UserPlus size={15} />}>
            <form className="stack-sm" onSubmit={onCreate}>
              <div className="grid-2" style={{ gap: 10 }}>
                <label className="field">
                  <span className="field-label">Username</span>
                  <input className="input" value={nUser} onChange={e => setNUser(e.target.value)} required autoComplete="off"
                    pattern="[A-Za-z0-9][A-Za-z0-9._\-]{2,63}" title="3-64 characters: letters, digits, '.', '_' or '-'" />
                </label>
                <label className="field">
                  <span className="field-label">Display name <span className="muted">(optional)</span></span>
                  <input className="input" value={nName} onChange={e => setNName(e.target.value)} autoComplete="off" />
                </label>
              </div>
              <label className="field">
                <span className="field-label">Role</span>
                <select className="select" value={nRole} onChange={e => setNRole(e.target.value as RoleId)}>
                  {roles.map(r => <option key={r.id} value={r.id}>{r.label} — {r.description}</option>)}
                </select>
              </label>
              <label className="field">
                <span className="field-label">Initial password <span className="muted">(leave empty to generate a strong one)</span></span>
                <input className="input" type="password" value={nPw} onChange={e => setNPw(e.target.value)} autoComplete="new-password" />
              </label>
              <label className="row small" style={{ gap: 6 }}>
                <input type="checkbox" checked={nMust} onChange={e => setNMust(e.target.checked)} />
                Ask the user to choose their own password at first sign-in
              </label>
              <div><button className="btn btn-primary" type="submit" disabled={creating}><UserPlus size={14} /> {creating ? 'Creating…' : 'Create user'}</button></div>
            </form>
          </Card>

          <Card title="Recent sign-in & admin activity" icon={<Shield size={15} />} flush>
            <div className="table-scroll" style={{ maxHeight: 330 }}>
              <table className="dtable">
                <thead><tr><th>When</th><th>Event</th><th>Account</th><th>By</th></tr></thead>
                <tbody>
                  {events.length === 0 ? <tr><td colSpan={4} className="muted small">No events yet.</td></tr> : events.map(ev => (
                    <tr key={ev.id} title={[ev.detail, ev.client_ip].filter(Boolean).join(' · ')}>
                      <td className="small" style={{ whiteSpace: 'nowrap' }}>{formatDateTime(ev.at)}</td>
                      <td className="small">{EVENT_LABELS[ev.event] ?? ev.event}{ev.detail ? <div className="muted">{ev.detail}</div> : null}</td>
                      <td className="small mono">{ev.username ?? '—'}</td>
                      <td className="small mono">{ev.actor ?? '—'}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          </Card>
        </div>
      </div>
    </div>
  );
}
