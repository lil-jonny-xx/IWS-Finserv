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

// The ornaments register belongs to a single entity and is shown only to that
// entity's own login, plus admins. Kept in sync with ORNAMENTS_ENTITY_ID in
// mis-portal/main.py, which is what actually enforces it (403) — hiding a tab
// here is presentation, not access control.
export const ORNAMENTS_ENTITY_ID = 12;

export const OWNER_ONLY_HREFS: Record<string, number> = {
  '/ornaments': ORNAMENTS_ENTITY_ID,
};

// Filter a nav list down to what the given user may see. Admin sees everything;
// anyone else has the admin-only entries removed, plus any owner-only section
// that isn't theirs.
export function navFor<T extends { href: string }>(
  items: T[], role?: string | null, entityId?: number | null,
): T[] {
  if (role === 'admin') return items;
  return items.filter((i) => {
    if (ADMIN_ONLY_HREFS.has(i.href)) return false;
    const owner = OWNER_ONLY_HREFS[i.href];
    return owner === undefined || owner === entityId;
  });
}
