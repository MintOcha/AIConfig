#!/usr/bin/env python3
"""Backfill datasets/<family>.parquet from existing CLIProxyAPI request logs.

Parses the plaintext logs/ files (REQUEST BODY + RESPONSE sections) with the
same protocol extractors as the live Go logger, and writes one parquet file
per model family. Safe to re-run: merges with existing parquet files,
deduplicating on request_id.

Usage:
    python3 scripts/backfill-datasets.py [--logs logs] [--out datasets] [--limit N]
"""

import argparse
import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

SECTION_RE = re.compile(r"^=== (.+) ===$")
TS_RE = re.compile(r"^Timestamp:\s*(.+)$")
URL_RE = re.compile(r"^URL:\s*(\S+)")
STATUS_RE = re.compile(r"^Status:\s*(\d+)")

def classify(model: str) -> str:
    # family = first dash-separated token longer than 2 chars (mirrors Go ClassifyModelFamily)
    name = (model or "").strip().lower().split("/")[-1]
    if not name or name == "unknown":
        return "other"
    for tok in name.split("-"):
        if len(tok) > 2:
            return tok
    return "other"


def get(obj, path, default=None):
    cur = obj
    for part in path.split("."):
        if isinstance(cur, dict) and part in cur:
            cur = cur[part]
        else:
            return default
    return cur


def block_text(content) -> str:
    if isinstance(content, str):
        return content.strip()
    if isinstance(content, list):
        parts = []
        for b in content:
            if isinstance(b, str):
                parts.append(b)
            elif isinstance(b, dict):
                t = b.get("type", "")
                if t in ("text", "input_text"):
                    if (v := str(b.get("text", "")).strip()):
                        parts.append(v)
                elif t in ("image_url", "image", "input_image", "file"):
                    parts.append("[image]")
                elif t in ("tool_use", "function_call", "custom_tool_call"):
                    name = b.get("name") or get(b, "function.name", "")
                    parts.append(f"[tool_call {name}] {b.get('input', b.get('arguments', ''))}")
                elif t in ("tool_result", "function_call_output", "custom_tool_call_output", "tool"):
                    out = b.get("content", b.get("output", ""))
                    parts.append(f"[tool_result] {out}")
        return "\n".join(p for p in parts if p)
    return ""


def extract_prompt(body: dict):
    system, turns = "", []
    msgs = body.get("messages")
    if isinstance(msgs, list) and "system" in body:  # anthropic first (shapes overlap)
        system = block_text(body.get("system", "")).strip()
        for m in msgs:
            if isinstance(m, dict):
                turns.append((m.get("role", ""), block_text(m.get("content", ""))))
    elif isinstance(msgs, list):  # openai chat
        for m in msgs:
            if not isinstance(m, dict):
                continue
            role = m.get("role", "")
            if role in ("system", "developer"):
                t = block_text(m.get("content", "")).strip()
                if t:
                    system = (system + "\n\n" + t).strip() if system else t
            else:
                turns.append((role, block_text(m.get("content", ""))))
        if not system and isinstance(body.get("instructions"), str):
            system = body["instructions"].strip()
    elif "input" in body:  # responses api
        if isinstance(body.get("instructions"), str):
            system = body["instructions"].strip()
        inp = body["input"]
        if isinstance(inp, str):
            turns.append(("user", inp.strip()))
        elif isinstance(inp, list):
            for item in inp:
                if not isinstance(item, dict):
                    continue
                typ = item.get("type", "")
                if typ == "message":
                    turns.append((item.get("role", ""), block_text(item.get("content", ""))))
                elif typ in ("function_call", "custom_tool_call"):
                    turns.append(("assistant", f"[tool_call {item.get('name', '')}] {item.get('arguments', '')}"))
                elif typ in ("function_call_output", "custom_tool_call_output"):
                    turns.append(("tool", f"[tool_result] {item.get('output', '')}"))
    elif isinstance(body.get("contents"), list):  # gemini
        si = body.get("systemInstruction", body.get("system_instruction", {}))
        texts = [p.get("text", "") for p in si.get("parts", []) if isinstance(p, dict)]
        system = "\n".join(t for t in texts if t).strip()
        for c in body["contents"]:
            role = c.get("role", "")
            role = "assistant" if role == "model" else role
            parts = []
            for p in c.get("parts", []):
                if not isinstance(p, dict):
                    continue
                if p.get("thought"):
                    continue
                if "text" in p:
                    parts.append(p["text"])
                elif "functionCall" in p:
                    fc = p["functionCall"]
                    parts.append(f"[tool_call {fc.get('name', '')}] {fc.get('args', '')}")
                elif "functionResponse" in p:
                    fr = p["functionResponse"]
                    parts.append(f"[tool_result {fr.get('name', '')}] {fr.get('response', '')}")
            if parts:
                turns.append((role, "\n".join(parts)))
    else:
        for key in ("prompt", "input", "query", "text"):
            if isinstance(body.get(key), str) and body[key].strip():
                return "", body[key].strip()
    out = []
    for role, text in turns:
        text = (text or "").strip()
        if text:
            out.append((role, f"[{role}]\n{text}" if role else text))
    # delta rule (mirrors Go): keep only turns after the last assistant message
    last_asst = max((i for i, (role, _) in enumerate(out) if role == "assistant"), default=-1)
    return system, "\n\n".join(t for _, t in out[last_asst + 1:])


