#!/usr/bin/env python3
"""Statistiques des chaînes (API YouTube Data + YouTube Analytics), écrites dans stats/<CHAINE>/.

Pour chaque chaîne qui a un secret YT_REFRESH_TOKEN_<CHAINE> :
  - dernier.json / dernier.md : vues, likes, commentaires de chaque vidéo (Data API) ;
  - si le jeton a l'autorisation yt-analytics.readonly : par vidéo, minutes regardées, durée moyenne,
    % moyen regardé, abonnés gagnés (28 derniers jours), sources de trafic, et la courbe de rétention
    (part des spectateurs encore là à chaque 5 % de la vidéo) pour les vidéos longues.
Le studio lit stats/<CHAINE>/dernier.md avant d'écrire la vidéo suivante.
"""
import datetime as dt
import json
import os

import requests
from google.oauth2.credentials import Credentials
from google.auth.transport.requests import Request
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

S = {k: (v.strip().strip('"').strip() if isinstance(v, str) else v) for k, v in json.loads(os.environ.get("SECRETS_JSON", "{}")).items()}
CID, CSEC = S.get("YT_CLIENT_ID"), S.get("YT_CLIENT_SECRET")


def creds(tok):
    c = Credentials(None, refresh_token=tok, client_id=CID, client_secret=CSEC, token_uri="https://oauth2.googleapis.com/token")
    c.refresh(Request())
    info = requests.get("https://oauth2.googleapis.com/tokeninfo", params={"access_token": c.token}, timeout=30).json()
    return c, set(info.get("scope", "").split())


def duree_s(iso):
    import re
    m = re.match(r"PT(?:(\d+)H)?(?:(\d+)M)?(?:(\d+)S)?", iso or "")
    return sum(int(x or 0) * k for x, k in zip(m.groups(), (3600, 60, 1))) if m else 0


def chaine(nom, tok):
    c, scopes = creds(tok)
    yt = build("youtube", "v3", credentials=c, cache_discovery=False)
    ch = yt.channels().list(part="id,snippet,statistics,contentDetails", mine=True).execute()["items"][0]
    up = ch["contentDetails"]["relatedPlaylists"]["uploads"]
    ids, page = [], None
    while True:
        r = yt.playlistItems().list(part="contentDetails", playlistId=up, maxResults=50, pageToken=page).execute()
        ids += [x["contentDetails"]["videoId"] for x in r["items"]]
        page = r.get("nextPageToken")
        if not page or len(ids) >= 200:
            break
    videos = []
    for i in range(0, len(ids), 50):
        r = yt.videos().list(part="snippet,statistics,contentDetails,status", id=",".join(ids[i:i + 50])).execute()
        for v in r["items"]:
            d = duree_s(v["contentDetails"]["duration"])
            videos.append({"id": v["id"], "titre": v["snippet"]["title"], "publie": v["snippet"]["publishedAt"],
                           "duree_s": d, "format": "short" if d <= 180 else "longue", "statut": v["status"]["privacyStatus"],
                           "vues": int(v["statistics"].get("viewCount", 0)), "likes": int(v["statistics"].get("likeCount", 0)),
                           "commentaires": int(v["statistics"].get("commentCount", 0))})
    rapport = {"chaine": ch["snippet"]["title"], "date": dt.datetime.now(dt.timezone.utc).isoformat(),
               "abonnes": int(ch["statistics"].get("subscriberCount", 0)), "vues_totales": int(ch["statistics"].get("viewCount", 0)),
               "autorisations": sorted(s.rsplit("/", 1)[-1] for s in scopes), "videos": videos}
    if any(s.endswith("yt-analytics.readonly") for s in scopes):
        ya = build("youtubeAnalytics", "v2", credentials=c, cache_discovery=False)
        fin = dt.date.today().isoformat()
        debut = (dt.date.today() - dt.timedelta(days=28)).isoformat()
        try:
            r = ya.reports().query(ids="channel==MINE", startDate=debut, endDate=fin, dimensions="video", maxResults=200,
                                   metrics="views,estimatedMinutesWatched,averageViewDuration,averageViewPercentage,subscribersGained",
                                   sort="-views").execute()
            par = {row[0]: dict(zip([h["name"] for h in r["columnHeaders"]][1:], row[1:])) for row in r.get("rows", [])}
            for v in videos:
                v["analytics_28j"] = par.get(v["id"])
            r = ya.reports().query(ids="channel==MINE", startDate=debut, endDate=fin, dimensions="insightTrafficSourceType",
                                   metrics="views", sort="-views").execute()
            rapport["sources_trafic_28j"] = {row[0]: row[1] for row in r.get("rows", [])}
            for v in videos:
                if v["format"] == "longue" and v["vues"] >= 20:
                    try:
                        r = ya.reports().query(ids="channel==MINE", startDate="2020-01-01", endDate=fin,
                                               dimensions="elapsedVideoTimeRatio", metrics="audienceWatchRatio",
                                               filters=f"video=={v['id']};audienceType==ORGANIC").execute()
                        v["retention"] = [[round(x[0], 2), round(x[1], 3)] for x in r.get("rows", [])][::5]
                    except HttpError as e:
                        v["retention"] = f"indisponible ({e.status_code})"
        except HttpError as e:
            rapport["analytics_erreur"] = str(e)
    else:
        rapport["analytics_erreur"] = "le jeton n'a pas l'autorisation yt-analytics.readonly : refaire le jeton avec ce scope"
    return rapport


def markdown(r):
    l = [f"# {r['chaine']} — {r['date'][:16].replace('T', ' ')} UTC", "",
         f"Abonnés : {r['abonnes']} · vues totales : {r['vues_totales']}", ""]
    if r.get("analytics_erreur"):
        l += [f"> Analytics : {r['analytics_erreur']}", ""]
    if r.get("sources_trafic_28j"):
        l += ["Sources de trafic (28 j) : " + ", ".join(f"{k} {v}" for k, v in r["sources_trafic_28j"].items()), ""]
    l += ["| Publiée | Format | Titre | Vues | Durée moy. | % regardé | Abonnés + |", "|---|---|---|---|---|---|---|"]
    for v in sorted(r["videos"], key=lambda x: x["publie"], reverse=True):
        a = v.get("analytics_28j") or {}
        l.append(f"| {v['publie'][:10]} | {v['format']} | {v['titre'][:60]} | {v['vues']} | "
                 f"{int(a.get('averageViewDuration', 0))} s | {round(a.get('averageViewPercentage', 0))} % | {a.get('subscribersGained', '')} |")
    for v in r["videos"]:
        if isinstance(v.get("retention"), list) and v["retention"]:
            l += ["", f"Rétention « {v['titre'][:50]} » (part de la vidéo → spectateurs restants) : " +
                  " · ".join(f"{int(x * 100)} % → {round(y * 100)} %" for x, y in v["retention"])]
    return "\n".join(l) + "\n"


def main():
    for k, tok in S.items():
        if not k.startswith("YT_REFRESH_TOKEN_"):
            continue
        nom = k[len("YT_REFRESH_TOKEN_"):]
        try:
            r = chaine(nom, tok)
        except Exception as e:
            print(f"{nom} : échec {e}")
            continue
        os.makedirs(f"stats/{nom}", exist_ok=True)
        json.dump(r, open(f"stats/{nom}/dernier.json", "w", encoding="utf-8"), ensure_ascii=False, indent=1)
        open(f"stats/{nom}/dernier.md", "w", encoding="utf-8").write(markdown(r))
        print(markdown(r))


if __name__ == "__main__":
    main()
