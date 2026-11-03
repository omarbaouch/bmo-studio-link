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


def mmss(sec):
    return f"{int(sec // 60)}:{int(sec % 60):02d}"


def analyse_retention(courbe, duree):
    """courbe = [(part de la vidéo, part des spectateurs, rétention relative)] → points utiles pour le studio."""
    if not courbe:
        return {}
    xs = [c[0] for c in courbe]
    def a(t):  # spectateurs restants au temps t (s), interpolé
        r = t / max(duree, 1)
        for (x0, y0, _), (x1, y1, _) in zip(courbe, courbe[1:]):
            if x0 <= r <= x1:
                return y0 + (y1 - y0) * (r - x0) / max(x1 - x0, 1e-9)
        return courbe[-1][1] if r > xs[-1] else courbe[0][1]
    creux = []
    for i in range(len(courbe) - 3):
        x0, y0, _ = courbe[i]
        x1, y1, _ = courbe[i + 3]
        if 0.03 <= x0 <= 0.93 and y0 - y1 > 0:
            creux.append((y0 - y1, x0))
    creux.sort(reverse=True)
    choisis = []
    for chute, x in creux:
        if all(abs(x - c) > 0.06 for _, c in choisis):
            choisis.append((chute, x))
        if len(choisis) == 4:
            break
    pics = [(c[0], c[1]) for i, c in enumerate(courbe[1:-1], 1) if c[1] > courbe[i - 1][1] + 0.02 and c[1] > courbe[i + 1][1]]
    rel = [c[2] for c in courbe if c[2] is not None]
    return {
        "a_30s": round(a(30), 3), "a_mi_parcours": round(a(duree / 2), 3), "a_la_fin": round(courbe[-1][1], 3),
        "creux": [{"vers": mmss(x * duree), "perte_points": round(ch * 100, 1)} for ch, x in sorted(choisis, key=lambda z: z[1])],
        "pics": [{"vers": mmss(x * duree)} for x, _ in pics[:4]],
        "retention_relative_moyenne": round(sum(rel) / len(rel), 2) if rel else None,
        "courbe": [[round(x, 2), round(y, 3)] for x, y, _ in courbe][::5],
    }


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
                                               dimensions="elapsedVideoTimeRatio",
                                               metrics="audienceWatchRatio,relativeRetentionPerformance",
                                               filters=f"video=={v['id']};audienceType==ORGANIC").execute()
                        courbe = [(float(x[0]), float(x[1]), float(x[2])) for x in r.get("rows", [])]
                        v["retention"] = analyse_retention(courbe, v["duree_s"])
                    except HttpError as e:
                        v["retention"] = f"indisponible ({e.status_code})"
        except HttpError as e:
            rapport["analytics_erreur"] = str(e)
        try:
            rep = reporting(c)
            rapport["reporting"] = {"rapports_disponibles": rep["rapports_disponibles"]}
            for v in videos:
                if v["id"] in rep["par_video"]:
                    v["miniature_7j"] = rep["par_video"][v["id"]]
        except Exception as e:
            rapport["reporting_erreur"] = str(e)[:300]
    else:
        rapport["analytics_erreur"] = "le jeton n'a pas l'autorisation yt-analytics.readonly : refaire le jeton avec ce scope"
    return rapport


RAPPORTS = {"channel_reach_basic_a1": "Impressions et taux de clic des miniatures", "channel_basic_a2": "Activité des vidéos"}


def reporting(c):
    """API YouTube Reporting : crée les tâches de rapports (une fois) et lit les impressions / taux de clic (7 derniers jours)."""
    import csv, io
    from google.auth.transport.requests import AuthorizedSession
    yr = build("youtubereporting", "v1", credentials=c, cache_discovery=False)
    jobs = {j["reportTypeId"]: j["id"] for j in yr.jobs().list().execute().get("jobs", [])}
    for t, nom in RAPPORTS.items():
        if t not in jobs:
            jobs[t] = yr.jobs().create(body={"reportTypeId": t, "name": nom}).execute()["id"]
    sess = AuthorizedSession(c)
    debut = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=8)).strftime("%Y-%m-%dT00:00:00Z")
    reps = yr.jobs().reports().list(jobId=jobs["channel_reach_basic_a1"], startTimeAtOrAfter=debut).execute().get("reports", [])
    par = {}
    for rep in reps:
        txt = sess.get(rep["downloadUrl"], timeout=60).text
        for row in csv.DictReader(io.StringIO(txt)):
            v = par.setdefault(row["video_id"], [0.0, 0.0])
            imp = float(row.get("video_thumbnail_impressions") or 0)
            v[0] += imp
            v[1] += imp * float(row.get("video_thumbnail_impressions_ctr") or 0)
    return {"rapports_disponibles": len(reps),
            "par_video": {k: {"impressions": int(a), "ctr": round(b / a * 100, 2) if a else None} for k, (a, b) in par.items()}}


