"""Per-tensor sha256 of every *.safetensors file in a directory (stdlib only; reads the raw byte ranges from
the safetensors header), so two checkpoints with different shard layouts can be compared tensor by tensor.

  python tensor_hash.py <model_dir> > hashes.json
  python tensor_hash.py --compare a.json b.json
"""
import hashlib
import json
import struct
import sys
from pathlib import Path


def hashes(d: Path) -> dict:
    out = {}
    for f in sorted(d.glob("*.safetensors")):
        with open(f, "rb") as fh:
            n = struct.unpack("<Q", fh.read(8))[0]
            hdr = json.loads(fh.read(n))
            base = 8 + n
            for name, meta in sorted(hdr.items(), key=lambda kv: kv[1].get("data_offsets", [0])[0] if kv[0] != "__metadata__" else -1):
                if name == "__metadata__":
                    continue
                a, b = meta["data_offsets"]
                fh.seek(base + a)
                h = hashlib.sha256()
                left = b - a
                while left:
                    chunk = fh.read(min(left, 1 << 24))
                    h.update(chunk)
                    left -= len(chunk)
                out[name] = [meta["dtype"], meta["shape"], h.hexdigest()]
    return out


if __name__ == "__main__":
    if sys.argv[1] == "--compare":
        a, b = (json.load(open(p)) for p in sys.argv[2:4])
        same = [k for k in a if k in b and a[k] == b[k]]
        diff = [k for k in a if k in b and a[k] != b[k]]
        print(json.dumps({"tensors_a": len(a), "tensors_b": len(b), "identical": len(same), "different": len(diff),
                          "only_a": sorted(set(a) - set(b))[:10], "only_b": sorted(set(b) - set(a))[:10],
                          "diff_examples": diff[:10]}, indent=1))
    else:
        print(json.dumps(hashes(Path(sys.argv[1]))))
