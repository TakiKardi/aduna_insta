#!/usr/bin/env python3
"""Publie une série de plans validés, l'un après l'autre, avec une pause entre chaque.
Usage : batch_publish.py DOSSIER_PLANS [minutes_entre_posts] [--dry]
--dry : crée les conteneurs Instagram et vérifie qu'ils sont prêts, sans publier."""
import json, sys, time
from pathlib import Path
from datetime import datetime, timezone
sys.path.insert(0, str(Path(__file__).resolve().parent))
import aduna_insta as a

folder = Path(sys.argv[1])
gap = float(sys.argv[2]) if len(sys.argv) > 2 and not sys.argv[2].startswith("--") else 3
dry = "--dry" in sys.argv
log = folder / ("dry-run.log" if dry else "publish.log")
done = {l.split()[1] for l in log.read_text().splitlines() if " OK " in l} if log.exists() else set()

def note(msg):
    line = f"{datetime.now(timezone.utc).isoformat(timespec='seconds')} {msg}"
    print(line, flush=True)
    with log.open("a") as f:
        f.write(line + "\n")

plans = sorted(folder.glob("[0-9][0-9].json"))
for i, p in enumerate(plans):
    if p.stem in done:
        continue  # déjà publié lors d'un lancement précédent
    plan = json.loads(p.read_text())
    try:
        if dry:
            j = a.load_journal()
            a.check_plan(plan, j)
            entries = [j["photos"][g] for g in plan["guids"]]
            branch, urls = a.host_images([a.out_file(g, e) for g, e in zip(plan["guids"], entries)])
            try:
                a.create_containers([(u, e.get("kind", "photo")) for u, e in zip(urls, entries)], plan["caption"])
            finally:
                a.unhost_images(branch)
            note(f"{p.stem} OK dry ({len(urls)} éléments prêts)")
        else:
            a.cmd_publish(str(p))
            note(f"{p.stem} OK publié")
    except Exception as e:
        note(f"{p.stem} ERREUR {e}")
        sys.exit(1)  # on s'arrête au premier problème
    if i < len(plans) - 1 and not dry:
        time.sleep(gap * 60)
note("FIN")
