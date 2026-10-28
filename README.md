# aduna_insta

Publication automatique des photos du chantier d'Aduna sur Instagram ([@aduna.sea](https://www.instagram.com/aduna.sea/)).

## Fonctionnement

1. **Album** : les photos sont déposées dans l'album partagé iCloud d'Aduna, par plusieurs personnes.
2. **Chaque matin**, Claude lit les nouvelles photos (`scan`), les regroupe en posts ou en carrousels, et écrit une légende d'un ou deux mots par photo : un objet, une lumière ou une couleur, séparés par des points.
3. **Validation** : Claude envoie la proposition à Theophile. Rien n'est publié sans son « ok ».
4. **Publication** aux bons créneaux (12 h 30, 18 h 45, 20 h 15, heure de Paris), avec 3 posts par jour au maximum.

## Le code

- `pipeline/aduna_insta.py` : `scan` (lecture de l'album iCloud via CloudKit), `prepare` (bandes noires si le ratio sort de 4:5 à 1,91:1, jamais de recadrage), `publish` (hébergement temporaire et publication via l'API Instagram), `quota` (cadence du jour).
- `pipeline/batch_publish.py` : publie une série de plans validés, avec une pause entre chaque post, et reprend là où il s'est arrêté.
- `.github/workflows/cleanup-tmp-branches.yml` : supprime toutes les heures les branches `tmp-*` qui ont servi à héberger les photos le temps qu'Instagram les récupère.

Le token Instagram n'apparaît nulle part dans le code : il est injecté par le proxy de l'environnement Claude.
