/**
 * AppShell — the one layout around every signed-in page: sidebar navigation
 * (both products, reference data, help, administration), top bar (breadcrumbs,
 * backend health chip, help, theme, user menu) and footer.
 *
 * v0.5.0: navigation is filtered by the signed-in user's permissions
 * (useAuth().can), so a Guest sees only CPARAS ISLD and the help pages; the
 * user menu offers "Change password" and "Sign out". Hiding is cosmetic —
 * every route is also guarded in App.tsx and every API call on the server.
 */
import { useEffect, useRef, useState } from 'react';
import { Link, NavLink, Outlet, useLocation, useNavigate } from 'react-router-dom';
import { Menu, Moon, Sun, ChevronRight, ChevronDown, Home, PanelLeftClose, PanelLeftOpen, KeyRound, LogOut, UserCircle2 } from 'lucide-react';
import cdotLogo from '../gitas/assets/cdot-logo-circle.png';
import { useTheme } from './theme';
import HelpMenu from './HelpMenu';
import {
  PRODUCTS, PLATFORM_NAV, HELP_NAV, ADMIN_NAV, PLATFORM_NAME, PLATFORM_SUBTITLE, PLATFORM_VERSION,
  productForPath, pageLabelForPath, type NavItem, type Product,
} from './products';
import { useAuth } from './auth/AuthContext';
import ChangePasswordDialog from './auth/ChangePasswordDialog';

type Health = 'checking' | 'ready' | 'degraded' | 'unavailable' | 'offline';

interface HealthInfo { health: Health; detail: string; llmLabel: string | null }

const LLM_STATE_LABEL: Record<string, string> = {
  unreachable: 'LLM unreachable',
  not_ollama: 'LLM port not Ollama',
  no_models: 'No LLM models',
  model_missing: 'Default model missing',
  error: 'LLM error',
};

/** Backend + database + local LLM, from GET /api/health. The chip names
 * the actual problem (e.g. "LLM unreachable") instead of a vague
 * "Degraded", and its tooltip carries the backend's specific error/hint. */
function useBackendHealth(): HealthInfo {
  const [info, setInfo] = useState<HealthInfo>({ health: 'checking', detail: 'Checking the backend', llmLabel: null });
  useEffect(() => {
    let cancelled = false;
    const check = () => {
      fetch('/api/health')
        .then(r => (r.ok ? r.json() : Promise.reject(r.status)))
        .then(d => {
          if (cancelled) return;
          const s = String(d?.status || '').toUpperCase();
          const health: Health = s === 'READY' ? 'ready' : s === 'DEGRADED' ? 'degraded' : 'unavailable';
          const llm = d?.llm ?? {};
          const parts: string[] = [];
          if (d?.database?.status !== 'ok') parts.push(`Database: ${d?.database?.detail ?? 'error'}`);
          if (d?.corpus_storage?.status !== 'ok') parts.push(`Storage: ${d?.corpus_storage?.detail ?? 'error'}`);
          if (llm.status !== 'ok') parts.push(`Local LLM: ${[llm.error, llm.hint].filter(Boolean).join(' ') || llm.state}`);
          if (d?.vector_store?.status !== 'ok') parts.push(`Vector store: ${d?.vector_store?.detail ?? 'error'}`);
          if (d?.embeddings && d.embeddings.status !== 'ok') parts.push(`Embeddings: ${d.embeddings.detail ?? d.embeddings.status}`);
          const llmLabel = llm.status && llm.status !== 'ok' ? (LLM_STATE_LABEL[llm.state] ?? 'LLM not ready')
            : d?.embeddings && d.embeddings.status !== 'ok' ? 'Embedding model missing' : null;
          setInfo({ health, detail: parts.join('\n') || 'Backend, database and local LLM ready', llmLabel });
        })
        .catch(() => { if (!cancelled) setInfo({ health: 'offline', detail: 'Cannot reach the FastAPI backend (/api). Is it running?', llmLabel: null }); });
    };
    check();
    const t = setInterval(check, 20_000);
    return () => { cancelled = true; clearInterval(t); };
  }, []);
  return info;
}

const HEALTH_META: Record<Health, { label: string; color: string }> = {
  checking: { label: 'Connecting…', color: 'var(--c-muted-2)' },
  ready: { label: 'Backend ready', color: 'var(--c-ok)' },
  degraded: { label: 'Degraded', color: 'var(--c-warn)' },
  unavailable: { label: 'Unavailable', color: 'var(--c-err)' },
  offline: { label: 'Backend offline', color: 'var(--c-err)' },
};

function isItemActive(item: NavItem, pathname: string): boolean {
  if (item.end ? pathname === item.to : pathname === item.to || pathname.startsWith(item.to + '/')) return true;
  return !!item.match?.some(m => pathname.startsWith(m));
}

