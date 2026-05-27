// Job-lifecycle recency windows. Kept free of env/Zod so pure modules (the
// Notion cache, queries, pruning) can import them without booting config.

/**
 * How long an application "locks" a company. While any of a company's jobs has
 * an Application Date inside this window we treat it as already-applied: new
 * listings are marked "Company Applied" instead of "To Review", and pruning
 * leaves the company's jobs alone. Once the window lapses the lock expires.
 *
 * Used by the Notion cache, the reconcile queries, and pruning — keep them in
 * sync by reading this constant rather than re-deriving the window.
 */
export const REAPPLY_WINDOW_MONTHS = 6;

/**
 * How recently a "Company Applied" job must have been scraped for reconcile to
 * re-check whether its company still has a live application worth keeping.
 */
export const COMPANY_APPLIED_UNSTALE_DAYS = 30;
