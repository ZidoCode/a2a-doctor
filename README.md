# a2a-doctor

**One command, every A2A audit in the family. Dumb math, honest numbers, zero dependencies.**

[![tests](https://github.com/ZidoCode/a2a-doctor/actions/workflows/ci.yml/badge.svg)](https://github.com/ZidoCode/a2a-doctor/actions/workflows/ci.yml) [![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)]()

The umbrella entry point for the ZidoCode A2A audit suite:

| Repo | What it audits |
|---|---|
| [a2a-tax](https://github.com/ZidoCode/a2a-tax) | token cost of the agent card + polling model |
| [a2a-stream-tax](https://github.com/ZidoCode/a2a-stream-tax) | token cost of a recorded SSE stream |
| [a2a-reconnect-tax](https://github.com/ZidoCode/a2a-reconnect-tax) | replayed-event tax across SSE reconnects |
| [a2a-artifact-audit](https://github.com/ZidoCode/a2a-artifact-audit) | chunk gaps, duplicates, writes after `last_chunk` |
| [a2a-task-state-audit](https://github.com/ZidoCode/a2a-task-state-audit) | task resurrection, illegal transitions, unknown states |
| [a2a-context-audit](https://github.com/ZidoCode/a2a-context-audit) | empty IDs, context mutation, orphan contexts ([A2A#1309](https://github.com/a2aproject/A2A/issues/1309)) |
| [a2a-card-lock](https://github.com/ZidoCode/a2a-card-lock) | agent-card pin + drift gate |

Each member works standalone. a2a-doctor runs them all in one pass and gives one verdict.

## Install

```bash
pip install a2a-doctor   # or: copy the .py files (they are all stdlib-only)
```

## Usage

```bash
$ a2a-doctor check --card card.json --stream stream.jsonl --lock a2a-card.lock
a2a-doctor check — 7 section(s)

  ✓ card       418 tok estimated
  ✓ polling    ~418 tok/task estimated
  ✓ stream     17 event(s) recorded
  ✗ artifacts  1 critical — event 14: write to 'a1' after last_chunk (artifact already closed)
  ✗ lifecycle  5 critical — line 7: task 'zombie-task' resumed after terminal state 'failed' -> 'working'
  ✓ reconnect  no replayed events
  ✓ lock       card unchanged — hash matches

  verdict: FAIL (artifacts, lifecycle)
$ echo $?
2
```

`check` is the CI gate: **exit 0** clean, **1** warnings only, **2** critical findings.

Individual sections:

```bash
a2a-doctor tax card.json          # card size + $ estimate
a2a-doctor stream stream.jsonl    # token breakdown of a recorded stream
a2a-doctor reconnect stream.jsonl # replayed events across reconnects
a2a-doctor artifacts stream.jsonl # artifact integrity report
a2a-doctor lifecycle stream.jsonl # task state machine audit
a2a-doctor context stream.jsonl   # context/task correlation audit
a2a-doctor lock card.json a2a-card.lock  # drift check
```

## Stream format

One JSON event per line — record your client's `message/stream` session, replay it anywhere, anytime. Deterministic: same input, same verdict, byte for byte.

## Why

Auditing an A2A endpoint once is easy. Continuously — in CI, on every deploy — is the point. a2a-doctor exists so there is one command to wire into the pipeline, not six. MIT license.