def content_text(content) -> str:
    if isinstance(content, str):
        return content.strip()
    if isinstance(content, list):
        parts = []
        for p in content:
            if isinstance(p, str):
                parts.append(p)
            elif isinstance(p, dict):
                t = p.get("type", "")
                if t in ("text", "input_text", "output_text"):
                    parts.append(str(p.get("text", "")).strip())
        return "".join(p for p in parts if p)
    return ""


def extract_response(body: dict):
    resp, thought = [], []
    for ch in body.get("choices", []) or []:
        msg = ch.get("message", ch)
        if isinstance(msg, dict):
            if (t := content_text(msg.get("content", ""))):
                resp.append(t)
            if (t := str(msg.get("reasoning_content", "")).strip()):
                thought.append(t)
    for item in body.get("output", []) or []:
        if not isinstance(item, dict):
            continue
        if item.get("type") == "message":
            for c in item.get("content", []) or []:
                if c.get("type") == "output_text" and c.get("text"):
                    resp.append(c["text"])
        elif item.get("type") == "reasoning":
            for s in item.get("summary", []) or []:
                if s.get("text"):
                    thought.append(s["text"])
    for b in body.get("content", []) or []:
        if b.get("type") == "text" and b.get("text"):
            resp.append(b["text"])
        elif b.get("type") in ("thinking", "redacted_thinking") and b.get("thinking"):
            thought.append(b["thinking"])
    for cand in body.get("candidates", []) or []:
        for p in get(cand, "content.parts", []) or []:
            if p.get("thought"):
                if p.get("text"):
                    thought.append(p["text"])
            elif p.get("text"):
                resp.append(p["text"])
    return "".join(resp), "".join(thought)


def parse_sse(text: str):
    resp, thought = [], []
    for event in text.replace("\r\n", "\n").split("\n\n"):
        name, datas = "", []
        for line in event.split("\n"):
            line = line.strip()
            if line.startswith("event:"):
                name = line[6:].strip()
            elif line.startswith("data:"):
                d = line[5:].strip()
                if d and d != "[DONE]":
                    datas.append(d)
        for d in datas:
            try:
                obj = json.loads(d)
            except json.JSONDecodeError:
                continue
            if not isinstance(obj, dict):
                continue
            if name.startswith("response."):
                if name in ("response.output_text.delta", "response.function_call_arguments.delta"):
                    if isinstance(obj.get("delta"), str):
                        resp.append(obj["delta"])
                elif name in ("response.reasoning_summary_text.delta", "response.reasoning_text.delta"):
                    if isinstance(obj.get("delta"), str):
                        thought.append(obj["delta"])
                continue
            delta_txt = get(obj, "delta.text")
            if isinstance(delta_txt, str) and delta_txt:
                resp.append(delta_txt)
                continue
            for ch in obj.get("choices", []) or []:
                delta = ch.get("delta", ch.get("message", {}))
                if isinstance(delta, dict):
                    if (t := content_text(delta.get("content", ""))):
                        resp.append(t)
                    if (t := str(delta.get("reasoning_content", "")).strip()):
                        thought.append(t)
                    for tc in delta.get("tool_calls", []) or []:
                        fn = (tc.get("function", tc) if isinstance(tc, dict) else {})
                        if isinstance(fn, dict):
                            if fn.get("name"):
                                resp.append(f"[tool_call {fn['name']}]")
                            if fn.get("arguments"):
                                resp.append(str(fn["arguments"]))
            for cand in obj.get("candidates", []) or []:
                for p in get(cand, "content.parts", []) or []:
                    if p.get("text"):
                        (thought if p.get("thought") else resp).append(p["text"])
    return "".join(resp), "".join(thought)


