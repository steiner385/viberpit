// Ported from plotroom@5166b52 frontend/src/__tests__/StatusStrip.test.tsx; edits: import repointing (../StatusStrip, ../types); eslint-disable @typescript-eslint/no-unused-vars (Bucket type import — kept verbatim from source)
/* eslint-disable @typescript-eslint/no-unused-vars */
import { describe, it, expect, vi } from 'vitest';
import { render, screen, fireEvent, within } from '@testing-library/react';
import { StatusStrip, bucketPr, isFailedPr, isActivePr, type Bucket } from '../StatusStrip';
import type { PrView } from '../types';

const pr = (stage: string, substate: string | null = null): PrView => ({
  repo: 'x', number: 1, title: 'pr', url: 'u',
  stage: { stage: stage as PrView['stage']['stage'], substate, percent: null,
    etaSeconds: null, etaRangeSeconds: null, overdue: false },
  queueAheadCount: null, checks: [], groupChecks: null, mergeEtaSim: null,
});

describe('bucketPr', () => {
  it('classifies ci stage as running', () => {
    expect(bucketPr(pr('ci'))).toBe('running');
  });

  it('classifies queue stage as queued', () => {
    expect(bucketPr(pr('queue'))).toBe('queued');
  });

  it('classifies qa-deploy as deploy', () => {
    expect(bucketPr(pr('qa-deploy'))).toBe('deploy');
  });

  it('classifies awaiting-prod as deploy', () => {
    expect(bucketPr(pr('awaiting-prod'))).toBe('deploy');
  });

  it('classifies merged as merged — retention-window stage for repos with no deploy environments', () => {
    expect(bucketPr(pr('merged'))).toBe('merged');
  });

  it('classifies parked/ci-failed as failed', () => {
    expect(bucketPr(pr('parked', 'ci-failed'))).toBe('failed');
  });

  it('classifies queue/group-failed as failed', () => {
    expect(bucketPr(pr('queue', 'group-failed'))).toBe('failed');
  });

  it('classifies parked (draft) as merged — retention-window noise bucket', () => {
    expect(bucketPr(pr('parked', 'draft'))).toBe('merged');
  });

  it('classifies parked (conflicting) as merged — retention-window noise bucket', () => {
    expect(bucketPr(pr('parked', 'conflicting'))).toBe('merged');
  });

  it('classifies ready as ready — the actionable bucket', () => {
    expect(bucketPr(pr('ready'))).toBe('ready');
  });

  it('classifies ready/stuck-enqueue as failed — armed but GitHub refused to enqueue', () => {
    expect(bucketPr(pr('ready', 'stuck-enqueue'))).toBe('failed');
  });
});

describe('isFailedPr / isActivePr (the shared classifiers — no per-surface copies)', () => {
  it('isFailedPr agrees with bucketPr === failed for every stage', () => {
    for (const [stage, sub] of [['ci', null], ['queue', null], ['queue', 'group-failed'],
      ['qa-deploy', null], ['awaiting-prod', null], ['parked', 'ci-failed'], ['parked', 'draft'],
      ['ready', null], ['ready', 'stuck-enqueue'], ['merged', null]] as const) {
      expect(isFailedPr(pr(stage, sub))).toBe(bucketPr(pr(stage, sub)) === 'failed');
    }
  });

  it('isActivePr is true for the moving pipeline stages, false for waiting/parked/done', () => {
    expect(isActivePr(pr('ci'))).toBe(true);
    expect(isActivePr(pr('queue'))).toBe(true);
    expect(isActivePr(pr('qa-deploy'))).toBe(true);
    expect(isActivePr(pr('awaiting-prod'))).toBe(false); // waiting, not working
    expect(isActivePr(pr('parked', 'ci-failed'))).toBe(false);
    expect(isActivePr(pr('ready'))).toBe(false);
    expect(isActivePr(pr('merged'))).toBe(false);
  });
});