def markdown(r):
    l = [f"# {r['chaine']} — {r['date'][:16].replace('T', ' ')} UTC", "",
         f"Abonnés : {r['abonnes']} · vues totales : {r['vues_totales']}", ""]
    if r.get("analytics_erreur"):
        l += [f"> Analytics : {r['analytics_erreur']}", ""]
    if r.get("sources_trafic_28j"):
        l += ["Sources de trafic (28 j) : " + ", ".join(f"{k} {v}" for k, v in r["sources_trafic_28j"].items()), ""]
    if r.get("reporting", {}).get("rapports_disponibles") == 0:
        l += ["> Impressions et taux de clic : rapports YouTube en préparation (premières données sous 48 h).", ""]
    l += ["| Publiée | Format | Titre | Vues | Durée moy. | % regardé | Abonnés + | Impressions 7 j | Clic |", "|---|---|---|---|---|---|---|---|---|"]
    for v in sorted(r["videos"], key=lambda x: x["publie"], reverse=True):
        a = v.get("analytics_28j") or {}
        l.append(f"| {v['publie'][:10]} | {v['format']} | {v['titre'][:60]} | {v['vues']} | "
                 f"{int(a.get('averageViewDuration', 0))} s | {round(a.get('averageViewPercentage', 0))} % | {a.get('subscribersGained', '')} | "
                 f"{(v.get('miniature_7j') or {}).get('impressions', '')} | {(v.get('miniature_7j') or {}).get('ctr') or ''}{' %' if (v.get('miniature_7j') or {}).get('ctr') else ''} |")
    shorts = [v for v in r["videos"] if v["format"] == "short"]
    if shorts:
        moy = sum(v["vues"] for v in shorts) / len(shorts)
        l += ["", f"Shorts : moyenne {round(moy)} vues. Au-dessus de la moyenne (formats à refaire) : " +
              "; ".join(f"« {v['titre'][:50]} » ({v['vues']})" for v in sorted(shorts, key=lambda z: -z["vues"]) if v["vues"] > moy)]
    for v in r["videos"]:
        rt = v.get("retention")
        if isinstance(rt, dict) and rt:
            rel = rt.get("retention_relative_moyenne")
            l += ["", f"### Rétention « {v['titre'][:60]} » ({mmss(v['duree_s'])})",
                  f"- Encore là à 30 s : {round(rt['a_30s'] * 100)} % · à mi-parcours : {round(rt['a_mi_parcours'] * 100)} % · à la fin : {round(rt['a_la_fin'] * 100)} %",
                  f"- Rétention relative (vs vidéos YouTube de même durée) : {rel} " + ("(au-dessus de la moyenne)" if rel and rel > 0.5 else "(en dessous de la moyenne)" if rel else ""),
                  "- Creux (les gens partent) : " + (", ".join(f"vers {c['vers']} (−{c['perte_points']} pts)" for c in rt["creux"]) or "aucun net"),
                  "- Pics (relecture) : " + (", ".join(p["vers"] for p in rt["pics"]) or "aucun"),
                  "- À faire : ouvrir la vidéo à chaque creux, noter la phrase et le plan, écrire la cause dans 00_lecons_analytics.md."]
    return "\n".join(l) + "\n"


def verifier_jeton(nom, tok):
    """Dit si le jeton est permanent ou s'il expire (appli en mode Test au moment de sa création), sans l'afficher."""
    r = requests.post("https://oauth2.googleapis.com/token", data={"client_id": CID, "client_secret": CSEC,
                      "refresh_token": tok, "grant_type": "refresh_token"}, timeout=30).json()
    if "error" in r:
        return f"jeton refusé ({r['error']})"
    if "refresh_token_expires_in" in r:
        return f"jeton TEMPORAIRE : expire dans {round(int(r['refresh_token_expires_in']) / 86400, 1)} jours (à refaire)"
    return "jeton permanent"


def main():
    for k, tok in S.items():
        if k.startswith("YT_REFRESH_TOKEN_"):
            print(f"{k[len('YT_REFRESH_TOKEN_'):]} : {verifier_jeton(k, tok)}", flush=True)
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
