import type { Automation } from '@/api/automations'

export const AUTOMATIONS: Automation[] = [
  {
    id: 'pr-doctor', name: 'PR doctor', description: 'Fixes failing PRs.', enabled: true, mode: 'auto',
    modes: ['off', 'propose', 'auto'], status: { text: '2 PRs failing', level: 'warn', counts: null },
    enginePolicy: 'claude_only', operatorTriggered: false,
    configSchema: [
      { key: 'auto_fix', label: 'Auto-fix', type: 'bool', default: true, help: 'Open fix sessions without asking.', choices: null },
      { key: 'enabled_repos', label: 'Repositories', type: 'repo-multiselect', default: [], help: '', choices: null },
    ],
    config: { auto_fix: true, enabled_repos: ['o/r'] }, schedule: null, lastActiveAt: '2026-09-11T10:00:00Z',
    activeSessions: 1, workClass: 'maintenance',
  },
  {
    id: 'backlog-runner', name: 'Backlog runner', description: 'Dispatches eligible issues.', enabled: true, mode: 'on',
    modes: ['off', 'on'], status: { text: 'Idle', level: 'idle', counts: null },
    enginePolicy: 'claude_only', operatorTriggered: false, configSchema: [], config: {}, schedule: null,
    lastActiveAt: null, activeSessions: 0, workClass: 'development',
  },
  {
    id: 'pm-classify', name: 'PM classify', description: 'Classifies issues on request.', enabled: false, mode: null,
    modes: [], status: { text: 'Idle', level: 'idle', counts: null }, enginePolicy: 'claude_only', operatorTriggered: true,
    configSchema: [], config: {}, schedule: null, lastActiveAt: null, activeSessions: 0, workClass: 'maintenance',
  },
]
