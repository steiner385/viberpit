/** The one DOM identity for a Pipeline PR row. A PR number is only unique within
 *  its repository, so the id is scoped by repo: PrRow stamps it and every
 *  QueueTrain jump link resolves through it. `:` cannot appear in a GitHub
 *  owner/repo name, so `owner/repo` + number maps to exactly one id. */
export function prAnchorId(repo: string, number: number): string {
  return `pr-${repo.replace('/', ':')}-${number}`;
}
