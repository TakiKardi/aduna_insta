#!/usr/bin/env python3
"""Aduna -> Instagram : outils du pipeline.

Claude fait le tri et les légendes. Ce script fait le reste :
  scan                 lit l'album iCloud, télécharge les nouvelles photos
  prepare GUID...      fabrique des JPEG au bon ratio pour Instagram
  publish PLAN.json    héberge les images, publie, met à jour le journal
  quota                cadence du jour (combien de posts on a le droit de faire)

Le token Instagram n'apparaît nulle part : le proxy de l'environnement ajoute
« Authorization: Bearer » tout seul sur graph.instagram.com.
"""
import json
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import requests
from PIL import Image, ImageOps

ALBUM_TOKEN = "013P7_YVnUH7tkMk2CxmGqnNQ"
IG_USER_ID = "17841480895256563"
GRAPH = "https://graph.instagram.com/v21.0"

# Hébergement public : un dépôt GitHub public, servi par raw.githubusercontent.com
HOST_REPO_DIR = Path("/home/user/aduna_insta")
HOST_RAW_BASE = "https://raw.githubusercontent.com/TakiKardi/aduna_insta"

ROOT = Path("/mnt/project-files/aduna/insta")  # journal et photos : dans les fichiers du projet Claude
JOURNAL = ROOT / "journal.json"
CACHE = ROOT / "cache"      # originaux téléchargés depuis iCloud
OUT = ROOT / "out"          # JPEG prêts pour Instagram

MIN_RATIO, MAX_RATIO = 4 / 5, 1.91  # largeur / hauteur acceptés par Instagram
MAX_WIDTH = 1440


# ---------- journal ----------

def load_journal():
    if JOURNAL.exists():
        return json.loads(JOURNAL.read_text())
    return {"photos": {}, "posts": []}


def save_journal(j):
    JOURNAL.write_text(json.dumps(j, indent=2, ensure_ascii=False))


# ---------- iCloud ----------

CK = "database/1/com.apple.photos.cloud/production"


def _ck_post(url, body):
    r = requests.post(url, data=json.dumps(body), headers={"Content-Type": "text/plain"}, timeout=30)
    r.raise_for_status()
    return r.json()


def fetch_album():
    """Lit l'album partagé (« SharedCollection » CloudKit), sans compte Apple.
    1) resolve donne la partition, la zone et un jeton anonyme valable 20 min ;
    2) records/changes liste les CPLAsset (photo retouchée) et CPLMaster (original) ;
    3) on prend le JPEG 2048 px : retouché si dispo, sinon celui de l'original."""
    res = _ck_post(f"https://ckdatabasews.icloud.com/{CK}/public/records/resolve"
                   "?remapEnums=true&getCurrentSyncToken=true",
                   {"shortGUIDs": [{"value": ALBUM_TOKEN}]})["results"][0]
    zone = res["rootRecord"]["zoneID"] if "rootRecord" in res else res["zoneID"]
    token = res["anonymousPublicAccess"]["token"]
    base = res["anonymousPublicAccess"].get("databasePartition") or res["databasePartition"]
    url = (f"{base}/{CK}/shared/records/changes?remapEnums=true&getCurrentSyncToken=true"
           f"&publicAccessAuthToken={requests.utils.quote(token, safe='')}")
    records, sync = [], None
    while True:
        body = {"zoneID": zone, "resultsLimit": 200}
        if sync:
            body["syncToken"] = sync
        page = _ck_post(url, body)
        records += page.get("records", [])
        sync = page.get("syncToken")
        if not page.get("moreComing"):
            break
    live = [r for r in records if not r.get("deleted")]
    masters = {r["recordName"]: r for r in live if r.get("recordType") == "CPLMaster"}
    out = []
    for asset in (r for r in live if r.get("recordType") == "CPLAsset"):
        f = asset.get("fields", {})
        master = masters.get(f.get("masterRef", {}).get("value", {}).get("recordName"), {})
        mf = master.get("fields", {})
        if "movie" in str(mf.get("itemType", {}).get("value", "")):
            continue  # pas de vidéo
        res_ = f.get("resJPEGMedRes") or mf.get("resJPEGMedRes")
        if not res_:
            continue
        dl = res_["value"]["downloadURL"].replace("${f}", "photo.jpg")
        out.append({
            "guid": asset["recordName"],
            "date": f.get("assetDate", {}).get("value"),
            "by": asset.get("created", {}).get("userRecordName"),
            "url": dl,
            "is_video": False,
        })
    return out


