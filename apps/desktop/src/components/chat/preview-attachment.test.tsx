import { act } from 'react'
import { createRoot, type Root } from 'react-dom/client'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import { $currentCwd } from '@/store/session'

import { PreviewAttachment } from './preview-attachment'

const mocks = vi.hoisted(() => ({
  normalizeOrLocalPreviewTarget: vi.fn(),
  notifyError: vi.fn()
}))

vi.mock('@/lib/local-preview', () => ({
  normalizeOrLocalPreviewTarget: mocks.normalizeOrLocalPreviewTarget
}))

vi.mock('@/store/notifications', () => ({
  notifyError: mocks.notifyError
}))

vi.mock('@/i18n', () => ({
  useI18n: () => ({
    t: {
      preview: {
        hide: 'Hide',
        openInBrowser: 'Open in browser',
        opening: 'Opening…',
        openPreview: 'Open preview',
        unavailable: 'Preview unavailable'
      }
    }
  })
}))

vi.mock('@/lib/icons', () => ({
  MonitorPlay: () => null
}))

describe('PreviewAttachment', () => {
  let container: HTMLDivElement
  let root: Root

  beforeEach(() => {
    container = document.createElement('div')
    document.body.appendChild(container)
    root = createRoot(container)

    mocks.normalizeOrLocalPreviewTarget.mockResolvedValue({
      kind: 'file',
      label: 'demo.html',
      path: 'C:/Users/clark/demo.html',
      previewKind: 'html',
      source: 'C:/Users/clark/demo.html',
      url: 'file:///C:/Users/clark/demo.html'
    })
    mocks.notifyError.mockClear()
    $currentCwd.set('')
  })

  afterEach(() => {
    act(() => root.unmount())
    container.remove()
    delete (window as unknown as { hermesDesktop?: unknown }).hermesDesktop
    vi.unstubAllGlobals()
    vi.clearAllMocks()
  })

  it('opens a normalized local preview target in the system browser', async () => {
    const openPreviewInBrowser = vi.fn(async () => {})

    ;(window as unknown as { hermesDesktop: { openPreviewInBrowser: typeof openPreviewInBrowser } }).hermesDesktop = {
      openPreviewInBrowser
    }

    act(() => {
      root.render(<PreviewAttachment source="explicit-link" target="C:/Users/clark/demo.html" />)
    })

    const openButton = Array.from(container.querySelectorAll('button')).find(
      (button) => button.textContent === 'Open in browser'
    )
    expect(openButton).toBeTruthy()

    await act(async () => {
      openButton?.dispatchEvent(new MouseEvent('click', { bubbles: true }))
    })

    await waitForExpect(() => {
      expect(openPreviewInBrowser).toHaveBeenCalledWith('file:///C:/Users/clark/demo.html')
    })
    expect(mocks.normalizeOrLocalPreviewTarget).toHaveBeenCalledWith('C:/Users/clark/demo.html', undefined)
    expect(mocks.notifyError).not.toHaveBeenCalled()
  })
})

async function waitForExpect(assertion: () => void) {
  let lastError: unknown

  for (let attempt = 0; attempt < 20; attempt += 1) {
    try {
      assertion()
      return
    } catch (error) {
      lastError = error
      await new Promise((resolve) => setTimeout(resolve, 10))
    }
  }

  throw lastError
}
