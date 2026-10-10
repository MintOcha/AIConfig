---
name: research-cleanup
description: Use when archiving completed research phases, purging stale model checkpoints, temporary web data, logs, and intermediary artifacts, while preserving executable scripts and active checkpoints in RESEARCH_ARCHIVE.md.
---

# Research Workspace Cleanup & Archiving

## Purpose

Research projects accumulate dozens of gigabytes of intermediate model checkpoints, temporary web scrapes, raw cache dumps, and exploratory branch files. Without systematic consolidation, repositories become unmaintainable, slow to sync, and full of dead code.

This skill defines the canonical protocol for distilling completed research initiatives into a permanent, structured `RESEARCH_ARCHIVE.md` while purging dead weight and retaining only what is strictly required for the current project to run as-is.

---

## The Archival Contract

### 1. What to Archive & Purge

- **Stale & Intermediate Checkpoints**: Outdated epoch checkpoints, rejected architecture weights, candidate run artifacts. Only retain the current best validated incumbent (tracked in `models/best_model.json` or active inference config).
- **Web-Backed & Temporary Datasets**: Scraped web data, raw API dumps, ticker histories, and training sets that are uploaded to Kaggle, S3, or remote storage. Record the remote dataset slug/location in the archive.
- **Ephemeral Logs & Caches**: Tensorboard logs, profiling traces, debug dumps, `.cache/`, `__pycache__/`, and transient temporary outputs.
- **Superseded Exploratory Scripts**: Scripts testing dead-end hypotheses or abandoned variations. Synthesize their mechanics and findings into the archive before deleting.

### 2. What to Keep

- **Current Executable Code**: Canonical run scripts, production pipelines, active training and inference entry points.
- **Active Model Checkpoints**: The single current production/best checkpoint needed to run inference or resume canonical training.
- **Core Configurations**: `requirements.txt`, environment templates (`.env.example`), configuration files, schema definitions.
- **Verified Tests**: Durable tests validating current system invariants.

---

## Archival Protocol

1. **Audit Project Footprint**:
   - Inspect disk usage: `du -sh * | sort -h`.
   - Inspect git status: `git status -s`.
   - Identify active vs historical assets.

2. **Synthesize `RESEARCH_ARCHIVE.md`**:
   Before removing files, record:
   - **Hypotheses & Explanations Tested**: What methods/architectures were tested and why.
   - **Empirical Findings & Metrics**: Performance tables, failure modes, mechanism discoveries.
   - **External Storage Pointers**: Links or identifiers for datasets hosted on Kaggle, HuggingFace, or remote servers.
   - **Current Project Architecture**: How to execute the cleaned project from scratch.

3. **Purge Stale Artifacts**:
   - Delete obsolete checkpoints, raw temp datasets, and abandoned scratch files.
   - Update `.gitignore` to prevent re-committing large artifacts (`*.ckpt`, `*.pt`, `*.bin`, `*.h5`, `data/temp/`).

4. **Verify Operability**:
   - Run a smoke test on the canonical runner script to ensure the clean workspace executes without missing dependency errors.

5. **Commit & Push**:
   - Stage changes, commit with `chore: archive research to RESEARCH_ARCHIVE.md and purge stale artifacts`, and push to upstream remote.
