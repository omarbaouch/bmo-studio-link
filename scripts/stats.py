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

import shutil
from zoneinfo import ZoneInfo

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
            # Vues « engagées » (le spectateur est resté après les premières secondes) : engagées / vues ≈ « ont continué de
            # regarder » de YouTube Studio. C'est le chiffre qui décide si un Short est montré à plus de monde.
            try:
                r = ya.reports().query(ids="channel==MINE", startDate=debut, endDate=fin, dimensions="video", maxResults=200,
                                       metrics="views,engagedViews", sort="-views").execute()
                for row in r.get("rows", []):
                    for v in videos:
                        if v["id"] == row[0] and row[1]:
                            v["restes"] = {"vues": int(row[1]), "engagees": int(row[2]), "pct": round(100 * row[2] / row[1], 1)}
            except HttpError as e:
                rapport["engagees_erreur"] = f"indisponible ({e.status_code})"
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
            # D'où viennent les vues de chaque vidéo longue (pour savoir si les liens des Shorts servent)
            for v in videos:
                if v["format"] == "longue":
                    try:
                        r = ya.reports().query(ids="channel==MINE", startDate="2020-01-01", endDate=fin,
                                               dimensions="insightTrafficSourceType", metrics="views,estimatedMinutesWatched",
                                               filters=f"video=={v['id']}", sort="-views").execute()
                        v["sources"] = {row[0]: int(row[1]) for row in r.get("rows", [])}
                    except HttpError as e:
                        v["sources"] = f"indisponible ({e.status_code})"
                    try:  # détail des vues venues des Shorts : quels Shorts envoient vers cette vidéo
                        r = ya.reports().query(ids="channel==MINE", startDate="2020-01-01", endDate=fin,
                                               dimensions="insightTrafficSourceDetail", metrics="views",
                                               filters=f"video=={v['id']};insightTrafficSourceType==SHORTS_CONTENT_LINKS",
                                               sort="-views", maxResults=25).execute()
                        v["depuis_shorts"] = {row[0]: int(row[1]) for row in r.get("rows", [])}
                    except HttpError as e:
                        v["depuis_shorts"] = f"indisponible ({e.status_code})"
        except HttpError as e:
            rapport["analytics_erreur"] = str(e)
        try:
            rapport["monetisation"] = monetisation(ya)
        except HttpError as e:
            rapport["monetisation"] = {"erreur": f"indisponible ({e.status_code})"}
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


RAPPORTS = {"channel_reach_basic_a1": "Impressions et taux de clic des miniatures", "channel_basic_a3": "Activité des vidéos"}


def reporting(c):
    """API YouTube Reporting : crée les tâches de rapports (une fois) et lit les impressions / taux de clic (7 derniers jours)."""
    import csv, io
    from google.auth.transport.requests import AuthorizedSession
    yr = build("youtubereporting", "v1", credentials=c, cache_discovery=False)
    etape = "reportTypes.list"
    try:
        types = [t["id"] for t in yr.reportTypes().list().execute().get("reportTypes", [])]
        print("  Reporting : types disponibles =", ", ".join(types) or "aucun", flush=True)
        etape = "jobs.list"
        jobs = {j["reportTypeId"]: j["id"] for j in yr.jobs().list().execute().get("jobs", [])}
    except HttpError as e:
        raise RuntimeError(f"{etape} : {e}") from e
    manquants = [t for t in RAPPORTS if t not in types]
    if manquants:
        raise RuntimeError(f"types absents pour cette chaîne : {manquants} ; disponibles : {types}")
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


PARIS = ZoneInfo("Europe/Paris")
SEUILS = {"abonnes": 1000, "heures_longues_365j": 4000, "vues_shorts_90j": 10_000_000}
MAX_SHORTS_PAR_JOUR = 3


def monetisation(ya):
    """Progression vers le Programme Partenaire : vues Shorts sur 90 j et heures de vidéos longues sur 365 j."""
    fin = dt.date.today()
    def par_type(jours, metrique):
        r = ya.reports().query(ids="channel==MINE", startDate=(fin - dt.timedelta(days=jours)).isoformat(),
                               endDate=fin.isoformat(), dimensions="creatorContentType", metrics=metrique).execute()
        return {str(row[0]).upper(): float(row[1]) for row in r.get("rows", [])}
    shorts = par_type(90, "views")
    longues = par_type(365, "estimatedMinutesWatched")
    return {"vues_shorts_90j": int(sum(v for k, v in shorts.items() if "SHORT" in k)),
            "heures_longues_365j": round(sum(v for k, v in longues.items() if "VIDEO_ON_DEMAND" in k or "VIDEOONDEMAND" in k) / 60, 1)}


def norm_titre(t):
    import re
    t = re.sub(r"#\w+", "", t.lower())
    return re.sub(r"[^\w€%]+", " ", t).strip()


