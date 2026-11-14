"""python -m stoa.ocr IMG ... | --index SPLIT.jsonl|IMG ... [--jobs N]"""
import json
import sys

from stoa.ocr import index, text_layer


def main() -> None:
    # python -m stoa.ocr IMG ...                     -> summary per image (fills the cache via server/subprocess)
    # python -m stoa.ocr --index SPLIT.jsonl|IMG ... [--jobs N]  -> parallel batch indexing
    args = sys.argv[1:]
    if args[:1] == ["--index"]:
        jobs = 3
        if "--jobs" in args:
            i = args.index("--jobs")
            jobs = int(args[i + 1])
            args = args[:i] + args[i + 2:]
        paths: list[str] = []
        for a in args[1:]:
            if a.endswith(".jsonl"):
                paths += [json.loads(ln)["path"] for ln in open(a) if ln.strip()]
            else:
                paths.append(a)
        index(paths, jobs)
    else:
        for a in args:
            tl = text_layer(a)
            print(a, len(tl.items), tl.seconds, tl.notes[-1])


if __name__ == "__main__":
    main()
