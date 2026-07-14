/**
 * Helper for seeding the allowlist from a contact export. The D1 import path is
 * wired via `wrangler d1 execute` in a later milestone; the number-normalization
 * logic lives here (pure + tested) so the import is reliable.
 */

/** Best-effort normalize a raw phone string to US/Canada E.164, or "" if unusable. */
export function toE164Us(raw: string): string {
  const digits = raw.replace(/\D/g, "");
  if (digits.length === 10) return `+1${digits}`;
  if (digits.length === 11 && digits.startsWith("1")) return `+${digits}`;
  if (raw.trim().startsWith("+") && digits.length > 0) return `+${digits}`;
  return "";
}

/** Turn rows of {name, number} into INSERT-ready allowlist tuples, skipping junk. */
export function toAllowlistRows(
  contacts: ReadonlyArray<{ name: string; number: string }>,
): Array<{ numberE164: string; name: string }> {
  const out: Array<{ numberE164: string; name: string }> = [];
  for (const c of contacts) {
    const numberE164 = toE164Us(c.number);
    if (numberE164 !== "") out.push({ numberE164, name: c.name });
  }
  return out;
}
