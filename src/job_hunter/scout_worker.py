"""Run one manual Scout assessment under an Orca worker; no native scheduling here."""
import json
import sys
from pathlib import Path
from .storage import Store
from .scout import assess


def main(argv):
    if len(argv) != 4:
        raise SystemExit("usage: python -m job_hunter.scout_worker DB PROFILE_ID JOB_JSON")
    store = Store(argv[1])
    data = json.loads(Path(argv[3]).read_text(encoding="utf-8"))
    job, report, created = assess(store, data, argv[2])
    print(json.dumps({"job": job, "assessment": report, "created": created}, ensure_ascii=False))


if __name__ == "__main__":
    main(sys.argv)
