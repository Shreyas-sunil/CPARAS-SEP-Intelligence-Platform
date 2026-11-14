/**
 * CPARAS Bulk company profile (/bulk/company/:name): one declarant's records, essentiality split
 * and release breakdown. Data: GET /api/sep/entity/company/{name}.
 */

import { useEffect, useMemo, useState } from 'react';
import { useParams, Link } from 'react-router-dom';
import { ArrowLeft } from 'lucide-react';
import * as sep from '../api';
import { EssentialityDonut, ReleaseBars } from '../components/charts';
import PatentRegistryTable from '../components/PatentRegistryTable';
import { CountryFlag } from '../components/common';
import { PRODUCTS } from '../../platform/products';
import { Banner, Card, EmptyState, Kpi, LoadingState, PageHeader, Pill } from '../../platform/ui';

const product = PRODUCTS.bulk;
const pct = (n: number, total: number) => (total ? `${((n / total) * 100).toFixed(1)}%` : '—');

/** Company profile page. */
export default function CompanyProfilePage() {
  const { name } = useParams();
  const [profile, setProfile] = useState<any>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    if (!name) return;
    setProfile(null);
    setError(null);
    sep.getCompanyProfile(name).then(setProfile).catch(() => setError('No records were found for this company.'));
  }, [name]);

  const releaseData = useMemo(() => {
    const buckets: Record<string, { Essential: number; Potential: number; 'Non-Essential': number }> = {};
    for (const p of (profile?.patents ?? [])) {
      if (!p.release) continue;
      const b = buckets[p.release] ?? (buckets[p.release] = { Essential: 0, Potential: 0, 'Non-Essential': 0 });
      if (p.essentiality === 'Essential') b.Essential++;
      else if (p.essentiality === 'Non-Essential') b['Non-Essential']++;
      else b.Potential++;
    }
    return Object.entries(buckets)
      .sort(([a], [b]) => a.localeCompare(b))
      .map(([release, counts]) => ({ release, count: counts.Essential + counts.Potential + counts['Non-Essential'], ...counts }));
  }, [profile]);

  if (error) {
    return (
      <div className="page page--narrow">
        <PageHeader product={product} title="Company profile" />
        <Banner tone="err" title="Company not found" actions={<Link className="btn btn-sm" to="/bulk">Back to dashboard</Link>}>{error}</Banner>
      </div>
    );
  }
  if (!profile) return <div className="page"><LoadingState label="Loading company profile…" /></div>;

  const total = profile.total_seps as number;
  const domains = Object.entries(profile.by_domain as Record<string, number>);

  return (
    <div className="page">
      <PageHeader
        product={product}
        eyebrow="CPARAS Bulk · Company profile"
        title={<span style={{ display: 'inline-flex', alignItems: 'center', gap: 12 }}><span className="g-profile-avatar">{profile.company.slice(0, 2).toUpperCase()}</span>{profile.company}</span>}
        description={<><CountryFlag code={profile.country} />{profile.country || 'Country not provided'} · {total.toLocaleString()} declared record{total === 1 ? '' : 's'} across all uploads</>}
        actions={<Link className="btn" to="/bulk"><ArrowLeft size={14} /> Dashboard</Link>}
      />

      <div className="stack">
        <div className="kpi-grid">
          <Kpi label="Declared records" value={total.toLocaleString()} color="var(--c-bulk)" />
          <Kpi label="Essential" value={profile.essential.toLocaleString()} note={pct(profile.essential, total)} color="var(--c-ok)" />
          <Kpi label="Potential" value={profile.potential.toLocaleString()} note={pct(profile.potential, total)} color="var(--c-warn)" />
          <Kpi label="Non-essential" value={profile.non_essential.toLocaleString()} note={pct(profile.non_essential, total)} color="var(--c-err)" />
        </div>

        <div className="grid-2">
          <Card title="Records by release" subtitle="Stacked by essentiality">
            {releaseData.length ? <ReleaseBars data={releaseData} /> : <EmptyState title="No release data" />}
          </Card>
          <Card title="Essentiality breakdown">
            <EssentialityDonut essential={profile.essential} potential={profile.potential} nonEssential={profile.non_essential} />
          </Card>
        </div>

        <Card title="Top technical domains">
          {domains.length ? (
            <div className="row">
              {domains.slice(0, 10).map(([domain, count]) => <Pill key={domain} tone="outline">{domain} · <span className="num">{count}</span></Pill>)}
            </div>
          ) : <span className="muted small">No domain data.</span>}
        </Card>

        <Card title="Declared records" subtitle={`${profile.patents.length.toLocaleString()} records`} flush>
          <PatentRegistryTable patents={profile.patents} />
        </Card>
      </div>
    </div>
  );
}
