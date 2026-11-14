/**
 * Root component: theme + authentication providers and the complete route table. See the comment
 * block below for which role reaches which route.
 */

import { BrowserRouter, Routes, Route, Navigate, useLocation, useParams } from 'react-router-dom';
import { ThemeProvider } from './platform/theme';
import AppShell from './platform/AppShell';
import HomePage from './platform/HomePage';
import RetrievalIndexPage from './platform/RetrievalIndexPage';
import DashboardPage from './gitas/pages/DashboardPage';
import RegistryPage from './gitas/pages/RegistryPage';
import RunsPage from './gitas/pages/RunsPage';
import PatentDetailPage from './gitas/pages/PatentDetailPage';
import CompanyProfilePage from './gitas/pages/CompanyProfilePage';
import CorpusPage from './gitas/pages/CorpusPage';
import { AboutPage, ResourcesPage, FaqsPage, FacilitationCenterPage, ReportsPage, ChatbotPage } from './gitas/pages/StaticPages';
import IsldApp from './cparas/App';
import { AuthProvider } from './platform/auth/AuthContext';
import LoginPage from './platform/auth/LoginPage';
import { HomeRoute, RequireAuth, RequirePermission } from './platform/auth/guards';
import UsersAdminPage from './platform/admin/UsersAdminPage';
import type { Permission } from './platform/auth/authApi';
import type { ReactNode } from 'react';

/*
 * CPARAS — C-DOT SEP Intelligence Platform
 *
 *   /bulk/*   CPARAS Bulk  — bulk / portfolio declaration analysis
 *             (code under src/gitas/, backend /api/sep)
 *   /isld/*   CPARAS ISLD  — single-declaration deep analysis + text matching
 *             (code under src/cparas/, backend /api/analysis, /api/text-match)
 *
 * Both products live inside one shell (platform/AppShell) with one theme and
 * one navigation. Legacy URLs from earlier builds redirect to their new home.
 *
 * v0.5.0 access control:
 *   /login            public sign-in / first-time setup page (outside the shell)
 *   everything else   <RequireAuth>: signed-in users only
 *   each product /
 *   reference page    <RequirePermission>: role must grant the permission
 *                     (Guest -> ISLD only; Data Entry -> ISLD + Bulk + reference
 *                     data; Super Admin -> all, plus /admin/users)
 *   /                 platform home for Bulk users; Guests are sent to /isld
 * The backend enforces the same rules on every API call (backend/auth/policy.py).
 */

/** Route element guarded by one permission. */
const guard = (permission: Permission, el: ReactNode) => <RequirePermission permission={permission}>{el}</RequirePermission>;

/** Keeps the query string (e.g. ?ref_id=… deep links into text matching). */
function RedirectKeepingQuery({ to }: { to: string }) {
  const { search } = useLocation();
  return <Navigate to={`${to}${search}`} replace />;
}

/**
 * Old /analyze URL: deep links carrying ?ref_id= go to ISLD Text Matching, everything else to ISLD
 * Declaration Analysis.
 */
function LegacyAnalyzeRedirect() {
  const { search } = useLocation();
  const params = new URLSearchParams(search);
  return <Navigate to={`${params.get('ref_id') ? '/isld/text-matching' : '/isld'}${search}`} replace />;
}

/** Old /patent/:id URL -> /bulk/patent/:id. */
function LegacyPatentRedirect() {
  const { id } = useParams();
  return <Navigate to={`/bulk/patent/${id}`} replace />;
}

/** Old /entity/company/:name URL -> /bulk/company/:name. */
function LegacyCompanyRedirect() {
  const { name } = useParams();
  return <Navigate to={`/bulk/company/${encodeURIComponent(name ?? '')}`} replace />;
}

/**
 * The application: ThemeProvider > AuthProvider > router. /login is public; every other route
 * renders inside the signed-in AppShell and is guarded by the permission it needs.
 */
export default function App() {
  return (
    <ThemeProvider>
      <AuthProvider>
      <BrowserRouter>
        <Routes>
          <Route path="login" element={<LoginPage />} />
          <Route element={<RequireAuth><AppShell /></RequireAuth>}>
            <Route index element={<HomeRoute><HomePage /></HomeRoute>} />

            {/* CPARAS Bulk — Data Entry and Super Admin only */}
            <Route path="bulk" element={guard('bulk.use', <DashboardPage />)} />
            <Route path="bulk/registry" element={guard('bulk.use', <RegistryPage />)} />
            <Route path="bulk/runs" element={guard('bulk.use', <RunsPage />)} />
            <Route path="bulk/patent/:id" element={guard('bulk.use', <PatentDetailPage />)} />
            <Route path="bulk/company/:name" element={guard('bulk.use', <CompanyProfilePage />)} />

            {/* CPARAS ISLD — one mounted component for both pages so analysis
                results survive switching between Declaration Analysis and
                Text Matching. All roles. */}
            <Route path="isld/*" element={guard('isld.use', <IsldApp />)} />

            {/* Shared reference data (Data Entry, Super Admin) + help (everyone) */}
            <Route path="corpus" element={guard('reference.view', <CorpusPage />)} />
            <Route path="retrieval-index" element={guard('reference.view', <RetrievalIndexPage />)} />

            {/* Administration — Super Admin */}
            <Route path="admin/users" element={guard('users.manage', <UsersAdminPage />)} />

            <Route path="about" element={<AboutPage />} />
            <Route path="resources" element={<ResourcesPage />} />
            <Route path="faqs" element={<FaqsPage />} />
            <Route path="facilitation-center" element={<FacilitationCenterPage />} />
            <Route path="reports" element={<ReportsPage />} />
            <Route path="chatbot" element={<ChatbotPage />} />

            {/* Legacy URLs */}
            <Route path="dashboard" element={<RedirectKeepingQuery to="/bulk" />} />
            <Route path="analyze" element={<LegacyAnalyzeRedirect />} />
            <Route path="patent/:id" element={<LegacyPatentRedirect />} />
            <Route path="entity/company/:name" element={<LegacyCompanyRedirect />} />
            <Route path="csaras" element={<Navigate to="/" replace />} />

            <Route path="*" element={<Navigate to="/" replace />} />
          </Route>
        </Routes>
      </BrowserRouter>
      </AuthProvider>
    </ThemeProvider>
  );
}
