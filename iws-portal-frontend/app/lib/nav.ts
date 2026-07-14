// Navigation visibility by role.
//
// Individual-entity logins (role === 'member', e.g. DHR / HHR / SDR) see their own
// portfolio across every section — Overview, asset tabs, Banks, Realised Gains and
// Reports are all scoped to their own entity by the backend. The only admin-only
// section is Manual Data (the firm-wide data-entry tool), hidden from members here
// and blocked by the API (403).
export const ADMIN_ONLY_HREFS = new Set<string>([
  '/manual-data',
  '/trades',        // manual trade register — firm-wide data entry, admin only
]);

// Filter a nav list down to what the given role may see. Admin sees everything;
// anyone else has the admin-only entries removed.
export function navFor<T extends { href: string }>(items: T[], role?: string | null): T[] {
  if (role === 'admin') return items;
  return items.filter((i) => !ADMIN_ONLY_HREFS.has(i.href));
}
