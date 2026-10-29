#!/usr/bin/env python3
"""Aduna -> Instagram : outils du pipeline.

Claude fait le tri et les légendes. Ce script fait le reste :
  scan                 lit l'album iCloud, télécharge les nouvelles photos et vidéos
  prepare GUID...      fabrique des JPEG (ou des MP4) au bon ratio pour Instagram
  frames GUID...       extrait 4 images de chaque vidéo, pour choisir la légende
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
REEL_MIN_RATIO = 9 / 16             # une vidéo seule part en Reel, plein écran vertical accepté
VIDEO_MAX_WIDTH = 1080
CAROUSEL_VIDEO_MAX_S = 60           # au-delà, une vidéo ne peut pas entrer dans un carrousel


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
        is_video = "movie" in str(mf.get("itemType", {}).get("value", ""))
        if is_video:
            # retouchée d'abord, puis l'original ; ffmpeg remet tout au format Instagram
            res_ = (f.get("resVidFullRes") or mf.get("resVidFullRes")
                    or f.get("resVidMedRes") or mf.get("resVidMedRes") or mf.get("resOriginalRes"))
        else:
            res_ = f.get("resJPEGMedRes") or mf.get("resJPEGMedRes")
        if not res_:
            continue
        dl = res_["value"]["downloadURL"].replace("${f}", "video.mov" if is_video else "photo.jpg")
        out.append({
            "guid": asset["recordName"],
            "date": f.get("assetDate", {}).get("value"),
            "by": asset.get("created", {}).get("userRecordName"),
            "url": dl,
            "is_video": is_video,
        })
    return out


def cmd_scan():
    j = load_journal()
    CACHE.mkdir(exist_ok=True)
    new = []
    for a in fetch_album():
        if a["guid"] in j["photos"]:
            continue
        dest = CACHE / f"{a['guid']}.{'mov' if a['is_video'] else 'jpg'}"
        with requests.get(a["url"], timeout=300, stream=True) as r:
            r.raise_for_status()
            with dest.open("wb") as fh:
                for chunk in r.iter_content(1 << 20):
                    fh.write(chunk)
        j["photos"][a["guid"]] = {"status": "new", "date": a["date"], "by": a["by"], "file": str(dest),
                                  "kind": "video" if a["is_video"] else "photo"}
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


def is_video(entry):
    return entry.get("kind") == "video"


def out_file(guid, entry):
    return OUT / f"{guid}.{'mp4' if is_video(entry) else 'jpg'}"


def probe(path):
    """Largeur, hauteur (rotation iPhone comprise), durée et présence de son."""
    info = json.loads(subprocess.run(
        ["ffprobe", "-v", "error", "-show_streams", "-show_format", "-of", "json", str(path)],
        check=True, capture_output=True, text=True).stdout)
    v = next(s for s in info["streams"] if s["codec_type"] == "video")
    w, h = v["width"], v["height"]
    rot = int(v.get("tags", {}).get("rotate", 0))
    for sd in v.get("side_data_list", []):
        rot = int(sd.get("rotation", rot))
    if abs(rot) % 180 == 90:
        w, h = h, w
    return {"w": w, "h": h, "duration": float(info["format"]["duration"]),
            "audio": any(s["codec_type"] == "audio" for s in info["streams"])}


def fit_video(src, dest, min_ratio):
    """Même règle que les photos : bandes noires, jamais de recadrage.
    Sortie H.264 + AAC, 30 i/s max, 1080 px de large max, lisible par Instagram."""
    p = probe(src)
    r = p["w"] / p["h"]
    if r < min_ratio:
        w2, h2 = round(p["h"] * min_ratio), p["h"]
    elif r > MAX_RATIO:
        w2, h2 = p["w"], round(p["w"] / MAX_RATIO)
    else:
        w2, h2 = p["w"], p["h"]
    scale = min(1, VIDEO_MAX_WIDTH / w2)
    W, H = 2 * round(w2 * scale / 2), 2 * round(h2 * scale / 2)  # H.264 veut des dimensions paires
    vf = (f"scale={W}:{H}:force_original_aspect_ratio=decrease:force_divisible_by=2,"
          f"pad={W}:{H}:(ow-iw)/2:(oh-ih)/2:black,fps=fps='min(source_fps,30)',format=yuv420p")
    cmd = ["ffmpeg", "-y", "-v", "error", "-i", str(src)]
    if not p["audio"]:  # Instagram préfère une piste son, même muette
        cmd += ["-f", "lavfi", "-i", "anullsrc=r=48000:cl=stereo", "-shortest"]
    cmd += ["-vf", vf, "-c:v", "libx264", "-preset", "medium", "-crf", "21", "-profile:v", "high",
            "-c:a", "aac", "-b:a", "128k", "-ar", "48000", "-movflags", "+faststart", str(dest)]
    subprocess.run(cmd, check=True)
    return p["duration"]


def cmd_prepare(guids):
    """Une vidéo seule est préparée pour un Reel (jusqu'au 9:16) ; dans un carrousel, 4:5 minimum."""
    j = load_journal()
    OUT.mkdir(exist_ok=True)
    reel = len(guids) == 1
    for g in guids:
        e = j["photos"][g]
        dest = out_file(g, e)
        if is_video(e):
            e["duration"] = fit_video(e["file"], dest, REEL_MIN_RATIO if reel else MIN_RATIO)
            e["prepared_for"] = "reel" if reel else "carousel"
        else:
            fit_ratio(Image.open(e["file"])).save(dest, "JPEG", quality=92)
        print(dest)
    save_journal(j)


def cmd_frames(guids):
    """Quatre images réparties sur la vidéo : de quoi choisir ses un ou deux mots."""
    j = load_journal()
    for g in guids:
        src = j["photos"][g]["file"]
        d = OUT / f"{g}-frames"
        d.mkdir(parents=True, exist_ok=True)
        dur = probe(src)["duration"]
        for i in range(4):
            t = dur * (i + 0.5) / 4
            subprocess.run(["ffmpeg", "-y", "-v", "error", "-ss", f"{t:.2f}", "-i", src, "-frames:v", "1",
                            "-vf", "scale=720:-2", str(d / f"{i + 1}.jpg")], check=True)
        print(d)


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


def wait_ready(container_id, tries=30):
    """Une image est prête en quelques secondes ; une vidéo peut demander plusieurs minutes."""
    for _ in range(tries):
        r = ig("GET", container_id, fields="status_code,status")
        if r["status_code"] == "FINISHED":
            return
        if r["status_code"] == "ERROR":
            raise RuntimeError(f"Conteneur {container_id} en erreur : {r.get('status')}")
        time.sleep(5)
    raise RuntimeError(f"Conteneur {container_id} jamais prêt")


def create_containers(items, caption):
    """items = [(url, "photo" | "video"), ...]. Renvoie le conteneur final, prêt à publier.
    Une vidéo seule devient un Reel (partagé aussi dans la grille)."""
    has_video = any(k == "video" for _, k in items)
    tries = 120 if has_video else 30
    if len(items) == 1:
        url, kind = items[0]
        if kind == "video":
            cid = ig("POST", f"{IG_USER_ID}/media", media_type="REELS", video_url=url,
                     caption=caption, share_to_feed="true")["id"]
        else:
            cid = ig("POST", f"{IG_USER_ID}/media", image_url=url, caption=caption)["id"]
    else:
        children = []
        for url, kind in items:
            media = {"media_type": "VIDEO", "video_url": url} if kind == "video" else {"image_url": url}
            children.append(ig("POST", f"{IG_USER_ID}/media", is_carousel_item="true", **media)["id"])
        for c in children:
            wait_ready(c, tries)
        cid = ig("POST", f"{IG_USER_ID}/media", media_type="CAROUSEL",
                 children=",".join(children), caption=caption)["id"]
    wait_ready(cid, tries)
    return cid


def publish_post(items, caption):
    return ig("POST", f"{IG_USER_ID}/media_publish", creation_id=create_containers(items, caption))["id"]


def check_plan(plan, j):
    """Refuse un plan qu'Instagram rejetterait : fichier manquant, vidéo trop longue en carrousel,
    vidéo préparée pour un Reel mise dans un carrousel (ou l'inverse)."""
    guids = plan["guids"]
    if not 1 <= len(guids) <= 10:
        raise ValueError("un post compte de 1 à 10 éléments")
    for g in guids:
        e = j["photos"][g]
        if not out_file(g, e).exists():
            raise ValueError(f"{g} pas préparé : lancer prepare")
        if is_video(e):
            want = "reel" if len(guids) == 1 else "carousel"
            if e.get("prepared_for") != want:
                raise ValueError(f"{g} préparé pour {e.get('prepared_for')}, relancer prepare avec ce groupe")
            if want == "carousel" and e.get("duration", 0) > CAROUSEL_VIDEO_MAX_S:
                raise ValueError(f"{g} dure {e['duration']:.0f} s : trop long pour un carrousel, en faire un Reel")


def cmd_publish(plan_path):
    """PLAN.json = {"guids": [...], "caption": "rouille. contre-jour.", "hashtags": [...]}
    À lancer seulement après le feu vert de Theophile."""
    plan = json.loads(Path(plan_path).read_text())
    j = load_journal()
    check_plan(plan, j)
    entries = [j["photos"][g] for g in plan["guids"]]
    branch, urls = host_images([out_file(g, e) for g, e in zip(plan["guids"], entries)])
    items = [(u, e.get("kind", "photo")) for u, e in zip(urls, entries)]
    caption = plan["caption"]
    if plan.get("hashtags"):
        caption += "\n\n" + " ".join(plan["hashtags"])
    try:
        media_id = publish_post(items, caption)
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
     "frames": lambda: cmd_frames(args),
     "publish": lambda: cmd_publish(args[0]),
     "quota": lambda: cmd_quota(),
     }.get(cmd, lambda: print(__doc__))()