describe('StatusStrip', () => {
  const prs: PrView[] = [
    pr('ci'),
    pr('ci'),
    pr('queue'),
    pr('queue'),
    pr('queue'),
    pr('qa-deploy'),
    pr('awaiting-prod'),
    pr('merged'),
    pr('parked', 'ci-failed'),
    pr('parked', 'draft'),
    pr('ready'),
    pr('ready', 'stuck-enqueue'),
  ];

  it('renders all six tiles with correct counts', () => {
    render(<StatusStrip prs={prs} activeFilter={null} onFilter={() => {}} />);
    // running=2, queued=3, deploy=2 (qa-deploy + awaiting-prod only),
    // failed=2 (parked/ci-failed + ready/stuck-enqueue),
    // ready=1 (open green), merged=2 (merged + parked/draft)
    const buttons = screen.getAllByRole('button');
    expect(buttons).toHaveLength(6);
    const countFor = (label: string) =>
      screen.getByText(label).closest('button')!.querySelector('b')!.textContent;
    expect(countFor('CI running')).toBe('2');
    expect(countFor('In queue')).toBe('3');
    expect(countFor('Deploying')).toBe('2');
    expect(countFor('Failed')).toBe('2');
    expect(countFor('Ready')).toBe('1');
    expect(countFor('Merged / other')).toBe('2');
  });

  it('empty buckets still render but are disabled', () => {
    const onlyRunning: PrView[] = [pr('ci')];
    render(<StatusStrip prs={onlyRunning} activeFilter={null} onFilter={() => {}} />);
    const buttons = screen.getAllByRole('button');
    expect(buttons).toHaveLength(6);
    // queued, deploy, failed, ready, merged all have count=0 and should be disabled
    const zeros = screen.getAllByText('0');
    expect(zeros.length).toBeGreaterThanOrEqual(5);
    zeros.forEach((el) => {
      expect(el.closest('button')).toHaveAttribute('disabled');
    });
    // running (count=1) is not disabled
    const runningBtn = screen.getByText('1').closest('button')!;
    expect(runningBtn).not.toHaveAttribute('disabled');
  });

  it('active tile has aria-pressed=true, others false', () => {
    render(<StatusStrip prs={prs} activeFilter="running" onFilter={() => {}} />);
    const buttons = screen.getAllByRole('button');
    const pressedBtns = buttons.filter((b) => b.getAttribute('aria-pressed') === 'true');
    expect(pressedBtns).toHaveLength(1);
    // The pressed button should correspond to the running bucket
    expect(pressedBtns[0]).toHaveTextContent('CI running');
  });

  it('clicking a tile calls onFilter with the bucket name', () => {
    const onFilter = vi.fn();
    render(<StatusStrip prs={prs} activeFilter={null} onFilter={onFilter} />);
    fireEvent.click(screen.getByText('CI running').closest('button')!);
    expect(onFilter).toHaveBeenCalledWith('running');
  });

  it('clicking the active tile calls onFilter with null (clear)', () => {
    const onFilter = vi.fn();
    render(<StatusStrip prs={prs} activeFilter="running" onFilter={onFilter} />);
    fireEvent.click(screen.getByText('CI running').closest('button')!);
    expect(onFilter).toHaveBeenCalledWith(null);
  });

  it('clicking a disabled tile does not fire onFilter', () => {
    const onFilter = vi.fn();
    const onlyRunning: PrView[] = [pr('ci')];
    render(<StatusStrip prs={onlyRunning} activeFilter={null} onFilter={onFilter} />);
    // find a disabled button (count=0) and click it
    const disabledBtn = screen.getAllByRole('button').find((b) => b.hasAttribute('disabled'))!;
    fireEvent.click(disabledBtn);
    expect(onFilter).not.toHaveBeenCalled();
  });

  it('active bucket with count 0 stays enabled so filter can be cleared (V1 stuck-filter fix)', () => {
    // Filter was set to 'running' while PRs existed; now zero PRs remain in that bucket.
    // The tile must NOT be disabled — the user must be able to click it to clear the filter.
    const onFilter = vi.fn();
    const noPrs: PrView[] = []; // all buckets empty
    render(<StatusStrip prs={noPrs} activeFilter="running" onFilter={onFilter} />);
    // The running tile has count=0 but is the active filter
    const runningBtn = screen.getByText('CI running').closest('button')!;
    expect(runningBtn).not.toHaveAttribute('disabled');
    // Clicking it calls onFilter(null) to clear
    fireEvent.click(runningBtn);
    expect(onFilter).toHaveBeenCalledWith(null);
  });

  // ---- in-place tooltips (legend feature) ----

  describe('tile title tooltips', () => {
    it('every tile carries a one-line bucket definition', () => {
      render(<StatusStrip prs={[pr('ci'), pr('queue'), pr('qa-deploy'),
        pr('parked', 'ci-failed'), pr('ready')]} activeFilter={null} onFilter={() => {}} />);
      const titles = screen.getAllByRole('button').map((b) => b.getAttribute('title'));
      expect(titles).toEqual([
        'CI running on the head commit',
        'In the merge queue — building or waiting for a slot',
        'Merged — deploying to QA or awaiting production deploy',
        'CI failed, merge-group build failed, or armed but stuck (not enqueued)',
        'Open, checks green — actionable (ready to arm auto-merge or merge directly)',
        'Recently merged (7-day retention), draft, or conflicting — no action needed',
      ]);
    });
  });
});

