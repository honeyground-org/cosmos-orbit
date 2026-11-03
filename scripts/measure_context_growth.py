#!/usr/bin/env python3
"""Measure your own sessions: how large contexts grow, and what a mid-task handoff at T tokens would save.

Reads local Claude Code transcripts (~/.claude/projects/*/*.jsonl) and Codex rollouts
(~/.codex/sessions/**/rollout-*.jsonl). Nothing leaves the machine; no model is called.

Cost model per turn (Anthropic price ratios): input x1, cache write x1.25, cache read x0.1. Codex rollouts
report total tokens per turn only, so they are counted as cache reads (the conservative case).
Replay: once a session has grown past max(T, start + 60k), a fresh session restarts from that session's
own start plus a 3k brief and a re-read penalty R (files the new session opens again).

  python3 scripts/measure_context_growth.py [--days 14] [--reread 15000,40000,80000]
"""
import argparse
import glob
import json
import os
import time


def claude_sessions(cut):
    for path in glob.glob(os.path.expanduser('~/.claude/projects/*/*.jsonl')):
        if os.path.getmtime(path) < cut:
            continue
        turns, last = [], None
        with open(path, encoding='utf-8', errors='replace') as stream:
            for line in stream:
                if '"usage"' not in line or '"assistant"' not in line:
                    continue
                try:
                    event = json.loads(line)
                except ValueError:
                    continue
                if event.get('type') != 'assistant' or event.get('isSidechain'):
                    continue
                message = event.get('message') or {}
                usage = message.get('usage') or {}
                fresh, write, read = (usage.get(k) or 0 for k in ('input_tokens', 'cache_creation_input_tokens', 'cache_read_input_tokens'))
                if fresh + write + read <= 0 or message.get('id') == last:
                    continue
                last = message.get('id')
                turns.append((fresh + write + read, fresh + 1.25 * write + 0.1 * read))
        if len(turns) >= 5:
            yield turns


def codex_sessions(cut):
    for path in glob.glob(os.path.expanduser('~/.codex/sessions/*/*/*/rollout-*.jsonl')):
        if os.path.getmtime(path) < cut:
            continue
        turns = []
        with open(path, encoding='utf-8', errors='replace') as stream:
            for line in stream:
                if '"token_count"' not in line:
                    continue
                try:
                    event = json.loads(line)
                except ValueError:
                    continue
                info = (event.get('payload') or {}).get('info') or {}
                tokens = (info.get('last_token_usage') or {}).get('total_tokens')
                if isinstance(tokens, int) and tokens > 0 and (not turns or turns[-1][0] != tokens):
                    turns.append((tokens, 0.1 * tokens))
        if len(turns) >= 5:
            yield turns


def replay(sessions, threshold, reread, growth=60_000, brief=3_000):
    saved, handoffs = 0.0, 0
    for turns in sessions:
        start = turns[0][0]
        limit = max(threshold, start + growth)
        simulated = previous = start
        for tokens, _ in turns:
            delta, previous = tokens - previous, tokens
            simulated = min(simulated, tokens) if delta < 0 else simulated + delta
            if simulated >= limit:
                handoffs += 1
                simulated = start + reread + brief
                saved -= 1.25 * (start + reread + brief)
            saved += 0.1 * max(0, tokens - simulated)
    return handoffs, saved


def report(name, sessions):
    if not sessions:
        print('%s: no sessions with five or more turns in the window' % name)
        return
    peaks = sorted(max(t for t, _ in s) for s in sessions)
    starts = sorted(s[0][0] for s in sessions)
    every = [t for s in sessions for t, _ in s]
    actual = sum(c for s in sessions for _, c in s)
    mid = lambda xs: xs[len(xs) // 2]
    print('\n%s · %d sessions · %d turns' % (name, len(sessions), len(every)))
    print('  session start median %dk · peak median %dk · peak p90 %dk' % (mid(starts) // 1000, mid(peaks) // 1000, peaks[int(.9 * (len(peaks) - 1))] // 1000))
    print('  turns above 200k: %.1f%%' % (100 * sum(t > 200_000 for t in every) / len(every)))
    return actual


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument('--days', type=int, default=14)
    parser.add_argument('--reread', default='15000,40000,80000', help='re-read penalties per handoff, comma separated')
    args = parser.parse_args()
    cut = time.time() - args.days * 86400
    rereads = [int(x) for x in args.reread.split(',') if x.strip()]
    for name, loader in (('Claude Code', claude_sessions), ('Codex', codex_sessions)):
        sessions = list(loader(cut))
        actual = report(name, sessions)
        if not actual:
            continue
        print('  cost-equivalent tokens saved by a handoff at T (handoffs in brackets):')
        for reread in rereads:
            cells = []
            for threshold in (120_000, 150_000, 200_000, 300_000):
                handoffs, saved = replay(sessions, threshold, reread)
                cells.append('%dk %5.1f%% (%d)' % (threshold // 1000, 100 * saved / actual, handoffs))
            print('    re-read %2dk: ' % (reread // 1000) + ' · '.join(cells))


if __name__ == '__main__':
    main()