def extract_usage(body: dict):
    u = body.get("usage", {}) or {}
    m = body.get("usageMetadata", {}) or {}
    p = u.get("prompt_tokens", u.get("input_tokens", m.get("promptTokenCount", 0))) or 0
    c = u.get("completion_tokens", u.get("output_tokens", m.get("candidatesTokenCount", 0))) or 0
    t = u.get("total_tokens", m.get("totalTokenCount", 0)) or 0
    return int(p), int(c), int(t or (p + c))


def parse_log(path: Path):
    """Yield dict rows from one plaintext request log."""
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return
    lines = text.split("\n")
    # split into sections
    sections, cur_name, cur = [], None, []
    for line in lines:
        m = SECTION_RE.match(line.strip())
        if m:
            if cur_name:
                sections.append((cur_name, cur))
            cur_name, cur = m.group(1).strip(), []
        elif cur_name is not None:
            cur.append(line)
    if cur_name:
        sections.append((cur_name, cur))
    if not sections:
        return

    url, ts, request_body = "", None, ""
    for name, body_lines in sections:
        if name == "REQUEST INFO":
            for ln in body_lines:
                if (m := URL_RE.match(ln)):
                    url = m.group(1)
                if (m := TS_RE.match(ln)):
                    try:
                        ts = datetime.fromisoformat(m.group(1))
                    except ValueError:
                        pass
        elif name == "REQUEST BODY":
            request_body = "\n".join(body_lines).strip()
    if not url or not request_body:
        return
    try:
        req = json.loads(request_body)
        if not isinstance(req, dict):
            return
    except json.JSONDecodeError:
        return
    model = str(req.get("model") or req.get("model", {}).get("name", "") if isinstance(req.get("model"), dict) else req.get("model", "") or "unknown")
    # gemini url fallback
    if model in ("", "unknown") and "/models/" in url:
        rest = url.split("/models/", 1)[1].split(":")[0].split("?")[0].strip()
        model = rest or "unknown"
    family = classify(model)
    system, prompt = extract_prompt(req)
    tools = json.dumps(req["tools"]) if isinstance(req.get("tools"), list) and req["tools"] else ""
    req_id = path.name.rsplit("-", 1)[-1].split(".")[0]

    for name, body_lines in sections:
        if name != "RESPONSE":
            continue
        # split headers from body: headers end at first blank after Status
        body_text = "\n".join(body_lines)
        # drop leading Status/headers block: find first blank line, body follows
        parts = body_text.split("\n\n", 1)
        payload = parts[1] if len(parts) > 1 else body_text
        status = 200
        for ln in body_lines[:15]:
            if (m := STATUS_RE.match(ln)):
                status = int(m.group(1))
        is_stream = "text/event-stream" in body_text[:2000]
        if is_stream:
            answer, reasoning = parse_sse(payload)
            pt = ct = tt = 0
        else:
            try:
                rbody = json.loads(payload) if payload.strip().startswith("{") else {}
            except json.JSONDecodeError:
                rbody = {}
            answer, reasoning = extract_response(rbody if isinstance(rbody, dict) else {})
            pt, ct, tt = extract_usage(rbody if isinstance(rbody, dict) else {})
        yield {
            "timestamp": ts or datetime.now(timezone.utc),
            "request_id": req_id,
            "family": family,
            "model": model,
            "endpoint": url,
            "format": ("openai-chat" if "/chat/completions" in url else
                       "openai-responses" if "/responses" in url else
                       "anthropic" if "/messages" in url else
                       "gemini" if "generateContent" in url else "other"),
            "system": system,
            "prompt": prompt,
            "response": answer,
            "reasoning": reasoning,
            "tools_json": tools,
            "prompt_tokens": pt,
            "completion_tokens": ct,
            "total_tokens": tt,
            "latency_ms": 0,
            "stream": is_stream,
            "status": status,
            "raw_request": request_body,
            "raw_response": payload,
        }


