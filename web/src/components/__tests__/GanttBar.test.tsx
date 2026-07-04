import { render } from '@testing-library/react'
import { GanttBar } from '../GanttBar'

describe('GanttBar', () => {
  it('renders a solid fill at the given width with progressbar role', () => {
    const { container, getByRole } = render(<GanttBar fillPct={42} colorClass="bg-accent" ariaNow={42} />)
    const solid = container.querySelector('[data-band="solid"]') as HTMLElement
    expect(solid.style.width).toBe('42%')
    expect(getByRole('progressbar')).toHaveAttribute('aria-valuenow', '42')
  })
  it('renders band and tick only when provided', () => {
    const { container } = render(
      <GanttBar fillPct={20} colorClass="bg-accent" bandLeftPct={30} bandWidthPct={40} tickPct={55} />,
    )
    expect(container.querySelector('[data-band="range"]')).toBeInTheDocument()
    expect(container.querySelector('[data-band="tick"]')).toBeInTheDocument()
  })
  it('omits band when width is zero and tick when null', () => {
    const { container } = render(
      <GanttBar fillPct={20} colorClass="bg-accent" bandLeftPct={30} bandWidthPct={0} tickPct={null} />,
    )
    expect(container.querySelector('[data-band="range"]')).toBeNull()
    expect(container.querySelector('[data-band="tick"]')).toBeNull()
  })
  it('applies a texture overlay when textureImage is set', () => {
    const { container } = render(
      <GanttBar fillPct={50} colorClass="bg-red-500" textureImage="repeating-linear-gradient(-45deg, red 0 2px, transparent 2px 6px)" />,
    )
    const overlay = container.querySelector('[data-band="texture"]') as HTMLElement
    expect(overlay.style.backgroundImage).toContain('repeating-linear-gradient')
  })
})