def pilotage(r, dossier):
    """Évolution (historique.csv), doublons, rythme des Shorts et alertes. Ajoute r['pilotage']."""
    import csv
    auj = dt.datetime.now(PARIS).date()
    chemin = os.path.join(dossier, "historique.csv")
    hist = []
    if os.path.exists(chemin):
        hist = [row for row in csv.DictReader(open(chemin, encoding="utf-8")) if row.get("date") != auj.isoformat()]
    hist.append({"date": auj.isoformat(), "abonnes": r["abonnes"], "vues_totales": r["vues_totales"]})
    with open(chemin, "w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=["date", "abonnes", "vues_totales"])
        w.writeheader()
        w.writerows(hist)

    def ecart(jours):
        cible = (auj - dt.timedelta(days=jours)).isoformat()
        avant = [h for h in hist if h["date"] <= cible]
        if not avant:
            return None
        h = avant[-1]
        return {"depuis": h["date"], "abonnes": r["abonnes"] - int(h["abonnes"]), "vues": r["vues_totales"] - int(h["vues_totales"])}

    publiques = [v for v in r["videos"] if v["statut"] == "public"]
    groupes = {}
    for v in publiques:
        groupes.setdefault((v["format"], norm_titre(v["titre"])), []).append(v)
    doublons = [sorted(g, key=lambda x: -x["vues"]) for g in groupes.values() if len(g) > 1]

    par_jour = {}
    for v in publiques:
        if v["format"] != "short":
            continue
        j = dt.datetime.fromisoformat(v["publie"].replace("Z", "+00:00")).astimezone(PARIS).date()
        if (auj - j).days < 7:
            par_jour[j.isoformat()] = par_jour.get(j.isoformat(), 0) + 1

    alertes = []
    if doublons:
        alertes.append(f"{len(doublons)} Short(s) en double : à passer en privé (le moins vu de chaque paire, liste plus bas)")
    trop = {j: n for j, n in sorted(par_jour.items()) if n > MAX_SHORTS_PAR_JOUR}
    if trop:
        alertes.append("Trop de Shorts le même jour (max conseillé " + str(MAX_SHORTS_PAR_JOUR) + ") : " +
                       ", ".join(f"{j} → {n}" for j, n in trop.items()))
    sem = ecart(7)
    if sem and sem["abonnes"] <= 0:
        alertes.append("Aucun abonné gagné en 7 jours")
    r["pilotage"] = {"evolution_24h": ecart(1), "evolution_7j": sem, "shorts_par_jour_7j": dict(sorted(par_jour.items())),
                     "doublons": [[{"id": v["id"], "titre": v["titre"], "vues": v["vues"]} for v in g] for g in doublons],
                     "alertes": alertes}


def barre(val, seuil):
    p = min(val / seuil, 1)
    return "█" * round(p * 20) + "░" * (20 - round(p * 20)) + f" {p * 100:.1f} %"


def section_pilotage(r):
    p = r.get("pilotage") or {}
    l = ["## Pilotage de la chaîne", ""]
    for a in p.get("alertes", []):
        l.append(f"- ⚠️ {a}")
    if not p.get("alertes"):
        l.append("- ✅ Aucune alerte")
    l.append("")
    for cle, nom in (("evolution_24h", "24 h"), ("evolution_7j", "7 jours")):
        e = p.get(cle)
        l.append(f"- Sur {nom} : " + (f"{e['abonnes']:+d} abonnés, {e['vues']:+d} vues (depuis le {e['depuis']})" if e
                                      else "pas encore assez d'historique"))
    if p.get("shorts_par_jour_7j"):
        l.append("- Shorts publiés par jour : " + ", ".join(f"{j[5:]} {n}" for j, n in p["shorts_par_jour_7j"].items()))
    m = r.get("monetisation") or {}
    l += ["", "**Seuils de monétisation** (il faut les abonnés ET l'un des deux autres)", "",
          f"- Abonnés : {r['abonnes']} / 1 000 — {barre(r['abonnes'], SEUILS['abonnes'])}"]
    if "erreur" in m or not m:
        l.append(f"- Heures et vues Shorts : {m.get('erreur', 'Analytics indisponible')}")
    else:
        l += [f"- Heures de vidéos longues (365 j) : {m['heures_longues_365j']} / 4 000 — {barre(m['heures_longues_365j'], SEUILS['heures_longues_365j'])}",
              f"- Vues Shorts (90 j) : {m['vues_shorts_90j']:,} / 10 000 000 — {barre(m['vues_shorts_90j'], SEUILS['vues_shorts_90j'])}".replace(",", " "),
              f"- ⚠️ À partir du 01/02/2027 (nouvelles candidatures) : {m['heures_longues_365j']} / 8 000 h — {barre(m['heures_longues_365j'], 8000)} "
              f"ou {m['vues_shorts_90j']:,} / 20 000 000 vues Shorts".replace(",", " ")]
        e = p.get("evolution_7j")
        if e and e["abonnes"] > 0:
            reste = SEUILS["abonnes"] - r["abonnes"]
            l.append(f"- Au rythme des 7 derniers jours : 1 000 abonnés dans ~{round(reste / e['abonnes'] * 7)} jours")
    if p.get("doublons"):
        l += ["", "**Doublons** (garder le premier, passer les autres en privé) :"]
        for g in p["doublons"]:
            l.append("- " + " · ".join(f"{v['id']} ({v['vues']} vues)" for v in g) + f" — « {g[0]['titre'][:50]} »")
    return l + [""]


SOURCES_FR = {"SUBSCRIBER": "ACCUEIL_NAVIGATION(SUBSCRIBER)"}


def source_fr(k):
    """Nom lisible d'une source de trafic. Attention : SUBSCRIBER dans l'API = « Fonctionnalités de navigation »
    (accueil, fil d'abonnements, historique), pas « les abonnés »."""
    return SOURCES_FR.get(k, k)


def markdown(r):
    l = [f"# {r['chaine']} — {r['date'][:16].replace('T', ' ')} UTC", "",
         f"Abonnés : {r['abonnes']} · vues totales : {r['vues_totales']}", ""]
    l += section_pilotage(r)
    if r.get("analytics_erreur"):
        l += [f"> Analytics : {r['analytics_erreur']}", ""]
    if r.get("sources_trafic_28j"):
        l += ["Sources de trafic (28 j) : " + ", ".join(f"{source_fr(k)} {v}" for k, v in r["sources_trafic_28j"].items()), ""]
    if r.get("reporting", {}).get("rapports_disponibles") == 0:
        l += ["> Impressions et taux de clic : rapports YouTube en préparation (premières données sous 48 h).", ""]
    l += ["| Publiée | Format | Titre | Vues | Restés | Durée moy. | % regardé | Abonnés + | Impressions 7 j | Clic |", "|---|---|---|---|---|---|---|---|---|---|"]
    for v in sorted(r["videos"], key=lambda x: x["publie"], reverse=True):
        a = v.get("analytics_28j") or {}
        restes = f"{v['restes']['pct']} %" if v.get("restes") and v["format"] == "short" else ""
        l.append(f"| {v['publie'][:10]} | {v['format']} | {v['titre'][:60]} | {v['vues']} | {restes} | "
                 f"{int(a.get('averageViewDuration', 0))} s | {round(a.get('averageViewPercentage', 0))} % | {a.get('subscribersGained', '')} | "
                 f"{(v.get('miniature_7j') or {}).get('impressions', '')} | {(v.get('miniature_7j') or {}).get('ctr') or ''}{' %' if (v.get('miniature_7j') or {}).get('ctr') else ''} |")
    shorts = [v for v in r["videos"] if v["format"] == "short"]
    mesures = [v["restes"] for v in shorts if v.get("restes") and v["restes"]["vues"] >= 50]
    if mesures:
        tot_v, tot_e = sum(m["vues"] for m in mesures), sum(m["engagees"] for m in mesures)
        l += ["", f"« Restés » = vues engagées / vues (qui n'a pas balayé le Short). Chaîne : {round(100 * tot_e / tot_v, 1)} % "
              f"sur {len(mesures)} Shorts de 50 vues et plus. Repère : les Shorts qui décollent sont à 70 % et plus."]
    if r.get("engagees_erreur"):
        l += ["", f"> Vues engagées : {r['engagees_erreur']}"]
    if shorts:
        moy = sum(v["vues"] for v in shorts) / len(shorts)
        l += ["", f"Shorts : moyenne {round(moy)} vues. Au-dessus de la moyenne (formats à refaire) : " +
              "; ".join(f"« {v['titre'][:50]} » ({v['vues']})" for v in sorted(shorts, key=lambda z: -z["vues"]) if v["vues"] > moy)]
    longues = [v for v in r["videos"] if v["format"] == "longue" and isinstance(v.get("sources"), dict)]
    if longues:
        l += ["", "### D'où viennent les vues des vidéos longues (depuis la sortie)"]
        for v in longues:
            l.append(f"- « {v['titre'][:55]} » : " + (", ".join(f"{source_fr(k)} {n}" for k, n in v["sources"].items()) or "aucune vue"))
            if isinstance(v.get("depuis_shorts"), dict) and v["depuis_shorts"]:
                l.append("  - Shorts qui envoient vers elle : " + ", ".join(f"{k} ({n})" for k, n in v["depuis_shorts"].items()))
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
        try:
            pilotage(r, f"stats/{nom}")
        except Exception as e:
            r["pilotage"] = {"alertes": [f"pilotage indisponible : {e}"]}
        json.dump(r, open(f"stats/{nom}/dernier.json", "w", encoding="utf-8"), ensure_ascii=False, indent=1)
        open(f"stats/{nom}/dernier.md", "w", encoding="utf-8").write(markdown(r))
        auj = dt.datetime.now(PARIS)
        if auj.weekday() == 0:  # chaque lundi : copie du rapport dans stats/<CHAINE>/semaines/
            os.makedirs(f"stats/{nom}/semaines", exist_ok=True)
            an, sem, _ = auj.isocalendar()
            shutil.copy(f"stats/{nom}/dernier.md", f"stats/{nom}/semaines/{an}-S{sem:02d}.md")
        print(markdown(r))


if __name__ == "__main__":
    main()
