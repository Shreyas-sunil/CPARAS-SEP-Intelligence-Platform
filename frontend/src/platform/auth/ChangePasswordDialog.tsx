/**
 * Change-your-own-password dialog (v0.5.0).
 *
 * Opened from the user menu in the top bar, or shown `forced` (no close
 * button) right after signing in with a password an administrator set, so the
 * user replaces it before using CPARAS. Calls POST /api/auth/password, which
 * signs out the user's OTHER sessions and keeps this one.
 */
import { useState, type FormEvent } from 'react';
import { KeyRound, X } from 'lucide-react';
import { Banner } from '../ui';
import { useAuth } from './AuthContext';
import { authErrorMessage, changePassword } from './authApi';

/** Modal password-change form; `forced` hides the close button. */
export default function ChangePasswordDialog({ forced = false, onClose }: { forced?: boolean; onClose?: () => void }) {
  const { setSignedIn, logout } = useAuth();
  const [current, setCurrent] = useState('');
  const [next, setNext] = useState('');
  const [next2, setNext2] = useState('');
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [done, setDone] = useState(false);

  const submit = async (e: FormEvent) => {
    e.preventDefault();
    if (next !== next2) { setError('The two new passwords differ.'); return; }
    setBusy(true); setError(null);
    try {
      const u = await changePassword(current, next);
      setDone(true);
      setSignedIn(u);
      if (!forced) setTimeout(() => onClose?.(), 900);
    } catch (err) {
      setError(authErrorMessage(err, 'Could not change the password.'));
    } finally { setBusy(false); }
  };

  return (
    <div className="modal-backdrop" role="dialog" aria-modal="true" aria-labelledby="pw-title">
      <form className="modal" style={{ maxWidth: 440 }} onSubmit={submit}>
        <div className="modal-head">
          <h2 id="pw-title"><KeyRound size={15} style={{ verticalAlign: -2, marginRight: 6 }} />{forced ? 'Choose your password' : 'Change password'}</h2>
          {!forced && <button type="button" className="btn btn-sm btn-ghost btn-icon" onClick={onClose} aria-label="Close"><X size={15} /></button>}
        </div>
        <div className="modal-body stack-sm">
          {forced && <Banner tone="info">An administrator set your current password. Please choose your own before continuing.</Banner>}
          <label className="field">
            <span className="field-label">Current password</span>
            <input className="input" type="password" value={current} onChange={e => setCurrent(e.target.value)} autoComplete="current-password" required autoFocus />
          </label>
          <div className="field">
            <label className="field-label" htmlFor="pw-new">New password</label>
            <input id="pw-new" className="input" type="password" value={next} onChange={e => setNext(e.target.value)} autoComplete="new-password" required minLength={10}
              aria-describedby="pw-new-hint" />
            <span className="field-hint" id="pw-new-hint">At least 10 characters, mixing letters with digits or symbols. Your other sessions will be signed out.</span>
          </div>
          <label className="field">
            <span className="field-label">Repeat new password</span>
            <input className="input" type="password" value={next2} onChange={e => setNext2(e.target.value)} autoComplete="new-password" required />
          </label>
          {error && <Banner tone="err">{error}</Banner>}
          {done && <Banner tone="ok">Password changed.</Banner>}
          <div className="row" style={{ justifyContent: 'flex-end' }}>
            {forced && <button type="button" className="btn btn-ghost" onClick={() => void logout()}>Sign out</button>}
            <button className="btn btn-primary" type="submit" disabled={busy || done}>{busy ? 'Saving…' : 'Change password'}</button>
          </div>
        </div>
      </form>
    </div>
  );
}
