# BMO Studio Link

Dépôt central de publication YouTube d'Omar (appli Google « BMO Studio Link », API YouTube Data v3).
Pages publiques de l'appli : https://bmo-studio-link.vercel.app/

## Publier une vidéo ou un Short depuis n'importe quel projet
Déposer un fichier `publications/<date>-<nom>.json` sur la branche `main` de ce dépôt :

```json
{
  "chaine": "LEVRAIPRIX",
  "video_url": "https://…/video.mp4",
  "titre": "Titre (100 caractères max)",
  "description": "…",
  "tags": ["…"],
  "categorie": 27,
  "miniature_url": "https://…/miniature.jpg",
  "commentaire": "Premier commentaire (facultatif)",
  "publier_le": "2026-11-04T08:00:00+01:00"
}
```
- `chaine` : nom du secret `YT_REFRESH_TOKEN_<CHAINE>` de ce dépôt.
- `video_url` / `miniature_url` : liens publics (Release GitHub publique, raw.githubusercontent.com…).
- `publier_le` absent : mise en ligne immédiate. Une vidéo de 3 min maximum en format vertical devient un Short.
- Catégories : 27 Éducation, 28 Science et technologie, 22 Personnes et blogs, 24 Divertissement.

Le workflow « Publier sur YouTube » envoie la vidéo, met la miniature et le commentaire, puis range le fichier
dans `publications/fait/` avec le lien YouTube.

## Secrets (Settings → Secrets and variables → Actions)
- `YT_CLIENT_ID`, `YT_CLIENT_SECRET` : identifiant OAuth de l'appli (Google Cloud, projet « Youtube Studio »).
- `YT_REFRESH_TOKEN_<CHAINE>` : un par chaîne (OAuth Playground, compte Google de la chaîne).

## Limite tant que l'audit Google n'est pas validé
YouTube garde les vidéos envoyées en **privé** : il faut les passer en public dans YouTube Studio.
