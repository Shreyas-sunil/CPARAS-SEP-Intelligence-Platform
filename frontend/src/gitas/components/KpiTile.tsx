/** Compatibility wrapper: older Bulk pages use KpiTile; it renders the shared platform Kpi tile. */

import { Kpi } from '../../platform/ui';

/** Platform Kpi tile with the legacy prop names. */
export default function KpiTile({ label, value, note, color }: { label: string; value: string | number; note?: string; color?: string }) {
  return <Kpi label={label} value={value} note={note} color={color} />;
}