def cmd_scan():
    j = load_journal()
    CACHE.mkdir(exist_ok=True)
    new = []
    for a in fetch_album():
        if a["guid"] in j["photos"] or a["is_video"]:
            continue
        dest = CACHE / f"{a['guid']}.jpg"
        r = requests.get(a["url"], timeout=60)
        r.raise_for_status()
        dest.write_bytes(r.content)
        j["photos"][a["guid"]] = {"status": "new", "date": a["date"], "by": a["by"], "file": str(dest)}
        new.append(str(dest))
    save_journal(j)
    pending = [g for g, p in j["photos"].items() if p["status"] == "new"]
    print(json.dumps({"downloaded": new, "pending": pending}, indent=2))


# ---------- préparation des images ----------

def fit_ratio(img):
    """Ramène l'image dans les ratios Instagram en ajoutant des bandes noires (jamais de recadrage)."""
    img = ImageOps.exif_transpose(img).convert("RGB")
    w, h = img.size
    r = w / h
    if r < MIN_RATIO:      # trop haute : on élargit le cadre
        w2, h2 = round(h * MIN_RATIO), h
    elif r > MAX_RATIO:    # trop large : on rehausse le cadre
        w2, h2 = w, round(w / MAX_RATIO)
    else:
        w2, h2 = w, h
    if (w2, h2) != (w, h):
        canvas = Image.new("RGB", (w2, h2), "black")
        canvas.paste(img, ((w2 - w) // 2, (h2 - h) // 2))
        img = canvas
    if img.width > MAX_WIDTH:
        img = img.resize((MAX_WIDTH, round(img.height * MAX_WIDTH / img.width)), Image.LANCZOS)
    return img


def cmd_prepare(guids):
    j = load_journal()
    OUT.mkdir(exist_ok=True)
    for g in guids:
        src = Path(j["photos"][g]["file"])
        dest = OUT / f"{g}.jpg"
        fit_ratio(Image.open(src)).save(dest, "JPEG", quality=92)
        print(dest)


# ---------- hébergement ----------

def _git(*args):
    subprocess.run(["git", "-C", str(HOST_REPO_DIR), *args], check=True)


def host_images(paths):
    """Pousse les JPEG sur une branche jetable sans historique, renvoie leurs URL publiques.
    La branche est supprimée juste après la publication (voir unhost_images) :
    les photos ne restent ni sur main ni dans l'historique."""
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    branch = f"tmp-{stamp}"
    _git("checkout", "--orphan", branch)
    _git("rm", "-rf", "--cached", "--ignore-unmatch", ".")
    names = []
    for p in paths:
        (HOST_REPO_DIR / Path(p).name).write_bytes(Path(p).read_bytes())
        names.append(Path(p).name)
    _git("add", *names)
    _git("commit", "-m", f"Photos temporaires {stamp}")
    _git("push", "-u", "origin", branch)
    urls = [f"{HOST_RAW_BASE}/{branch}/{n}" for n in names]
    for u in urls:  # attendre que GitHub serve bien le fichier
        for _ in range(20):
            if requests.head(u, timeout=10).status_code == 200:
                break
            time.sleep(3)
    return branch, urls


def unhost_images(branch):
    """Revient sur main et tente de supprimer la branche jetable.
    Le proxy git de l'environnement refuse les suppressions de branche (403) :
    dans ce cas on la note dans le journal pour un ménage côté GitHub."""
    _git("checkout", "-f", "main")
    _git("clean", "-fd")
    subprocess.run(["git", "-C", str(HOST_REPO_DIR), "branch", "-D", branch])
    r = subprocess.run(["git", "-C", str(HOST_REPO_DIR), "push", "origin", "--delete", branch])
    if r.returncode != 0:
        j = load_journal()
        j.setdefault("branches_to_delete", []).append(branch)
        save_journal(j)


# ---------- Instagram ----------

def ig(method, path, **params):
    r = requests.request(method, f"{GRAPH}/{path}", params=params, timeout=60)
    if not r.ok:
        raise RuntimeError(f"Instagram {r.status_code}: {r.text}")
    return r.json()


def wait_ready(container_id):
    for _ in range(30):
        s = ig("GET", container_id, fields="status_code")["status_code"]
        if s == "FINISHED":
            return
        if s == "ERROR":
            raise RuntimeError(f"Conteneur {container_id} en erreur")
        time.sleep(5)
    raise RuntimeError(f"Conteneur {container_id} jamais prêt")


def publish_post(image_urls, caption):
    if len(image_urls) == 1:
        cid = ig("POST", f"{IG_USER_ID}/media", image_url=image_urls[0], caption=caption)["id"]
    else:
        children = [ig("POST", f"{IG_USER_ID}/media", image_url=u, is_carousel_item="true")["id"]
                    for u in image_urls]
        for c in children:
            wait_ready(c)
        cid = ig("POST", f"{IG_USER_ID}/media", media_type="CAROUSEL",
                 children=",".join(children), caption=caption)["id"]
    wait_ready(cid)
    return ig("POST", f"{IG_USER_ID}/media_publish", creation_id=cid)["id"]


def cmd_publish(plan_path):
    """PLAN.json = {"guids": [...], "caption": "rouille. contre-jour.", "hashtags": [...]}
    À lancer seulement après le feu vert de Theophile."""
    plan = json.loads(Path(plan_path).read_text())
    j = load_journal()
    files = [OUT / f"{g}.jpg" for g in plan["guids"]]
    branch, urls = host_images(files)
    caption = plan["caption"]
    if plan.get("hashtags"):
        caption += "\n\n" + " ".join(plan["hashtags"])
    try:
        media_id = publish_post(urls, caption)
    finally:
        unhost_images(branch)  # Instagram a sa copie, on efface la nôtre
    now = datetime.now(timezone.utc).isoformat()
    for g in plan["guids"]:
        j["photos"][g].update(status="posted", media_id=media_id, posted_at=now)
    j["posts"].append({"media_id": media_id, "guids": plan["guids"], "caption": caption, "at": now})
    save_journal(j)
    print(json.dumps({"media_id": media_id}))


# ---------- cadence ----------

def posts_allowed(pending):
    """0 photo : rien. 1-3 : 1 post. 4-8 : 2 posts. Au-delà : 3, jamais plus."""
    if pending == 0:
        return 0
    if pending <= 3:
        return 1
    if pending <= 8:
        return 2
    return 3


def cmd_quota():
    j = load_journal()
    pending = sum(1 for p in j["photos"].values() if p["status"] == "new")
    today = datetime.now(timezone.utc).date().isoformat()
    done = sum(1 for p in j["posts"] if p["at"].startswith(today))
    print(json.dumps({"pending": pending, "posted_today": done,
                      "allowed_today": max(0, posts_allowed(pending) - done)}))


if __name__ == "__main__":
    cmd, *args = sys.argv[1:] or ["help"]
    {"scan": lambda: cmd_scan(),
     "prepare": lambda: cmd_prepare(args),
     "publish": lambda: cmd_publish(args[0]),
     "quota": lambda: cmd_quota(),
     }.get(cmd, lambda: print(__doc__))()