// ---- kiosk mode (issue #20): non-interactive strip ----

describe('StatusStrip interactive=false (kiosk)', () => {
  it('renders tiles as plain divs — no buttons, counts and labels intact', () => {
    render(<StatusStrip prs={[pr('ci'), pr('queue')]} activeFilter={null}
      onFilter={vi.fn()} interactive={false} />);
    const strip = screen.getByRole('group', { name: 'Status overview' });
    expect(within(strip).queryAllByRole('button')).toHaveLength(0);
    expect(within(strip).getByText('CI running')).toBeInTheDocument();
    expect(within(strip).getByText('In queue')).toBeInTheDocument();
    // both non-empty buckets show their count of 1
    expect(within(strip).getAllByText('1')).toHaveLength(2);
  });

  it('clicking a non-interactive tile never calls onFilter', () => {
    const onFilter = vi.fn();
    render(<StatusStrip prs={[pr('ci')]} activeFilter={null}
      onFilter={onFilter} interactive={false} />);
    fireEvent.click(screen.getByText('CI running'));
    expect(onFilter).not.toHaveBeenCalled();
  });
});

// ---- Failed-tile delta (#189): a rising failed count is the "something broke" signal ----

describe('StatusStrip — Failed-tile delta (#189)', () => {
  const failed = (n: number): PrView[] => Array.from({ length: n }, () => pr('queue', 'group-failed'));

  it('shows a rising/falling delta vs the mount baseline, only on the Failed tile', () => {
    const { rerender } = render(<StatusStrip prs={failed(2)} activeFilter={null} onFilter={() => {}} />);
    expect(screen.queryByText(/▲|▼/)).toBeNull();              // baseline === current → no delta at mount
    rerender(<StatusStrip prs={failed(5)} activeFilter={null} onFilter={() => {}} />);
    expect(screen.getByText('▲3')).toBeInTheDocument();        // 5 − 2 baseline
    rerender(<StatusStrip prs={failed(1)} activeFilter={null} onFilter={() => {}} />);
    expect(screen.getByText('▼1')).toBeInTheDocument();        // 1 − 2 baseline
  });

  it('shows no delta on other buckets even when their counts change', () => {
    const { rerender } = render(<StatusStrip prs={[pr('ci')]} activeFilter={null} onFilter={() => {}} />);
    rerender(<StatusStrip prs={[pr('ci'), pr('ci'), pr('ci')]} activeFilter={null} onFilter={() => {}} />);
    expect(screen.queryByText(/▲|▼/)).toBeNull();              // running count rose 1→3, but no urgency badge
  });
});
