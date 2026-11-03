#!/usr/bin/env python3
"""Publie sur YouTube les fichiers de publications/*.json (appli Google « BMO Studio Link », API YouTube Data v3).

Un fichier = une vidéo ou un Short. Champs :
  chaine        nom du secret sans préfixe : "LEVRAIPRIX" → secret YT_REFRESH_TOKEN_LEVRAIPRIX
  video_url     lien public du MP4 (Release ou raw GitHub)
  titre, description, tags (liste), categorie (id YouTube, 27 = Éducation)
  miniature_url (facultatif)   commentaire (facultatif, posté sous la vidéo)
  publier_le    (facultatif) date ISO, ex. "2026-11-04T08:00:00+01:00" → la vidéo est mise en ligne à cette heure
  pour_enfants  false par défaut
Après envoi, le fichier part dans publications/fait/ avec l'id YouTube.
Tant que Google n'a pas audité l'appli, YouTube garde les vidéos en privé : il faut les passer en public dans Studio.
"""
import datetime as dt
import glob
import json
import os
import shutil
import sys
import tempfile
import time

import requests
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
from googleapiclient.http import MediaFileUpload

SECRETS = json.loads(os.environ.get("SECRETS_JSON", "{}"))
SECRETS = {k: (v.strip().strip('"').strip() if isinstance(v, str) else v) for k, v in SECRETS.items()}  # espaces, guillemets collés par erreur
CLIENT_ID, CLIENT_SECRET = SECRETS.get("YT_CLIENT_ID"), SECRETS.get("YT_CLIENT_SECRET")


def diagnostic(chaine):
    """Décrit la forme des clés sans jamais les afficher (aide à trouver une erreur de collage)."""
    cid, sec, tok = CLIENT_ID or "", CLIENT_SECRET or "", SECRETS.get(f"YT_REFRESH_TOKEN_{chaine}") or ""
    print(f"  diagnostic : ID client {len(cid)} car., se termine par .apps.googleusercontent.com = {cid.endswith('.apps.googleusercontent.com')}, "
          f"commence par {cid.split('-')[0][:4]}… ; secret {len(sec)} car., commence par GOCSPX = {sec.startswith('GOCSPX')} ; "
          f"token {len(tok)} car., commence par 1// = {tok.startswith('1//')}", flush=True)
SCOPES = ["https://www.googleapis.com/auth/youtube.upload",
          "https://www.googleapis.com/auth/youtube.force-ssl",
          "https://www.googleapis.com/auth/youtube.readonly"]


def service(chaine):
    jeton = SECRETS.get(f"YT_REFRESH_TOKEN_{chaine}")
    if not (CLIENT_ID and CLIENT_SECRET and jeton):
        raise SystemExit(f"Secrets manquants : YT_CLIENT_ID, YT_CLIENT_SECRET ou YT_REFRESH_TOKEN_{chaine}")
    cred = Credentials(None, refresh_token=jeton, client_id=CLIENT_ID, client_secret=CLIENT_SECRET,
                       token_uri="https://oauth2.googleapis.com/token", scopes=SCOPES)
    return build("youtube", "v3", credentials=cred, cache_discovery=False)


def telecharger(url, dossier, nom):
    chemin = os.path.join(dossier, nom)
    with requests.get(url, stream=True, timeout=120) as r:
        r.raise_for_status()
        with open(chemin, "wb") as f:
            for bloc in r.iter_content(1 << 20):
                f.write(bloc)
    return chemin


def ajouter_playlist(yt, vid, titre):
    """Ajoute la vidéo à la playlist de ce titre sur la chaîne (la crée publique si elle n'existe pas)."""
    pid, page = None, None
    while pid is None:
        r = yt.playlists().list(part="snippet", mine=True, maxResults=50, pageToken=page).execute()
        pid = next((x["id"] for x in r.get("items", []) if x["snippet"]["title"].strip().lower() == titre.strip().lower()), None)
        page = r.get("nextPageToken")
        if not page:
            break
    if pid is None:
        pid = yt.playlists().insert(part="snippet,status", body={"snippet": {"title": titre, "defaultLanguage": "fr"},
                                                                  "status": {"privacyStatus": "public"}}).execute()["id"]
    yt.playlistItems().insert(part="snippet", body={"snippet": {"playlistId": pid, "resourceId": {
        "kind": "youtube#video", "videoId": vid}}}).execute()
    return f"ok ({titre})"


