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
from zoneinfo import ZoneInfo

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


def ajouter_playlist(yt, vid, titre, position=None):
    """Ajoute la vidéo à la playlist de ce titre sur la chaîne (la crée publique si elle n'existe pas).
    position=0 la met en tête. Réessaie 3 fois si YouTube répond 409 (playlist pas encore prête)."""
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
    snip = {"playlistId": pid, "resourceId": {"kind": "youtube#video", "videoId": vid}}
    if position is not None:
        snip["position"] = position
    for essai in range(4):
        try:
            yt.playlistItems().insert(part="snippet", body={"snippet": snip}).execute()
            break
        except HttpError as e:
            if e.status_code != 409 or essai == 3:
                raise
            time.sleep(5 * (essai + 1))
    return f"ok ({titre})"


PARIS = ZoneInfo("Europe/Paris")
CRENEAUX_SHORTS = [(8, 0), (12, 30), (18, 0)]  # heures de Paris
MAX_SHORTS_PAR_JOUR = 3
ECART_MIN = dt.timedelta(minutes=90)


def est_short(p):
    return p.get("format") == "short" or "#shorts" in p.get("titre", "").lower()


def heure_short(p):
    """Choisit l'heure de mise en ligne d'un Short : au plus 3 par jour, aux créneaux 8 h / 12 h 30 / 18 h (Paris),
    jamais à moins de 90 min d'un autre Short de la même chaîne. Garde l'heure demandée si elle respecte ces règles.
    Mettre "forcer_heure": true dans la fiche pour publier exactement à l'heure demandée."""
    chaine = p.get("chaine", "LEVRAIPRIX")
    pris = []
    for f in glob.glob("publications/fait/*.json"):
        q = json.load(open(f, encoding="utf-8"))
        if q.get("action") or q.get("chaine", "LEVRAIPRIX") != chaine or not est_short(q):
            continue
        quand = q.get("publier_le") or (q.get("resultat") or {}).get("envoye_le")
        if quand:
            pris.append(dt.datetime.fromisoformat(quand).astimezone(PARIS))
    maintenant = dt.datetime.now(PARIS) + dt.timedelta(minutes=15)
    voulu = max(dt.datetime.fromisoformat(p["publier_le"]).astimezone(PARIS), maintenant) if p.get("publier_le") else maintenant

    def libre(t):
        return (sum(1 for x in pris if x.date() == t.date()) < MAX_SHORTS_PAR_JOUR and
                all(abs(x - t) >= ECART_MIN for x in pris))
    if p.get("publier_le") and libre(voulu):
        return voulu, False
    jour = voulu.date()
    for _ in range(60):
        for h, m in CRENEAUX_SHORTS:
            t = dt.datetime(jour.year, jour.month, jour.day, h, m, tzinfo=PARIS)
            if t >= voulu and libre(t):
                return t, True
        jour += dt.timedelta(days=1)
    return voulu, False


def publier(p, yt, dossier):
    if est_short(p) and not p.get("forcer_heure"):
        quand, change = heure_short(p)
        if change:
            print(f"  rythme : Short programmé au {quand:%d/%m %H:%M} (Paris) au lieu de "
                  f"{p.get('publier_le') or 'tout de suite'} (max {MAX_SHORTS_PAR_JOUR} par jour, 90 min d'écart)", flush=True)
            p["heure_demandee"] = p.get("publier_le")
            p["publier_le"] = quand.isoformat()
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


