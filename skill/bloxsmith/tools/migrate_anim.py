"""Rewrite old .anim files to the short format, recursively: python migrate_anim.py <dir>
JSON track paths -> slash paths; tracks whose keys are all bare weight=0 are dropped (ancestors are filled in now)."""
import json, pathlib, re, sys
for f in pathlib.Path(sys.argv[1]).rglob("*.anim"):
    out, block = [], None
    def flush():
        if block and not all(re.fullmatch(r"\s*[\d.]+\s+weight=0\s*", k) for k in block[1:]):
            out.extend(block)
    for line in f.read_text(encoding="utf-8").splitlines():
        m = re.match(r"\s*track\s+(\[.*\])\s*$", line)
        if m:
            flush()
            path = json.loads(m.group(1))
            simple = all(isinstance(s, str) or s[1] == 1 for s in path)
            block = ["track " + "/".join(s if isinstance(s, str) else s[0] for s in path) if simple else line]
        elif block is not None and re.match(r"\s*[\d.]", line):
            block.append(line)
        else:
            flush(); block = None; out.append(line)
    flush()
    f.write_text("\n".join(out) + "\n", encoding="utf-8")
    print("migrated", f)