/** One sidebar navigation link (active state also covers its `match` prefixes). */
function SideLink({ item, pathname, onNavigate, collapsed }: { item: NavItem; pathname: string; onNavigate: () => void; collapsed?: boolean }) {
  const Icon = item.icon;
  const active = isItemActive(item, pathname);
  return (
    <NavLink to={item.to} end={item.end} onClick={onNavigate} className={() => `sb-link${active ? ' active' : ''}`} aria-current={active ? 'page' : undefined}
      title={collapsed ? item.label : undefined} aria-label={collapsed ? item.label : undefined}>
      <Icon size={15} />
      <span>{item.label}</span>
    </NavLink>
  );
}

/** A product's sidebar block: name/tagline header plus its navigation. */
function ProductBlock({ product, pathname, onNavigate, collapsed }: { product: Product; pathname: string; onNavigate: () => void; collapsed?: boolean }) {
  const Icon = product.icon;
  const active = productForPath(pathname)?.id === product.id;
  return (
    <div className={`sb-product ${product.className}${active ? ' is-active' : ''}`}>
      <Link to={product.home} onClick={onNavigate} className="sb-product-head" aria-label={`${product.name} — ${product.tagline}`}
        title={collapsed ? product.name : undefined}>
        <span className="sb-product-icon"><Icon size={16} /></span>
        <span style={{ minWidth: 0 }}>
          <div className="sb-product-name">{product.name}</div>
          <div className="sb-product-desc">{product.tagline}</div>
        </span>
      </Link>
      <nav className="sb-product-nav" aria-label={product.name}>
        {product.nav.map(item => <SideLink key={item.to} item={item} pathname={pathname} onNavigate={onNavigate} collapsed={collapsed} />)}
      </nav>
    </div>
  );
}

/** Top-bar account menu: who is signed in, their role, change password, sign out. */
function UserMenu() {
  const { user, logout } = useAuth();
  const navigate = useNavigate();
  const [open, setOpen] = useState(false);
  const [pwOpen, setPwOpen] = useState(false);
  const ref = useRef<HTMLDivElement>(null);
  useEffect(() => {
    const onClick = (e: MouseEvent) => { if (ref.current && !ref.current.contains(e.target as Node)) setOpen(false); };
    document.addEventListener('mousedown', onClick);
    return () => document.removeEventListener('mousedown', onClick);
  }, []);
  if (!user) return null;
  return (
    <div style={{ position: 'relative' }} ref={ref}>
      <button className="btn btn-sm btn-ghost user-chip" onClick={() => setOpen(o => !o)} aria-expanded={open} aria-haspopup="menu"
        title={`Signed in as ${user.username} (${user.role_label})`}>
        <UserCircle2 size={16} />
        <span className="hide-sm user-chip-name">{user.display_name}</span>
        <span className={`role-badge role-${user.role}`}>{user.role_label}</span>
        <ChevronDown size={13} />
      </button>
      {open && (
        <div className="popover user-popover" role="menu">
          <div className="user-popover-head">
            <div className="user-popover-name">{user.display_name}</div>
            <div className="muted small">{user.username} · {user.role_label}</div>
          </div>
          <button role="menuitem" className="user-popover-item" onClick={() => { setOpen(false); setPwOpen(true); }}>
            <KeyRound size={14} /> Change password
          </button>
          <button role="menuitem" className="user-popover-item" onClick={async () => { setOpen(false); await logout(); navigate('/login', { replace: true }); }}>
            <LogOut size={14} /> Sign out
          </button>
        </div>
      )}
      {pwOpen && <ChangePasswordDialog onClose={() => setPwOpen(false)} />}
    </div>
  );
}

