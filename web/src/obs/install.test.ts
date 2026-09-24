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

  it('logs the benign ResizeObserver loop notice at info, not error (#1993)', () => {
    const logger = new ClientLogger()
    const error = vi.spyOn(logger, 'error')
    const info = vi.spyOn(logger, 'info')
    const uninstall = installGlobalErrorHandlers(logger)
    for (const message of [
      'ResizeObserver loop completed with undelivered notifications.',
      'ResizeObserver loop limit exceeded',
    ]) window.dispatchEvent(new ErrorEvent('error', { message }))
    expect(error).not.toHaveBeenCalled()
    expect(info).toHaveBeenCalledTimes(2)
    uninstall()
  })

  it('still reports a real error whose message mentions ResizeObserver', () => {
    const logger = new ClientLogger()
    const error = vi.spyOn(logger, 'error')
    const uninstall = installGlobalErrorHandlers(logger)
    window.dispatchEvent(new ErrorEvent('error', {
      message: 'ResizeObserver loop callback threw', error: new TypeError('boom'),
    }))
    expect(error).toHaveBeenCalled()
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
