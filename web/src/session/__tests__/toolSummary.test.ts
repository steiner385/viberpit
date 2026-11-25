import { describe, expect, it } from 'vitest'
import { changedPaths, toolLabel, toolSummary } from '@/session/toolSummary'

/**
 * One summary for two surfaces. The transcript card and the approval card had a
 * copy each and drifted the moment Codex arrived — the transcript learned to
 * read `changes: [{path}]`, the approval card did not, so the card an operator
 * has to ACT on was the one that said only "Edit".
 */
describe('toolSummary', () => {
  it('summarises a Claude edit from file_path', () => {
    expect(toolSummary('edit', { file_path: 'server/app.py' })).toBe('server/app.py')
  })

  it('summarises a Codex edit from its changes list', () => {
    expect(toolSummary('edit', { changes: [{ path: 'a.py', kind: 'update' }] })).toBe('a.py')
  })

  it('counts the rest when a Codex edit touches several files', () => {
    expect(
      toolSummary('edit', {
        changes: [{ path: 'a.py' }, { path: 'b.py' }, { path: 'c.py' }],
      }),
    ).toBe('a.py +2 more')
  })

  it('falls back to the grant root of a Codex file-change approval', () => {
    // The approval arrives BEFORE the write, so there is no path list yet —
    // only the scope being requested. Without this the operator saw bare "Edit".
    expect(toolSummary('edit', { grant_root: '/srv/repo', reason: 'write files' })).toBe(
      '/srv/repo',
    )
  })

  it('truncates a long bash command rather than quoting it whole', () => {
    const summary = toolSummary('bash', { command: 'x'.repeat(200) })
    expect(summary).toHaveLength(60)
  })

  it('returns nothing for a shape it does not recognise', () => {
    // Truthful: the caller then shows the bare tool name. Guessing from an
    // unknown payload is how a card ends up asserting something false.
    expect(toolSummary('edit', { something_else: 1 })).toBe('')
    expect(toolSummary('mcp', { file_path: 'a.py' })).toBe('')
    expect(toolSummary('bash', {})).toBe('')
  })

  it('ignores malformed entries in a changes list', () => {
    expect(changedPaths([{ path: 'a.py' }, null, { nope: 1 }, { path: '' }])).toEqual(['a.py'])
    expect(changedPaths('not-an-array')).toEqual([])
  })
})

/** The CLI's user-facing tools arrive as `unknown` kind: summarised by NAME, so a
 *  row or a permission card never reads as a bare "SendUserFile". */
describe('toolSummary — CLI user-facing tools', () => {
  it('summarises SendUserFile by its caption, else its file', () => {
    expect(toolSummary('unknown', { files: ['/home/x/repo/.rendertest/shot.png'], caption: 'Settings page', status: 'normal' }, 'SendUserFile'))
      .toBe('Settings page')
    expect(toolSummary('unknown', { files: ['/home/x/repo/.rendertest/shot.png'], status: 'normal' }, 'SendUserFile'))
      .toBe('/home/x/repo/.rendertest/shot.png')
    expect(toolSummary('unknown', { files: ['a.png', 'b.txt'] }, 'SendUserFile')).toBe('2 files')
  })

  it('summarises the message, findings count and notification', () => {
    expect(toolSummary('unknown', { message: 'Done.\nDetails below' }, 'SendUserMessage')).toBe('Done.')
    expect(toolSummary('unknown', { findings: [] }, 'ReportFindings')).toBe('no findings')
    expect(toolSummary('unknown', { findings: [{ file: 'a.ts', line: 1, summary: 's', failure_scenario: 'f' }] }, 'ReportFindings')).toBe('1 finding')
    expect(toolSummary('unknown', { message: 'Build finished' }, 'PushNotification')).toBe('Build finished')
  })

  it('stays name-blind without a name, and labels by name', () => {
    expect(toolSummary('unknown', { message: 'x' })).toBe('')
    expect(toolLabel('PushNotification', 'unknown', { message: 'Build finished' })).toBe('PushNotification: Build finished')
  })
})
