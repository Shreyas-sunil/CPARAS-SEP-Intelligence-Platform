/**
 * Platform catalogue — the two products, their navigation, and which
 * permission each part of the UI needs (v0.5.0 RBAC).
 *
 * AppShell builds the sidebar from PRODUCTS / PLATFORM_NAV / HELP_NAV /
 * ADMIN_NAV, skipping entries whose `permission` the signed-in user lacks;
 * `permissionForPath` is the matching route -> permission map used by the
 * route guards in App.tsx and by the post-login redirect. Permission names
 * are the backend's (backend/auth/roles.py); the backend enforces the same
 * rules on every API call (backend/auth/policy.py).
 */
import type { LucideIcon } from 'lucide-react';
import {
  LayoutDashboard, UploadCloud, Table2, FileSearch, ScanText, Layers, Info, BookOpen,
  HelpCircle, LifeBuoy, BarChart3, MessagesSquare, Boxes, FileStack, Database, Users,
} from 'lucide-react';
import type { Permission } from './auth/authApi';

/** Product identifiers. */
export type ProductId = 'bulk' | 'isld';

/** One sidebar link. */
export interface NavItem {
  to: string;
  label: string;
  icon: LucideIcon;
  /** Extra path prefixes that should also highlight this item. */
  match?: string[];
  end?: boolean;
  /** Hidden unless the signed-in user has this permission (none = any signed-in user). */
  permission?: Permission;
}

/** One product: identity, home, navigation, capabilities and required permission. */
export interface Product {
  id: ProductId;
  name: string;
  short: string;
  tagline: string;
  description: string;
  icon: LucideIcon;
  className: string;
  home: string;
  nav: NavItem[];
  capabilities: string[];
  /** Permission needed to see/use the product at all. */
  permission: Permission;
}

/** Platform name shown in the shell and reports. */
export const PLATFORM_NAME = 'CPARAS';
/** Platform subtitle shown next to the name. */
export const PLATFORM_SUBTITLE = 'C-DOT SEP Intelligence Platform';
/** Displayed version; keep equal to backend/core/version.py and package.json. */
export const PLATFORM_VERSION = 'v0.5.0';

/** The two products. */
export const PRODUCTS: Record<ProductId, Product> = {
  bulk: {
    id: 'bulk',
    name: 'CPARAS Bulk',
    short: 'Bulk',
    tagline: 'Bulk & portfolio declaration analysis',
    description:
      'Upload an ETSI bulk IPR-declaration export and explore it at portfolio scale — essentiality scoring for every row, company and release breakdowns, the patent registry, and an enriched Excel export that keeps every source row.',
    icon: Boxes,
    className: 'p-bulk',
    home: '/bulk',
    permission: 'bulk.use',
    nav: [
      { to: '/bulk', label: 'Portfolio Dashboard', icon: LayoutDashboard, end: true, match: ['/bulk/company'] },
      { to: '/bulk/registry', label: 'Patent Registry', icon: Table2, match: ['/bulk/patent'] },
      { to: '/bulk/runs', label: 'Uploads & Runs', icon: UploadCloud },
    ],
    capabilities: [
      'Bulk ETSI export upload (.xlsx / .xls / .csv), every row preserved',
      'Essentiality scoring with claim-match, similarity & coverage signals',
      'Company, release, country and domain dashboards',
      'Enriched Excel export + portfolio PDF report',
    ],
  },
  isld: {
    id: 'isld',
    name: 'CPARAS ISLD',
    short: 'ISLD',
    tagline: 'Single declaration deep analysis',
    description:
      'Deep analysis of individual IPR declarations (ISLD) — PDF, CSV, Excel or HTML — with a local LLM: per-patent standards mapping, SEP strength, research-style evidence, and clause-level text matching against the standards corpus.',
    icon: FileSearch,
    className: 'p-isld',
    home: '/isld',
    permission: 'isld.use',
    nav: [
      { to: '/isld', label: 'Declaration Analysis', icon: FileStack, end: true },
      { to: '/isld/text-matching', label: 'Text Matching', icon: ScanText },
    ],
    capabilities: [
      'PDF (with OCR), CSV, Excel and HTML declarations',
      'Quick extraction or deep 3GPP enrichment (local LLM)',
      'Standard / version / clause recommendation & verification',
      'Clause-level text matching, PDF / HTML / CSV / JSON reports',
    ],
  },
};

/** Shared reference-data pages. */
export const PLATFORM_NAV: NavItem[] = [
  { to: '/retrieval-index', label: '3GPP Retrieval Index', icon: Database, permission: 'reference.view' },
  { to: '/corpus', label: 'Standards Corpus', icon: Layers, permission: 'reference.view' },
];

/** Super Admin tools. */
export const ADMIN_NAV: NavItem[] = [
  { to: '/admin/users', label: 'User Management', icon: Users, permission: 'users.manage' },
];

/**
 * Permission required to open a frontend route (null = any signed-in user).
 * Keep in step with the <Route> table in App.tsx.
 */
export function permissionForPath(pathname: string): Permission | null {
  const path = pathname.split('?')[0];
  if (path === '/bulk' || path.startsWith('/bulk/')) return 'bulk.use';
  if (path === '/isld' || path.startsWith('/isld/')) return 'isld.use';
  if (path.startsWith('/corpus') || path.startsWith('/retrieval-index')) return 'reference.view';
  if (path.startsWith('/admin')) return 'users.manage';
  if (path === '/') return 'bulk.use'; // the platform home summarises Bulk + reference data
  return null;
}

/** Whether a user with permission test `can` may open `pathname`. */
export function isPathAllowed(pathname: string, can: (p: Permission) => boolean): boolean {
  const p = permissionForPath(pathname);
  return p === null || can(p);
}

/** Help & information pages (every signed-in role). */
export const HELP_NAV: NavItem[] = [
  { to: '/about', label: 'About', icon: Info },
  { to: '/resources', label: 'Resources', icon: BookOpen },
  { to: '/faqs', label: 'FAQs', icon: HelpCircle },
  { to: '/facilitation-center', label: 'Facilitation Center', icon: LifeBuoy },
  { to: '/reports', label: 'Reports', icon: BarChart3 },
  { to: '/chatbot', label: 'Assistant', icon: MessagesSquare },
];

/** Which product a path belongs to (null for platform pages). */
export function productForPath(pathname: string): Product | null {
  if (pathname === '/bulk' || pathname.startsWith('/bulk/')) return PRODUCTS.bulk;
  if (pathname === '/isld' || pathname.startsWith('/isld/')) return PRODUCTS.isld;
  return null;
}

/** Breadcrumb / document-title label for a path. */
export function pageLabelForPath(pathname: string): string {
  for (const p of Object.values(PRODUCTS)) {
    for (const item of p.nav) {
      if (item.end ? pathname === item.to : pathname === item.to || pathname.startsWith(item.to + '/')) return item.label;
      if (item.match?.some(m => pathname.startsWith(m))) {
        if (pathname.startsWith('/bulk/patent')) return 'Patent Detail';
        if (pathname.startsWith('/bulk/company')) return 'Company Profile';
        return item.label;
      }
    }
  }
  for (const item of [...PLATFORM_NAV, ...HELP_NAV, ...ADMIN_NAV]) {
    if (pathname === item.to || pathname.startsWith(item.to + '/')) return item.label;
  }
  return 'Home';
}