def main() -> int:
    import pyarrow as pa
    import pyarrow.parquet as pq

    ap = argparse.ArgumentParser()
    ap.add_argument("--logs", default="logs")
    ap.add_argument("--out", default="datasets")
    ap.add_argument("--limit", type=int, default=0)
    args = ap.parse_args()

    logdir = Path(args.logs)
    outdir = Path(args.out)
    outdir.mkdir(parents=True, exist_ok=True)
    files = sorted(logdir.glob("*.log"))
    files = [f for f in files if not f.name.startswith("error-")]
    if args.limit:
        files = files[: args.limit]
    print(f"scanning {len(files)} log files...")

    # load existing request_ids per family for dedup
    seen: dict[str, set] = {}
    for pf in outdir.glob("*.parquet"):
        if pf.name.startswith("."):
            continue
        try:
            seen[pf.stem] = set(pq.read_table(pf, columns=["request_id"]).column("request_id").to_pylist())
        except Exception:
            seen[pf.stem] = set()

    def norm_table(rows):
        table_new = pa.Table.from_pylist(rows)
        # match live logger schema: unitless UTC micros
        if pa.types.is_timestamp(table_new.schema.field("timestamp").type):
            table_new = table_new.set_column(
                table_new.schema.get_field_index("timestamp"), "timestamp",
                table_new.column("timestamp").cast(pa.timestamp("us")))
        # match live logger schema: int32 status
        if table_new.schema.field("status").type != pa.int32():
            table_new = table_new.set_column(
                table_new.schema.get_field_index("status"), "status",
                table_new.column("status").cast(pa.int32()))
        return table_new

    def append_sidecar(fam, rows):
        # single write per family: no incremental merge, no re-read
        path = outdir / f".{fam}.new.parquet"
        table_new = norm_table(rows)
        pq.write_table(table_new, path, compression="snappy")
        n = table_new.num_rows
        del table_new
        return n

    totals: dict[str, int] = {}
    new_rows: dict[str, list] = {}
    n_logs = n_rows = batch_no = 0
    for i, f in enumerate(files):
        for row in parse_log(f):
            n_logs += 1
            fam = row["family"]
            if row["request_id"] in seen.get(fam, set()):
                continue
            new_rows.setdefault(fam, []).append(row)
            seen.setdefault(fam, set()).add(row["request_id"])
            n_rows += 1
        # write each batch to a UNIQUE sidecar: never re-read, never hold >1 batch in RAM
        if (i + 1) % 25 == 0:
            for fam, rows in sorted(new_rows.items()):
                table_new = norm_table(rows)
                pq.write_table(table_new, outdir / f".{fam}.new-{batch_no:04d}.parquet",
                               compression="snappy")
                del table_new, rows
            new_rows = {}
            batch_no += 1
            import gc as _gc; _gc.collect()
            print(f"  ... {i + 1}/{len(files)} files, {n_rows} new rows", flush=True)
    print(f"parsed {n_logs} responses, {n_rows} new rows")
    # flush leftover rows, then single merge per family: main + all sidecars
    import gc as _gc
    for fam, rows in sorted(new_rows.items()):
        table_new = norm_table(rows)
        pq.write_table(table_new, outdir / f".{fam}.new-{batch_no:04d}.parquet",
                       compression="snappy")
        print(f"  {fam}: +{len(rows)} new rows")
        del table_new, rows
    new_rows = {}
    _gc.collect()
    # group sidecars by family
    fams: dict[str, list] = {}
    for side in sorted(outdir.glob(".*.new-*.parquet")):
        fam = side.name[1:side.name.index(".new-")]
        fams.setdefault(fam, []).append(side)
    for fam, sides in sorted(fams.items()):
        path = outdir / f"{fam}.parquet"
        writer = None
        target = None
        if path.exists():
            target = pq.read_schema(path)
            writer = pq.ParquetWriter(path.with_suffix(".tmp"), target, compression="snappy")
            for batch in pq.ParquetFile(path).iter_batches(batch_size=200):
                writer.write_batch(batch, row_group_size=200)
            _gc.collect()
        for side in sides:
            if writer is None:
                target = pq.read_schema(side)
                writer = pq.ParquetWriter(path.with_suffix(".tmp"), target, compression="snappy")
            for batch in pq.ParquetFile(side).iter_batches(batch_size=200):
                if batch.schema != target:
                    try:
                        tbl = pa.Table.from_batches([batch])
                        cols = []
                        for field in target:
                            if field.name in tbl.schema.names:
                                col = tbl.column(field.name).cast(field.type)
                            else:
                                col = pa.array([None] * tbl.num_rows, type=field.type)
                            cols.append(col)
                        batch = pa.Table.from_arrays(cols, schema=target).to_batches()[0]
                    except Exception:
                        pass
                writer.write_batch(batch, row_group_size=200)
            del batch
            _gc.collect()
            side.unlink()
        writer.close()
        path.with_suffix(".tmp").replace(path)
        print(f"  {fam}.parquet merged ({len(sides)} parts)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
