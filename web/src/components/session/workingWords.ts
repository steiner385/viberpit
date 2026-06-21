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

/** Returns a random entry from WORKING_WORDS. */
export function pickWorkingWord(): string {
  return WORKING_WORDS[Math.floor(Math.random() * WORKING_WORDS.length)]
}
