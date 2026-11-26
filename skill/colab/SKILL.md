---
name: colab
description: Use when the user asks to run code, models, GPU experiments, or interactive follow-ups on Google Colab. Reuse the installed Colab CLI and repository adapter; keep experiment execution and dependencies inside the Colab runtime.
---

# Google Colab

- If the user chose Colab, use Colab. Do not substitute Kaggle, local inference, or a CPU runtime. If Colab cannot provide the requested accelerator, report the provider error instead of switching venues.
- Local work is orchestration only: inspect the adapter, send code, and read results. Download model weights and experiment dependencies, build inference tools, and run inference **inside the Colab VM**. Do not install a second Colab CLI or resolve a new dependency stack when the installed adapter already works.
- The canonical adapter is `~/Projects/AIConfig/mcp/colab_mcp.py`. Read it for the managed workflow. It owns the pinned CLI and kernel-client override. Run its existing entry point with `uv run --script ~/Projects/AIConfig/mcp/colab_mcp.py --help` if needed; do not replace that environment with `uv run --with google-colab-cli ...`.

## Interactive work: one live session, many calls

Use the repository adapter's `session_*` MCP tools, or its CLI. Both use the existing pinned Colab environment. In the commands below, `colabctl` is this shell function:

```bash
colabctl() { uv run --script "$HOME/Projects/AIConfig/mcp/colab_mcp.py" "$@"; }
colabctl sessions
colabctl new -s gemma-v3-eval --accelerator T4
colabctl exec -s gemma-v3-eval <<'PY'
import subprocess
subprocess.run(['nvidia-smi', '-L'], check=True)
PY
```

Reuse an appropriate session if it belongs to this task; do not stop unrelated sessions. Verify the accelerator before model setup. The upstream CLI's bundled operator skill is available through its `skill` command in the existing uv script environment.

Keep cells short: **never run a model download, build, or a blocking server loop directly in a live `exec` cell**. A busy Jupyter kernel serializes later cells, including diagnostics. Start slow setup with `session_launch` / `launch`, then use `session_job_status` / `job-status` and `session_job_logs` / `job-logs` with the returned job ID. A ready server should bind to loopback. For example:

```bash
colabctl launch -s gemma-v3-eval -- python /content/setup.py
colabctl job-logs -s gemma-v3-eval JOB_ID
colabctl session-status -s gemma-v3-eval
```

Pull artifacts before calling `colabctl stop -s gemma-v3-eval`; a provisioned runtime can consume quota while idle.

For model evaluation, load the model once or start a server inside Colab and keep it bound to loopback. Send the first prompt with one `exec`, read the output, then choose the next prompt and pass the actual prior reply as chat history. Keep model weights and the server in the same session between calls; do not start a new notebook run for each prompt. Save the final-answer text, finish reason, and token usage separately so a truncated thinking response is not mistaken for an answer or a refusal. Use the adapter's managed `push` / `logs` / `wait` / `pull` workflow for unattended batch jobs, not as a substitute for an interactive conversation.
