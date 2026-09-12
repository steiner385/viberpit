// Title → slug, shared by every roadmap form that derives one (R8c-6): the
// "+ Initiative" dialog and the capability dialogs the map-changes PRs are made
// from. One rule, in one place — two copies would be two normalisations, and a
// slug is what a URL, a branch name and a feature-doc filename are all built
// from.

/** What the user is typing, kept slug-shaped: a trailing separator survives so
 *  the space between two words can be typed at all. */
export const sanitize = (s: string) => s.toLowerCase().replace(/[^a-z0-9]+/g, '-').replace(/^-/, '')
/** The final slug — `sanitize` plus the trailing separator a finished word never has. */
export const slugify = (s: string) => sanitize(s.trim()).replace(/-$/, '')
