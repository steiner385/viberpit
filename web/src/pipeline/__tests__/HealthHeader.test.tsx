// Ported from plotroom@5166b52 frontend/src/__tests__/HealthHeader.test.tsx; edits: kiosk (aria-disabled) cases, #1555
import { describe, it, expect, vi } from 'vitest';
import { render, screen, fireEvent } from '@testing-library/react';
import { HealthHeader } from '../HealthHeader';
import { InteractionProvider } from '@/shell/InteractionContext';
import type { DashboardState } from '../types';

// A repo whose only PR failed CI → the PR CI lane is red.
const redState = (): DashboardState => ({
  generatedAt: '', staleSince: null,
  repos: [{ repo: 'acme/widgets', hasDeploy: false, queue: null,
    prs: [{ number: 1, title: 't', url: 'u',
      stage: { stage: 'ci', substate: 'ci-failed', percent: null, etaSeconds: null,
        etaRangeSeconds: null, overdue: false }, queueAheadCount: null, checks: [] }] }],
}) as unknown as DashboardState;

// No PRs in CI, main green, no deploy/scheduled/cost data → nothing needs attention.
const greenState = (): DashboardState => ({
  generatedAt: '', staleSince: null,
  repos: [{ repo: 'acme/widgets', hasDeploy: false, prs: [], queue: null,
    laneHealth: { main: 'green', lastGreenSha: 'abc1234', lastGreenAt: '2026-06-10T10:00:00Z',
      mainSeries: [{ ok: true }] } }],
}) as unknown as DashboardState;

describe('HealthHeader', () => {
  it('renders the rollup pill and one chip per lifecycle lane', () => {
    render(<HealthHeader state={redState()} onJumpToLane={vi.fn()} />);
    expect(screen.getByTestId('health-rollup')).toBeInTheDocument();
    for (const id of ['pr-ci', 'merge-queue', 'main', 'deploy', 'scheduled', 'failures', 'cost']) {
      expect(screen.getByTestId(`health-lane-${id}`)).toBeInTheDocument();
    }
  });

  it('rolls up to "need attention" when a wired lane is red', () => {
    render(<HealthHeader state={redState()} onJumpToLane={vi.fn()} />);
    expect(screen.getByTestId('health-rollup')).toHaveTextContent(/needs? attention/i);
    // the state reads as a tone via LANE_TONE — red → the danger dot
    expect(screen.getByTestId('health-rollup').querySelector('.bg-danger')).not.toBeNull();
  });

  it('rolls up to all-green when nothing needs attention', () => {
    render(<HealthHeader state={greenState()} onJumpToLane={vi.fn()} />);
    const rollup = screen.getByTestId('health-rollup');
    expect(rollup).toHaveTextContent(/all systems green/i);
    expect(rollup.querySelector('.bg-success')).not.toBeNull();
  });

  it('clicking the rollup jumps to the first lane needing attention, with its status', () => {
    const onJump = vi.fn();
    render(<HealthHeader state={redState()} onJumpToLane={onJump} />);
    fireEvent.click(screen.getByTestId('health-rollup'));
    expect(onJump).toHaveBeenCalledWith('pr-ci', 'red');   // the red lane + its status
  });

  it('clicking a lane chip jumps to that lane, passing its status (for PR CI routing)', () => {
    const onJump = vi.fn();
    render(<HealthHeader state={greenState()} onJumpToLane={onJump} />);
    fireEvent.click(screen.getByTestId('health-lane-main'));
    expect(onJump).toHaveBeenCalledWith('main', expect.anything());
  });

  it('dims lanes with no data source (not-wired)', () => {
    render(<HealthHeader state={greenState()} onJumpToLane={vi.fn()} />);
    // no repo ships deploy/scheduled data and no rates → those lanes are not-wired
    expect(screen.getByTestId('health-lane-deploy')).toHaveClass('opacity-60');
    expect(screen.getByTestId('health-lane-cost')).toHaveClass('opacity-60');
    // a wired lane is not dimmed
    expect(screen.getByTestId('health-lane-main')).not.toHaveClass('opacity-60');
  });

  it('lane chip exposes its title + summary to assistive tech via aria-label', () => {
    render(<HealthHeader state={redState()} onJumpToLane={vi.fn()} />);
    expect(screen.getByTestId('health-lane-pr-ci'))
      .toHaveAttribute('aria-label', expect.stringContaining('PR CI'));
  });

  it('surfaces the lane summary inline on a non-green chip (#189)', () => {
    render(<HealthHeader state={redState()} onJumpToLane={vi.fn()} />);
    // the muted-ink span is the inline summary (the chip's only muted text)
    const summary = screen.getByTestId('health-lane-pr-ci').querySelector('.text-ink-muted');
    expect(summary).not.toBeNull();
    expect(summary!.textContent!.length).toBeGreaterThan(0);
  });

  it('hides the inline summary on a green lane (#189)', () => {
    render(<HealthHeader state={greenState()} onJumpToLane={vi.fn()} />);
    expect(screen.getByTestId('health-lane-main').querySelector('.text-ink-muted')).toBeNull();
  });

  it('below md the chip collapses to dot + glyph; the title stays in aria-label (#1555)', () => {
    render(<HealthHeader state={redState()} onJumpToLane={vi.fn()} />);
    const chip = screen.getByTestId('health-lane-pr-ci');
    const title = Array.from(chip.querySelectorAll('span')).find((s) => s.textContent === 'PR CI');
    expect(title).toHaveClass('hidden', 'md:inline');
    expect(chip).toHaveAttribute('aria-label', expect.stringContaining('PR CI'));
  });

  // Shell spec §5 / #1555: kiosk buttons render aria-disabled and do not act.
  it('kiosk (non-interactive): chips and rollup are aria-disabled and do not jump', () => {
    const onJump = vi.fn();
    render(
      <InteractionProvider interactive={false}>
        <HealthHeader state={redState()} onJumpToLane={onJump} />
      </InteractionProvider>,
    );
    const rollup = screen.getByTestId('health-rollup');
    const chip = screen.getByTestId('health-lane-pr-ci');
    expect(rollup).toHaveAttribute('aria-disabled', 'true');
    expect(chip).toHaveAttribute('aria-disabled', 'true');
    fireEvent.click(rollup);
    fireEvent.click(chip);
    expect(onJump).not.toHaveBeenCalled();
  });

  it('interactive: chips carry no aria-disabled', () => {
    render(<HealthHeader state={redState()} onJumpToLane={vi.fn()} />);
    expect(screen.getByTestId('health-rollup')).not.toHaveAttribute('aria-disabled');
    expect(screen.getByTestId('health-lane-pr-ci')).not.toHaveAttribute('aria-disabled');
  });
});