def publier(p, yt, dossier):
    video = telecharger(p["video_url"], dossier, "video.mp4")
    statut = {"privacyStatus": "public", "selfDeclaredMadeForKids": bool(p.get("pour_enfants", False))}
    if p.get("publier_le"):
        quand = dt.datetime.fromisoformat(p["publier_le"])
        if quand > dt.datetime.now(dt.timezone.utc) + dt.timedelta(minutes=10):
            statut = {**statut, "privacyStatus": "private",
                      "publishAt": quand.astimezone(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")}
    corps = {"snippet": {"title": p["titre"][:100], "description": p.get("description", "")[:5000],
                         "tags": p.get("tags", []), "categoryId": str(p.get("categorie", 27)),
                         "defaultLanguage": "fr", "defaultAudioLanguage": "fr"},
             "status": statut}
    media = MediaFileUpload(video, mimetype="video/mp4", chunksize=16 << 20, resumable=True)
    req = yt.videos().insert(part="snippet,status", body=corps, media_body=media, notifySubscribers=True)
    rep = None
    while rep is None:
        etat, rep = req.next_chunk()
        if etat:
            print(f"  envoi {int(etat.progress() * 100)} %", flush=True)
    vid = rep["id"]
    print(f"  ✔ vidéo envoyée : https://youtu.be/{vid} (statut {rep['status'].get('privacyStatus')})", flush=True)
    res = {"id": vid, "lien": f"https://youtu.be/{vid}", "statut_youtube": rep["status"].get("privacyStatus")}
    if p.get("miniature_url"):
        try:
            img = telecharger(p["miniature_url"], dossier, "miniature.jpg")
            yt.thumbnails().set(videoId=vid, media_body=MediaFileUpload(img, mimetype="image/jpeg")).execute()
            res["miniature"] = "ok"
        except HttpError as e:
            res["miniature"] = f"refusée : {e.reason if hasattr(e, 'reason') else e}"
        print(f"  miniature : {res['miniature']}", flush=True)
    if p.get("playlist"):
        try:
            res["playlist"] = ajouter_playlist(yt, vid, p["playlist"])
        except HttpError as e:
            res["playlist"] = f"refusée : {e}"
        print(f"  playlist : {res['playlist']}", flush=True)
    if p.get("commentaire") and statut.get("publishAt"):
        res["commentaire"] = "en attente"  # impossible de commenter une vidéo encore privée : posté après la mise en ligne
    elif p.get("commentaire"):
        try:
            yt.commentThreads().insert(part="snippet", body={"snippet": {"videoId": vid, "topLevelComment": {
                "snippet": {"textOriginal": p["commentaire"]}}}}).execute()
            res["commentaire"] = "ok"
        except HttpError as e:
            res["commentaire"] = f"refusé : {e}"
        print(f"  commentaire : {res['commentaire']}", flush=True)
    return res


def mettre_en_ligne(p, yt):
    """Fiche {"action": "mettre_en_ligne", "video_id": "..."} : passe tout de suite en public une vidéo déjà envoyée
    (programmée ou privée), puis avance sa date dans publications/fait/ pour que le premier commentaire parte."""
    vid = p["video_id"]
    items = yt.videos().list(part="status", id=vid).execute().get("items", [])
    if not items:
        raise RuntimeError(f"vidéo {vid} introuvable sur la chaîne")
    statut = {k: v for k, v in items[0]["status"].items()
              if k in ("license", "embeddable", "publicStatsViewable", "selfDeclaredMadeForKids")}
    statut["privacyStatus"] = "public"
    rep = yt.videos().update(part="status", body={"id": vid, "status": statut}).execute()
    maintenant = dt.datetime.now(dt.timezone.utc).isoformat()
    for f in glob.glob("publications/fait/*.json"):
        q = json.load(open(f, encoding="utf-8"))
        if (q.get("resultat") or {}).get("id") == vid:
            q["publier_le"] = maintenant
            q["resultat"]["statut_youtube"] = rep["status"].get("privacyStatus")
            json.dump(q, open(f, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    print(f"  ✔ {vid} mis en ligne (statut {rep['status'].get('privacyStatus')})", flush=True)
    return {"id": vid, "lien": f"https://youtu.be/{vid}", "statut_youtube": rep["status"].get("privacyStatus")}


def commenter(p, yt):
    """Fiche {"action": "commenter", "videos": [...], "commentaire": "..."} : poste le commentaire sur chaque vidéo,
    sauf si la chaîne y a déjà laissé un lien vers une vidéo (pas de doublon)."""
    moi = yt.channels().list(part="id", mine=True).execute()["items"][0]["id"]
    res = {}
    for vid in p["videos"]:
        try:
            fils = yt.commentThreads().list(part="snippet", videoId=vid, maxResults=50).execute().get("items", [])
            deja = any(t["snippet"]["topLevelComment"]["snippet"].get("authorChannelId", {}).get("value") == moi and
                       ("youtu" in t["snippet"]["topLevelComment"]["snippet"].get("textOriginal", ""))
                       for t in fils)
            if deja:
                res[vid] = "déjà un lien"
            else:
                yt.commentThreads().insert(part="snippet", body={"snippet": {"videoId": vid, "topLevelComment": {
                    "snippet": {"textOriginal": p["commentaire"]}}}}).execute()
                res[vid] = "ok"
        except HttpError as e:
            res[vid] = f"refusé : {e}"
        print(f"  commentaire {vid} : {res[vid]}", flush=True)
    return {"commentaires": res}


def commentaires_en_attente():
    """Poste les premiers commentaires des vidéos programmées, une fois qu'elles sont en ligne."""
    maintenant = dt.datetime.now(dt.timezone.utc)
    for f in sorted(glob.glob("publications/fait/*.json")):
        p = json.load(open(f, encoding="utf-8"))
        r = p.get("resultat") or {}
        if not p.get("commentaire") or r.get("commentaire") not in ("en attente",):
            continue
        if p.get("publier_le") and dt.datetime.fromisoformat(p["publier_le"]) > maintenant:
            continue
        try:
            yt = service(p.get("chaine", "LEVRAIPRIX"))
            yt.commentThreads().insert(part="snippet", body={"snippet": {"videoId": r["id"], "topLevelComment": {
                "snippet": {"textOriginal": p["commentaire"]}}}}).execute()
            r["commentaire"] = "ok"
        except HttpError as e:
            r["commentaire"] = "en attente" if e.status_code == 403 else f"refusé : {e}"
        p["resultat"] = r
        json.dump(p, open(f, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
        print(f"commentaire {r['id']} : {r['commentaire']}", flush=True)


def main():
    try:
        commentaires_en_attente()
    except Exception as e:
        print(f"commentaires en attente : {e}", flush=True)
    fichiers = sorted(glob.glob("publications/*.json"))
    if not fichiers:
        print("Rien à publier.")
        return
    erreurs = 0
    for f in fichiers:
        p = json.load(open(f, encoding="utf-8"))
        print(f"→ {f} : {p.get('titre')}", flush=True)
        try:
            with tempfile.TemporaryDirectory() as d:
                yt = service(p.get("chaine", "LEVRAIPRIX"))
                action = p.get("action")
                res = (mettre_en_ligne(p, yt) if action == "mettre_en_ligne" else
                       commenter(p, yt) if action == "commenter" else publier(p, yt, d))
            p["resultat"] = {**res, "envoye_le": dt.datetime.now(dt.timezone.utc).isoformat()}
            json.dump(p, open(f, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
            shutil.move(f, os.path.join("publications/fait", os.path.basename(f)))
        except Exception as e:  # on continue avec les suivants
            erreurs += 1
            print(f"  ✘ échec : {e}", flush=True)
            diagnostic(p.get("chaine", "LEVRAIPRIX"))
        time.sleep(2)
    try:
        commentaires_en_attente()  # commentaires des vidéos mises en ligne à l'instant
    except Exception as e:
        print(f"commentaires en attente : {e}", flush=True)
    sys.exit(1 if erreurs else 0)


if __name__ == "__main__":
    main()