/** The signed-in layout (sidebar, top bar, content outlet, footer). */
export default function AppShell() {
  const { theme, toggleTheme } = useTheme();
  const location = useLocation();
  const pathname = location.pathname;
  const [navOpen, setNavOpen] = useState(false);
  // Desktop: collapse the sidebar to an icon rail to give pages more room.
  // Remembered per browser; storage can be unavailable (private mode), so
  // every access is guarded and the default is expanded.
  const [collapsed, setCollapsed] = useState<boolean>(() => {
    try { return localStorage.getItem('cparas_sidebar_collapsed') === '1'; } catch { return false; }
  });
  const toggleCollapsed = () => setCollapsed(c => {
    const next = !c;
    try { localStorage.setItem('cparas_sidebar_collapsed', next ? '1' : '0'); } catch { /* ignore */ }
    return next;
  });
  const healthInfo = useBackendHealth();
  const { can } = useAuth();
  const visible = (items: NavItem[]) => items.filter(i => !i.permission || can(i.permission));
  const products = [PRODUCTS.bulk, PRODUCTS.isld].filter(p => can(p.permission));
  const referenceNav = visible(PLATFORM_NAV);
  const adminNav = visible(ADMIN_NAV);
  const product = productForPath(pathname);
  const pageLabel = pageLabelForPath(pathname);

  useEffect(() => { setNavOpen(false); }, [pathname]);

  useEffect(() => {
    const base = `${PLATFORM_NAME} · ${PLATFORM_SUBTITLE}`;
    document.title = product ? `${pageLabel} — ${product.name} · ${PLATFORM_NAME}` : pathname === '/' ? base : `${pageLabel} · ${PLATFORM_NAME}`;
  }, [pathname, product, pageLabel]);

  const close = () => setNavOpen(false);
  const hm = HEALTH_META[healthInfo.health];

  return (
    <div className="app-root" data-theme={theme}>
      <div className={`shell${navOpen ? ' nav-open' : ''}${collapsed ? ' nav-collapsed' : ''}`}>
        <aside className="sidebar" aria-label="Primary navigation">
          <Link to={can('bulk.use') ? '/' : PRODUCTS.isld.home} className="sb-brand" onClick={close}>
            <img src={cdotLogo} alt="C-DOT" className="sb-logo" />
            <span className="sb-brand-text">
              <span className="sb-brand-name">{PLATFORM_NAME}</span>
              <span className="sb-brand-sub">{PLATFORM_SUBTITLE}</span>
            </span>
          </Link>

          <div className="sb-section">
            <div className="sb-section-label">Products</div>
            {products.map(p => <ProductBlock key={p.id} product={p} pathname={pathname} onNavigate={close} collapsed={collapsed} />)}
          </div>

          {referenceNav.length > 0 && (
            <div className="sb-section">
              <div className="sb-section-label">Reference data</div>
              {referenceNav.map(item => <SideLink key={item.to} item={item} pathname={pathname} onNavigate={close} collapsed={collapsed} />)}
            </div>
          )}

          {adminNav.length > 0 && (
            <div className="sb-section">
              <div className="sb-section-label">Administration</div>
              {adminNav.map(item => <SideLink key={item.to} item={item} pathname={pathname} onNavigate={close} collapsed={collapsed} />)}
            </div>
          )}

          <div className="sb-section">
            <div className="sb-section-label">Help &amp; information</div>
            {HELP_NAV.map(item => <SideLink key={item.to} item={item} pathname={pathname} onNavigate={close} collapsed={collapsed} />)}
          </div>

          <div className="sb-footer">
            <span><b>{PLATFORM_NAME} {PLATFORM_VERSION}</b> · Offline / on-premise</span>
            <span>Centre for Development of Telematics</span>
          </div>
        </aside>
        <div className="sb-backdrop" onClick={close} aria-hidden />

        <div className="main-col">
          <header className="topbar">
            <button className="btn btn-sm btn-ghost btn-icon tb-burger" onClick={() => setNavOpen(o => !o)} aria-label="Open navigation">
              <Menu size={17} />
            </button>
            <button className="btn btn-sm btn-ghost btn-icon tb-collapse" onClick={toggleCollapsed}
              aria-label={collapsed ? 'Expand sidebar' : 'Collapse sidebar'} aria-pressed={collapsed}
              title={collapsed ? 'Expand sidebar' : 'Collapse sidebar'}>
              {collapsed ? <PanelLeftOpen size={17} /> : <PanelLeftClose size={17} />}
            </button>
            <nav className="crumbs" aria-label="Breadcrumb">
              <Link to={can('bulk.use') ? '/' : PRODUCTS.isld.home} aria-label="Platform home"><Home size={14} style={{ verticalAlign: -2 }} /></Link>
              {product && (
                <>
                  <ChevronRight size={13} className="crumb-sep" />
                  <Link to={product.home} className={`${product.className}`} style={{ color: 'var(--p-color)', fontWeight: 500 }}>{product.name}</Link>
                </>
              )}
              {pathname !== '/' && (
                <>
                  <ChevronRight size={13} className="crumb-sep" />
                  <span className="crumb-current">{pageLabel}</span>
                </>
              )}
            </nav>
            <div className="tb-right">
              <span className="status-chip" title={healthInfo.detail} role="status">
                <span className="status-dot" style={{ background: hm.color }} />
                <span className="status-label">{healthInfo.health === 'degraded' && healthInfo.llmLabel ? healthInfo.llmLabel : hm.label}</span>
              </span>
              <HelpMenu />
              <button className="btn btn-sm btn-ghost btn-icon" onClick={toggleTheme}
                title={theme === 'dark' ? 'Switch to light mode' : 'Switch to dark mode'} aria-label="Toggle dark mode">
                {theme === 'dark' ? <Sun size={16} /> : <Moon size={16} />}
              </button>
              <UserMenu />
            </div>
          </header>

          <main id="main" style={{ flex: 1, minWidth: 0 }}>
            <Outlet />
          </main>

          <footer className="app-footer">
            <span>© {new Date().getFullYear()} Centre for Development of Telematics (C-DOT) · {PLATFORM_NAME} {PLATFORM_SUBTITLE}</span>
            <span>Essentiality results are analytical signals, not legal opinions. IPR declarations are sourced from ETSI / TSDSI.</span>
          </footer>
        </div>
      </div>
    </div>
  );
}
