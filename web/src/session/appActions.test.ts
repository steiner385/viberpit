import { describe, it, expect } from 'vitest'
import { APP_ACTION_TITLES, appActionTitle } from './appActions'
import toolSpecKeys from './__fixtures__/appActionToolKeys.json'

describe('APP_ACTION_TITLES', () => {
  it('has the same key set as the server TOOL_SPECS fixture', () => {
    expect(Object.keys(APP_ACTION_TITLES).sort()).toEqual([...toolSpecKeys].sort())
  })
})

describe('appActionTitle', () => {
  it('renders the ccswitch MCP tool name as its title', () => {
    expect(appActionTitle('mcp__ccswitch__list_sessions')).toBe('Listed sessions')
  })

  it('returns undefined for a non-ccswitch MCP tool', () => {
    expect(appActionTitle('mcp__github__create_issue')).toBeUndefined()
  })

  it('returns undefined for a plain tool name', () => {
    expect(appActionTitle('Bash')).toBeUndefined()
  })
})
