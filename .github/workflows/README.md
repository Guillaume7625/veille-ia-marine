# 🚢 Veille IA – Marine (Full Web, Hardened)

**0 € – 100 % cloud – MAJ quotidienne – UI Tailwind – Export CSV – Aucune télémétrie.**

## Déploiement rapide
1. Créez un repo public `veille-ia-marine` sur GitHub.
2. Uploadez ces fichiers (respecter l’arborescence).
3. **Settings → Pages** : Source = `gh-pages` / **root**.
4. **Actions** : attendre 2–5 min.  
5. URL : `https://<USER>.github.io/veille-ia-marine/`

## Personnalisation
- Flux : `RSS_FEEDS` dans `veille_ia.py`
- Fenêtre : `DAYS_WINDOW` (env ou code, défaut 7)
- Scoring : `KEYWORDS_WEIGHTS`
- Endpoint JSON optionnel : secrets `GEN_ENDPOINT`, `GEN_TOKEN`

## Sécurité / conformité
- Aucune inclusion de scripts de tracking ni badges tiers.
- Secrets **jamais** en clair (passés via GitHub Actions).
- Contenu : sources ouvertes uniquement.

## Test local
```bash
pip install feedparser pandas
python veille_ia.py
open docs/index.html  # macOS (ou xdg-open sous Linux / start sous Windows)
```

## Limitations
- Dédup = titre+lien (embeddings possible en évolution)
- Dépendance à la qualité des flux RSS
- Scoring heuristique à calibrer
