// web/src/obs/install.test.ts
import { describe, it, expect, vi } from 'vitest'
import { installGlobalErrorHandlers } from './install'
import { ClientLogger } from './logger'

describe('installGlobalErrorHandlers', () => {
  it('forwards window error events', () => {
    const logger = new ClientLogger()
    const spy = vi.spyOn(logger, 'error')
    const uninstall = installGlobalErrorHandlers(logger)
    window.dispatchEvent(new ErrorEvent('error', { message: 'kaboom', error: new Error('kaboom') }))
    expect(spy).toHaveBeenCalledWith(expect.stringContaining('kaboom'), expect.any(Object))
    uninstall()
  })

  it('forwards unhandledrejection', () => {
    const logger = new ClientLogger()
    const spy = vi.spyOn(logger, 'error')
    const uninstall = installGlobalErrorHandlers(logger)
    const evt = new Event('unhandledrejection') as Event & { reason?: unknown }
    evt.reason = new Error('rejected')
    window.dispatchEvent(evt)
    expect(spy).toHaveBeenCalled()
    uninstall()
  })
})
