/**
 * Whimsical gerunds shown while Claude is working — Claude Code style.
 * All entries end in "ing".
 */
export const WORKING_WORDS: string[] = [
  'Clauding',
  'Finagling',
  'Noodling',
  'Percolating',
  'Pondering',
  'Cogitating',
  'Reticulating',
  'Schlepping',
  'Vibing',
  'Simmering',
  'Marinating',
  'Conjuring',
  'Hatching',
  'Brewing',
  'Mulling',
  'Musing',
  'Ruminating',
  'Synthesizing',
  'Transmuting',
  'Moseying',
  'Honking',
  'Smooshing',
  'Crunching',
  'Manifesting',
  'Cooking',
  'Spinning',
  'Churning',
  'Hustling',
  'Ideating',
  'Puttering',
  'Coalescing',
  'Deliberating',
  'Wrangling',
  'Tinkering',
  'Whirring',
]

/**
 * Returns a random entry from WORKING_WORDS.
 *
 * @param exclude  If given, the returned word is guaranteed to differ from it, so
 *   a new "waiting for Claude" period never repeats the immediately-previous
 *   period's phrase.
 */
export function pickWorkingWord(exclude?: string): string {
  const pool =
    exclude !== undefined && WORKING_WORDS.length > 1
      ? WORKING_WORDS.filter((w) => w !== exclude)
      : WORKING_WORDS
  return pool[Math.floor(Math.random() * pool.length)]
}