def reprogrammer(p, yt):
    """Fiche {"action": "reprogrammer", "video_id": "...", "publier_le": "2026-11-04T13:00:00+01:00"} :
    change l'heure de mise en ligne d'une vidéo programmée (et de son premier commentaire)."""
    vid = p["video_id"]
    quand = dt.datetime.fromisoformat(p["publier_le"])
    items = yt.videos().list(part="status", id=vid).execute().get("items", [])
    if not items:
        raise RuntimeError(f"vidéo {vid} introuvable sur la chaîne")
    statut = {k: v for k, v in items[0]["status"].items()
              if k in ("license", "embeddable", "publicStatsViewable", "selfDeclaredMadeForKids")}
    statut.update(privacyStatus="private", publishAt=quand.astimezone(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z"))
    rep = yt.videos().update(part="status", body={"id": vid, "status": statut}).execute()
    for f in glob.glob("publications/fait/*.json"):
        q = json.load(open(f, encoding="utf-8"))
        if (q.get("resultat") or {}).get("id") == vid:
            q["publier_le"] = p["publier_le"]
            json.dump(q, open(f, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    print(f"  ✔ {vid} reprogrammée au {rep['status'].get('publishAt')}", flush=True)
    return {"id": vid, "publier_le": p["publier_le"]}


def garder_prive(p, yt):
    """Fiche {"action": "garder_prive", "video_id": "..."} : annule la mise en ligne programmée (la vidéo reste privée,
    rien n'est supprimé) et le premier commentaire en attente."""
    vid = p["video_id"]
    items = yt.videos().list(part="status", id=vid).execute().get("items", [])
    if not items:
        raise RuntimeError(f"vidéo {vid} introuvable sur la chaîne")
    statut = {k: v for k, v in items[0]["status"].items()
              if k in ("license", "embeddable", "publicStatsViewable", "selfDeclaredMadeForKids")}
    statut["privacyStatus"] = "private"
    yt.videos().update(part="status", body={"id": vid, "status": statut}).execute()
    for f in glob.glob("publications/fait/*.json"):
        q = json.load(open(f, encoding="utf-8"))
        if (q.get("resultat") or {}).get("id") == vid:
            q["resultat"]["commentaire"] = "annulé (vidéo gardée privée)"
            q["resultat"]["statut_youtube"] = "private"
            json.dump(q, open(f, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    print(f"  ✔ {vid} gardée privée (programmation annulée)", flush=True)
    return {"id": vid, "statut_youtube": "private"}


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


def playlist(p, yt):
    """Fiche {"action": "playlist", "videos": ["id", ...], "playlist": "Titre", "en_tete": "id"} : ajoute les vidéos
    à la playlist (créée publique si elle n'existe pas) ; "en_tete" (facultatif) est placée en première position."""
    res = {}
    for vid in p["videos"]:
        try:
            res[vid] = ajouter_playlist(yt, vid, p["playlist"], 0 if vid == p.get("en_tete") else None)
        except HttpError as e:
            res[vid] = f"refusée : {e}"
        print(f"  playlist {vid} : {res[vid]}", flush=True)
    return {"playlist": res}


def id_playlist(yt, titre):
    page = None
    while True:
        r = yt.playlists().list(part="snippet", mine=True, maxResults=50, pageToken=page).execute()
        pid = next((x["id"] for x in r.get("items", []) if x["snippet"]["title"].strip().lower() == titre.strip().lower()), None)
        page = r.get("nextPageToken")
        if pid or not page:
            return pid


def section_playlist(p, yt):
    """Fiche {"action": "section_playlist", "playlist": "Titre", "position": 0, "description": "..."} : met la playlist
    en section de la page d'accueil de la chaîne (en tête par défaut) et lui donne une description. Ne crée pas de doublon."""
    pid = id_playlist(yt, p["playlist"])
    if not pid:
        raise RuntimeError(f"playlist « {p['playlist']} » introuvable")
    res = {"playlist_id": pid, "lien": f"https://www.youtube.com/playlist?list={pid}"}
    if p.get("description"):
        sn = yt.playlists().list(part="snippet", id=pid).execute()["items"][0]["snippet"]
        yt.playlists().update(part="snippet", body={"id": pid, "snippet": {"title": sn["title"], "description": p["description"][:5000],
                                                                             "defaultLanguage": sn.get("defaultLanguage", "fr")}}).execute()
        res["description"] = "ok"
    secs = yt.channelSections().list(part="snippet,contentDetails", mine=True).execute().get("items", [])
    if any(pid in (x.get("contentDetails") or {}).get("playlists", []) for x in secs):
        res["section"] = "déjà présente"
    else:
        try:
            yt.channelSections().insert(part="snippet,contentDetails", body={
                "snippet": {"type": "singlePlaylist", "position": int(p.get("position", 0))},
                "contentDetails": {"playlists": [pid]}}).execute()
            res["section"] = "ok"
        except HttpError as e:
            res["section"] = f"refusée : {e}"
    print(f"  section {p['playlist']} : {res}", flush=True)
    return res


def lire_chaine(p, yt):
    """Fiche {"action": "lire_chaine"} : relève la présentation de la chaîne (description, mots-clés, bande-annonce),
    ses playlists et les derniers commentaires des spectateurs. Rien n'est modifié."""
    ch = yt.channels().list(part="id,snippet,brandingSettings", mine=True).execute()["items"][0]
    pls, page = [], None
    while True:
        r = yt.playlists().list(part="snippet,contentDetails,status", mine=True, maxResults=50, pageToken=page).execute()
        pls += [{"id": x["id"], "titre": x["snippet"]["title"], "videos": x["contentDetails"]["itemCount"],
                 "statut": x["status"]["privacyStatus"]} for x in r.get("items", [])]
        page = r.get("nextPageToken")
        if not page:
            break
    coms = []
    try:
        r = yt.commentThreads().list(part="snippet", allThreadsRelatedToChannelId=ch["id"], maxResults=50,
                                     order="time").execute()
        for t in r.get("items", []):
            s = t["snippet"]["topLevelComment"]["snippet"]
            coms.append({"fil": t["id"], "video": t["snippet"].get("videoId"), "auteur": s.get("authorDisplayName"),
                         "de_la_chaine": s.get("authorChannelId", {}).get("value") == ch["id"],
                         "texte": s.get("textOriginal", "")[:300], "reponses": t["snippet"].get("totalReplyCount", 0),
                         "date": s.get("publishedAt")})
    except HttpError as e:
        coms = f"indisponible : {e}"
    bs = ch.get("brandingSettings", {}).get("channel", {})
    print(f"  chaîne {ch['snippet']['title']} : {len(pls)} playlists, {len(coms) if isinstance(coms, list) else 0} commentaires", flush=True)
    return {"chaine_id": ch["id"], "titre": ch["snippet"]["title"], "description": bs.get("description", ""),
            "mots_cles": bs.get("keywords", ""), "bande_annonce": bs.get("unsubscribedTrailer"),
            "pays": bs.get("country"), "playlists": pls, "commentaires": coms}


def chaine(p, yt):
    """Fiche {"action": "chaine", "description": "...", "mots_cles": "...", "bande_annonce": "id vidéo", "pays": "FR"} :
    met à jour la présentation de la chaîne. Seuls les champs donnés changent ; les anciennes valeurs sont gardées
    dans le résultat pour pouvoir revenir en arrière."""
    ch = yt.channels().list(part="id,brandingSettings", mine=True).execute()["items"][0]
    bs = ch.get("brandingSettings", {})
    c = dict(bs.get("channel", {}))
    avant = {k: c.get(k) for k in ("description", "keywords", "unsubscribedTrailer", "country")}
    for champ, cle in (("description", "description"), ("mots_cles", "keywords"),
                       ("bande_annonce", "unsubscribedTrailer"), ("pays", "country")):
        if p.get(champ):
            c[cle] = p[champ]
    corps = {"id": ch["id"], "brandingSettings": {"channel": c, **({"image": bs["image"]} if bs.get("image") else {})}}
    yt.channels().update(part="brandingSettings", body=corps).execute()
    print(f"  ✔ présentation de la chaîne mise à jour ({', '.join(k for k in ('description', 'mots_cles', 'bande_annonce', 'pays') if p.get(k))})", flush=True)
    return {"avant": avant, "apres": {k: c.get(k) for k in avant}}


def repondre(p, yt):
    """Fiche {"action": "repondre", "reponses": [{"fil": "id du fil", "texte": "..."}]} : répond aux commentaires."""
    res = {}
    for r in p["reponses"]:
        try:
            yt.comments().insert(part="snippet", body={"snippet": {"parentId": r["fil"], "textOriginal": r["texte"]}}).execute()
            res[r["fil"]] = "ok"
        except HttpError as e:
            res[r["fil"]] = f"refusé : {e}"
        print(f"  réponse {r['fil']} : {res[r['fil']]}", flush=True)
    return {"reponses": res}


def chercher(p, yt):
    """Fiche {"action": "chercher", "requetes": ["prix gazole", ...], "jours": 3} : relève les vidéos françaises récentes
    les plus vues sur ces sujets (titre, chaîne, vues, commentaires). Rien n'est publié."""
    apres = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=p.get("jours", 3))).strftime("%Y-%m-%dT%H:%M:%SZ")
    res = {}
    for q in p["requetes"]:
        r = yt.search().list(part="snippet", q=q, type="video", order="viewCount", publishedAfter=apres,
                             regionCode="FR", relevanceLanguage="fr", maxResults=15).execute()
        ids = [x["id"]["videoId"] for x in r.get("items", [])]
        vids = yt.videos().list(part="snippet,statistics,contentDetails", id=",".join(ids)).execute().get("items", []) if ids else []
        res[q] = [{"id": v["id"], "titre": v["snippet"]["title"], "chaine": v["snippet"]["channelTitle"],
                   "publie": v["snippet"]["publishedAt"], "duree": v["contentDetails"]["duration"],
                   "vues": int(v["statistics"].get("viewCount", 0)), "commentaires": v["statistics"].get("commentCount")}
                  for v in vids]
        print(f"  {q} : {len(res[q])} vidéos", flush=True)
    return {"resultats": res}


def longueur_tags(tags):
    """Longueur comptée par YouTube (limite 500) : un tag avec espace compte avec ses guillemets, plus une virgule entre tags."""
    return sum(len(t.encode("utf-8")) + (2 if " " in t else 0) for t in tags) + max(len(tags) - 1, 0)


def modifier(p, yt, dossier=None):
    """Fiche {"action": "modifier", "video_id": "...", "titre": "...", "description_prefixe": "...", "tags_ajout": [...], "miniature_url": "..."} :
    change le titre, ajoute une phrase en tête de description (si elle n'y est pas déjà) et ajoute des tags (sans doublon,
    < 500 caractères). Les anciennes valeurs sont gardées dans le résultat pour revenir en arrière."""
    vid = p["video_id"]
    items = yt.videos().list(part="snippet", id=vid).execute().get("items", [])
    if not items:
        raise RuntimeError(f"vidéo {vid} introuvable sur la chaîne")
    sn = items[0]["snippet"]
    avant = {"titre": sn.get("title"), "description": sn.get("description", ""), "tags": sn.get("tags", [])}
    corps = {k: sn[k] for k in ("title", "description", "tags", "categoryId", "defaultLanguage", "defaultAudioLanguage") if k in sn}
    if p.get("titre"):
        corps["title"] = p["titre"][:100]
    pre = p.get("description_prefixe")
    if pre and not corps.get("description", "").startswith(pre):
        corps["description"] = (pre + "\n\n" + corps.get("description", ""))[:5000]
    tags = list(corps.get("tags", []))
    for t in p.get("tags_ajout", []):
        if t.lower() not in [x.lower() for x in tags] and longueur_tags(tags + [t]) <= 450:
            tags.append(t)
    corps["tags"] = tags
    yt.videos().update(part="snippet", body={"id": vid, "snippet": corps}).execute()
    print(f"  ✔ {vid} modifiée : « {corps['title']} », {len(tags)} tags", flush=True)
    res = {"id": vid, "avant": avant, "apres": {"titre": corps["title"], "tags": tags}}
    if p.get("miniature_url"):  # nouvelle miniature (JPG < 2 Mo, 1280×720)
        try:
            img = telecharger(p["miniature_url"], dossier or tempfile.mkdtemp(), "miniature.jpg")
            yt.thumbnails().set(videoId=vid, media_body=MediaFileUpload(img, mimetype="image/jpeg")).execute()
            res["miniature"] = "ok"
        except HttpError as e:
            res["miniature"] = f"refusée : {e.reason if hasattr(e, 'reason') else e}"
        print(f"  miniature : {res['miniature']}", flush=True)
    return res


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
                       playlist(p, yt) if action == "playlist" else
                       section_playlist(p, yt) if action == "section_playlist" else
                       lire_chaine(p, yt) if action == "lire_chaine" else
                       chaine(p, yt) if action == "chaine" else
                       repondre(p, yt) if action == "repondre" else
                       chercher(p, yt) if action == "chercher" else
                       commenter(p, yt) if action == "commenter" else
                       reprogrammer(p, yt) if action == "reprogrammer" else
                       garder_prive(p, yt) if action == "garder_prive" else
                       modifier(p, yt, d) if action == "modifier" else publier(p, yt, d))
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
