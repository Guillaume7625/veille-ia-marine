#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Veille IA – Militaire / Marine (Full Web, GitHub Actions + Pages)
- Fenêtre glissante paramétrable (DAYS_WINDOW, défaut 30 jours)
- Filtrage obligatoire IA + pertinence militaire (combat/ISR, soutien, C2, cyber, spatial, marine…)
- Déduplication (hash + similarité)
- Résumés FR courts (avec traduction offline Argos si dispo)
- UI Tailwind + filtres + export CSV (corrigé sans template string JS)
- Déploiement vers GitHub Pages (docs/index.html)
Aucune télémétrie.
"""

import os
import re
import json
import html
import time
import math
import unicodedata
import calendar
from hashlib import md5
from datetime import datetime, timezone, timedelta
from difflib import SequenceMatcher

import feedparser
import pandas as pd

# =========================================================
# Configuration
# =========================================================

DAYS_WINDOW = int(os.getenv("DAYS_WINDOW", "30"))  # période d’analyse glissante
OUT_DIR = "docs"
OUT_FILE = "index.html"
MAX_SUMMARY_FR_CHARS = 400  # longueur résumés FR
OFFLINE_TRANSLATION = os.getenv("OFFLINE_TRANSLATION", "true").lower() in {"1", "true", "yes"}
ARGOS_FROM_LANG = "en"
ARGOS_TO_LANG = "fr"

# Filtrage
REQUIRE_AI = True          # un article doit parler d’IA (titre+résumé)
MIL_REQUIRED = True        # et être pertinent côté défense/marine/cyber etc.
BOOST_DEFENSE = 2          # bonus si militaire pertinent

# Fichiers de cache (facultatif)
TRANSLATION_CACHE_FILE = "translation_cache.json"

# =========================================================
# Sources RSS
# =========================================================

RSS_FEEDS = [
    # FR / Tech
    "https://www.numerama.com/feed/",
    "https://www.actuia.com/feed/",
    # EN / AI & Defense
    "https://venturebeat.com/category/ai/feed/",
    "https://www.c4isrnet.com/arc/outboundfeeds/rss/",
    "https://breakingdefense.com/feed/",
    "https://www.naval-technology.com/feed/",
]

# =========================================================
# Vocabulaires
# =========================================================

AI_HINTS = {
    "ai", "artificial intelligence", "intelligence artificielle",
    "machine learning", "deep learning", "gpt", "genai",
    "neural", "modèle", "model", "inference", "agent",
    "multi-agent", "computer vision", "nlp", "llm", "rlhf",
}

# Catégories militaires (tags sémantiques)
MIL_CATEGORIES = {
    "Marine": {"marine", "naval", "navy", "frégate", "sous-marin", "aéronaval"},
    "Cyber": {"cyber", "cybersecurity", "cybersécurité", "ransomware", "soc", "cisa"},
    "Combat/ISR": {"drone", "uav", "ew", "guerre électronique", "isr", "reconnaissance",
                   "missile", "manpads", "sam", "kill chain", "targeting"},
    "Soutien/Logistique": {"maintenance", "logistics", "supply", "mco", "additive manufacturing"},
    "C2/Commandement": {"c2", "command", "datalink", "interoperability", "joint", "multi-domain"},
    "Spatial": {"satellite", "space force", "starlink", "constellation"},
}

# Pondérations (scoring simple)
KEYWORDS_WEIGHTS = {
    "marine": 4, "naval": 4, "navy": 4, "frégate": 4, "sous-marin": 5, "aéronaval": 5,
    "défense": 4, "defense": 4, "otan": 4, "nato": 4, "armée": 3,
    "cyber": 4, "ransomware": 3, "soc": 3,
    "drone": 4, "uav": 4, "ew": 4, "guerre électronique": 5, "isr": 4,
    "satellite": 3, "space force": 3,
    "c2": 3, "commandement": 3, "joint": 2, "multi-domain": 3,
}

# Bruit à écarter (gaming pur, deals, lifestyle…)
NOISE_TERMS = {
    "jeux vidéo", "gameplay", "deal du jour", "meilleur prix", "précommander",
    "série netflix", "prime video", "battlefield", "nintendo", "playstation", "xbox",
    "bons plans", "promo", "cinéma", "tests jeux",
}

# =========================================================
# Utilitaires texte / dates
# =========================================================

def strip_html(text: str) -> str:
    if not text:
        return ""
    text = re.sub(r"<[^>]+>", " ", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text

def normalize(s: str) -> str:
    if not s:
        return ""
    s = s.lower()
    s = unicodedata.normalize("NFKD", s)
    s = "".join(ch for ch in s if not unicodedata.combining(ch))
    return s

def detect_language_simple(text: str) -> str:
    if not text:
        return "unknown"
    t = text.lower()
    fr = sum(1 for m in (" le ", " la ", " les ", " des ", " une ", " un ", " que ", " est ", " sont ", " avec ") if m in t)
    en = sum(1 for m in (" the ", " and ", " are ", " was ", " with ", " from ", " this ", " that ", " which ") if m in t)
    if fr > en:
        return "fr"
    if en > fr:
        return "en"
    return "unknown"

def parse_entry_datetime(entry):
    for attr in ("published_parsed", "updated_parsed"):
        t = getattr(entry, attr, None)
        if t:
            try:
                return datetime.fromtimestamp(calendar.timegm(t), tz=timezone.utc)
            except Exception:
                pass
    for attr in ("published", "updated", "pubDate"):
        s = entry.get(attr, "")
        if s:
            ts = pd.to_datetime(s, utc=True, errors="coerce")
            if pd.notnull(ts):
                return ts.to_pydatetime()
    return None

# =========================================================
# Traduction offline (Argos) + cache
# =========================================================

def load_translation_cache():
    try:
        if os.path.exists(TRANSLATION_CACHE_FILE):
            with open(TRANSLATION_CACHE_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
    except Exception:
        pass
    return {}

def save_translation_cache(cache: dict):
    try:
        with open(TRANSLATION_CACHE_FILE, "w", encoding="utf-8") as f:
            json.dump(cache, f, ensure_ascii=False, indent=2)
    except Exception:
        pass

def _cache_key(text: str) -> str:
    return md5(("en2fr|" + text).encode("utf-8")).hexdigest()

def init_argos_translator():
    if not OFFLINE_TRANSLATION:
        return None
    try:
        import argostranslate.package as argos_package
        import argostranslate.translate as argos_translate
        # Les modèles sont installés par le workflow; ici on récupère la paire
        for lang in argos_translate.get_installed_languages():
            if lang.code == ARGOS_FROM_LANG:
                for to_lang in lang.translations:
                    if to_lang.code == ARGOS_TO_LANG:
                        return argos_translate
    except Exception:
        return None
    return None

_ARGOS = None
_TRANS_CACHE = load_translation_cache()

def translate_offline_en_to_fr(text: str) -> str:
    global _ARGOS, _TRANS_CACHE
    if not text:
        return ""
    if not OFFLINE_TRANSLATION:
        return text
    if detect_language_simple(text) != "en":
        return text

    key = _cache_key(text)
    if key in _TRANS_CACHE:
        return _TRANS_CACHE[key]

    if _ARGOS is None:
        _ARGOS = init_argos_translator()
        if _ARGOS is None:
            return text

    try:
        translated = _ARGOS.translate(text, ARGOS_FROM_LANG, ARGOS_TO_LANG)
        if translated and translated != text:
            _TRANS_CACHE[key] = translated
            save_translation_cache(_TRANS_CACHE)
            return translated
    except Exception:
        pass
    return text

# =========================================================
# Filtrage IA / militaire / bruit
# =========================================================

def has_any(text: str, vocab: set) -> bool:
    t = normalize(text)
    return any(k in t for k in vocab)

def is_ai_related(title: str, summary: str) -> bool:
    t = normalize((title or "") + " " + (summary or ""))
    return has_any(t, AI_HINTS)

def military_relevance(title: str, summary: str):
    """Retourne (score_bonus, liste_categories, ok_bool)."""
    t = normalize((title or "") + " " + (summary or ""))
    found = []
    bonus = 0
    for cat, vocab in MIL_CATEGORIES.items():
        if has_any(t, vocab):
            found.append(cat)
            bonus += 2
    ok = len(found) > 0
    return bonus, found, ok

def is_noise(title: str, summary: str) -> bool:
    t = normalize((title or "") + " " + (summary or ""))
    return has_any(t, NOISE_TERMS)

# =========================================================
# Scoring / dédup
# =========================================================

def score_text(title: str, summary: str):
    t = normalize((title or "") + " " + (summary or ""))
    score = 0
    tags = []
    for k, w in KEYWORDS_WEIGHTS.items():
        nk = normalize(k)
        if nk in t:
            score += w
            tags.append(k)
    seen = set()
    tags = [x for x in tags if not (x in seen or seen.add(x))]
    level = "HIGH" if score >= 9 else "MEDIUM" if score >= 5 else "LOW"
    return score, level, tags

def generate_content_hash(title: str, summary: str) -> str:
    base = normalize(title) + "|" + normalize(summary[:100])
    return md5(base.encode("utf-8")).hexdigest()[:12]

def calculate_similarity(a: str, b: str) -> float:
    if not a or not b:
        return 0.0
    return SequenceMatcher(None, a.lower(), b.lower()).ratio()

def is_duplicate_article(new_entry: dict, existing_entries: list, thr: float = 0.85) -> bool:
    h = generate_content_hash(new_entry["Titre"], new_entry["Résumé"])
    for ex in existing_entries:
        if h == generate_content_hash(ex["Titre"], ex["Résumé"]):
            return True
        if calculate_similarity(new_entry["Titre"], ex["Titre"]) > thr:
            return True
        if new_entry.get("Lien") and ex.get("Lien") and new_entry["Lien"] == ex["Lien"]:
            return True
    return False

# =========================================================
# Résumés FR
# =========================================================

def summarize_fr(raw_text: str) -> tuple:
    """
    Retourne (summary_fr, is_translated, detected_lang)
    - si contenu EN: traduction offline (si dispo), sinon texte d’origine
    - résumé = 2 phrases max, tronqué
    """
    txt = strip_html(raw_text or "")
    if not txt:
        return "", False, "unknown"

    lang = detect_language_simple(txt)
    is_tr = False

    if lang == "en":
        t2 = translate_offline_en_to_fr(txt)
        if t2 != txt:
            txt = t2
            is_tr = True
        else:
            # pas de trad dispo; on gardera en EN
            pass

    # split en phrases FR/EN basique
    sents = re.split(r'(?<=[\.\!\?])\s+', txt)
    sents = [s.strip() for s in sents if len(s.strip()) > 10]
    summary = " ".join(sents[:2]) if sents else txt[:MAX_SUMMARY_FR_CHARS]

    if len(summary) > MAX_SUMMARY_FR_CHARS:
        summary = summary[:MAX_SUMMARY_FR_CHARS-1].rsplit(" ", 1)[0] + "…"

    return summary, is_tr, lang

# =========================================================
# HTML
# =========================================================

def build_html(df: pd.DataFrame, generated_at: str) -> str:
    total = len(df)
    high = int((df["Niveau"] == "HIGH").sum()) if total else 0
    sources_count = len(set(df["Source"])) if total else 0
    translated_count = int(df.get("Traduit", pd.Series(dtype=bool)).sum()) if total else 0

    # Lignes du tableau
    if total:
        rows = []
        for _, r in df.iterrows():
            rows.append(
                f"<tr class='hover:bg-gray-50' data-level='{r.get('Niveau','')}' data-source='{html.escape(r.get('Source',''))}'>"
                f"<td class='p-3 text-sm text-gray-600'>{r.get('Date','')}</td>"
                f"<td class='p-3 text-sm'><span class='bg-blue-100 text-blue-800 px-2 py-1 rounded-full'>{html.escape(r.get('Source',''))}</span></td>"
                f"<td class='p-3'><a href='{html.escape(r.get('Lien',''))}' target='_blank' "
                f"class='text-blue-700 hover:underline font-semibold'>{html.escape(r.get('Titre',''))}</a></td>"
                f"<td class='p-3 text-sm text-gray-700' title='{html.escape(r.get('Résumé',''))}'>"
                f"{html.escape(r.get('Résumé',''))}"
                + (" <span class='ml-2 text-xs px-2 py-1 rounded text-white' style='background:#6d28d9'>🇫🇷 Traduit</span>" if r.get("Traduit") else "") +
                "</td>"
                f"<td class='p-3 text-center'><span class='px-2 py-1 bg-indigo-100 text-indigo-800 rounded-full text-sm font-bold'>{int(r.get('Score',0))}</span></td>"
                f"<td class='p-3 text-center'>"
                f"<span class='px-2 py-1 rounded-full text-white text-xs font-bold "
                + ("bg-red-600" if r.get("Niveau")=="HIGH" else "bg-orange-600" if r.get("Niveau")=="MEDIUM" else "bg-green-600")
                + f"'>{r.get('Niveau','')}</span></td>"
                f"<td class='p-3 text-sm text-gray-600'>{html.escape(r.get('Tags',''))}</td>"
                "</tr>"
            )
        rows_html = "\n".join(rows)
    else:
        rows_html = "<tr><td colspan='7' class='p-6 text-center text-gray-500'>Aucune entrée sur la période.</td></tr>"

    # f-string : toutes les accolades JS/CSS doivent être doublées {{ }}
    html_page = f"""<!doctype html>
<html lang="fr">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Veille IA Militaire</title>
<link href="https://cdn.jsdelivr.net/npm/tailwindcss@2.2.19/dist/tailwind.min.css" rel="stylesheet">
<style>
  .summary-cell {{
    max-height: 4.5rem; overflow: hidden; display: -webkit-box;
    -webkit-line-clamp: 3; -webkit-box-orient: vertical; line-height: 1.5;
  }}
</style>
</head>
<body class="bg-gray-50">
<header class="bg-blue-900 text-white">
  <div class="max-w-6xl mx-auto px-4 py-6 flex items-center justify-between">
    <div class="flex items-center gap-3">
      <span class="text-2xl">⚓</span>
      <div>
        <h1 class="text-2xl font-bold">Veille IA Militaire</h1>
        <div class="text-blue-200 text-sm">Fenêtre {DAYS_WINDOW} jours • Généré : {generated_at}</div>
      </div>
    </div>
    <button id="btnCsv" class="bg-blue-600 hover:bg-blue-500 px-4 py-2 rounded">Exporter CSV</button>
  </div>
</header>

<main class="max-w-6xl mx-auto px-4 py-6">
  <!-- Stats -->
  <div class="grid grid-cols-1 md:grid-cols-4 gap-4 mb-6">
    <div class="bg-white rounded shadow p-4 text-center">
      <div class="text-3xl font-bold text-blue-700">{total}</div>
      <div class="text-gray-600">Articles analysés</div>
    </div>
    <div class="bg-white rounded shadow p-4 text-center">
      <div class="text-3xl font-bold text-red-600">{high}</div>
      <div class="text-gray-600">Priorité Haute</div>
    </div>
    <div class="bg-white rounded shadow p-4 text-center">
      <div class="text-3xl font-bold text-green-600">{sources_count}</div>
      <div class="text-gray-600">Sources actives</div>
    </div>
    <div class="bg-white rounded shadow p-4 text-center">
      <div class="text-3xl font-bold text-purple-600">{translated_count}</div>
      <div class="text-gray-600">Articles traduits</div>
    </div>
  </div>

  <!-- Filtres -->
  <div class="bg-white rounded shadow p-4 mb-4">
    <div class="grid grid-cols-1 md:grid-cols-3 gap-4">
      <input id="q" type="search" placeholder="Recherche (titre, résumé, tags)…" class="border rounded px-3 py-2">
      <select id="level" class="border rounded px-3 py-2">
        <option value="">Niveau (tous)</option>
        <option value="HIGH">HIGH</option>
        <option value="MEDIUM">MEDIUM</option>
        <option value="LOW">LOW</option>
      </select>
      <input id="source" type="search" placeholder="Filtrer par source…" class="border rounded px-3 py-2">
    </div>
  </div>

  <!-- Tableau -->
  <div class="bg-white rounded shadow overflow-x-auto">
    <table class="min-w-full">
      <thead class="bg-blue-50">
        <tr>
          <th class="text-left p-3">Date</th>
          <th class="text-left p-3">Source</th>
          <th class="text-left p-3">Article</th>
          <th class="text-left p-3">Résumé (FR)</th>
          <th class="text-left p-3">Score</th>
          <th class="text-left p-3">Niveau</th>
          <th class="text-left p-3">Tags</th>
        </tr>
      </thead>
      <tbody id="tbody">
        {rows_html}
      </tbody>
    </table>
  </div>
</main>

<script>
(function() {{
  const rows = Array.from(document.querySelectorAll("#tbody tr"));
  const q = document.getElementById("q");
  const level = document.getElementById("level");
  const source = document.getElementById("source");

  function applyFilters() {{
    const qv = (q.value || "").toLowerCase();
    const lv = level.value;
    const sv = (source.value || "").toLowerCase();
    rows.forEach(tr => {{
      const t = tr.innerText.toLowerCase();
      const rl = tr.getAttribute("data-level") || "";
      const rs = (tr.getAttribute("data-source") || "").toLowerCase();
      let ok = true;
      if (qv && !t.includes(qv)) ok = false;
      if (lv && rl !== lv) ok = false;
      if (sv && !rs.includes(sv)) ok = false;
      tr.style.display = ok ? "" : "none";
    }});
  }}
  [q, level, source].forEach(el => el.addEventListener("input", applyFilters));

  // Export CSV — sans template string JS pour éviter les f-strings Python
  document.getElementById("btnCsv").addEventListener("click", () => {{
    const header = ["Titre","Lien","Date","Source","Résumé","Niveau","Score","Tags"];
    const table = document.querySelector("#tbody");
    const data = [];
    for (const tr of table.querySelectorAll("tr")) {{
      if (tr.style.display === "none") continue;
      const tds = tr.querySelectorAll("td");
      if (tds.length < 7) continue;
      const titre = tds[2].innerText.trim();
      const lienA = tds[2].querySelector("a");
      const lien = lienA ? lienA.getAttribute("href") : "";
      const row = [
        titre,
        lien,
        tds[0].innerText.trim(),
        tds[1].innerText.trim(),
        tds[3].innerText.trim(),
        tds[5].innerText.trim(),
        tds[4].innerText.trim(),
        tds[6].innerText.trim()
      ];
      data.push(row);
    }}
    const csv = [header, ...data]
      .map(r => r.map(x => '"' + String((x==null ? "" : x)).replace(/"/g,'""') + '"').join(","))
      .join("\\n");
    const blob = new Blob([csv], {{type: "text/csv;charset=utf-8"}});
    const url = URL.createObjectURL(blob);
    const a = document.createElement("a");
    a.href = url;
    a.download = "veille_ia_militaire.csv";
    a.click();
    URL.revokeObjectURL(url);
  }});
}})();
</script>
</body>
</html>
"""
    return html_page

# =========================================================
# Main
# =========================================================

def main():
    os.makedirs(OUT_DIR, exist_ok=True)
    now_utc = datetime.now(timezone.utc)
    cutoff = now_utc - timedelta(days=DAYS_WINDOW)

    entries = []
    for url in RSS_FEEDS:
        try:
            feed = feedparser.parse(url)
        except Exception as e:
            print(f"[WARN] RSS parse failed: {url} -> {e}")
            continue

        source_title = feed.feed.get("title", url)
        bucket = []

        for entry in feed.entries:
            dt = parse_entry_datetime(entry)
            if not dt or dt < cutoff:
                continue

            title = (entry.get("title") or "").strip()
            link = (entry.get("link") or "").strip()
            raw = entry.get("summary") or entry.get("description") or ""
            raw = strip_html(raw)

            if not title or not link:
                continue
            if is_noise(title, raw):
                continue
            if REQUIRE_AI and not is_ai_related(title, raw):
                continue

            mil_bonus, mil_cats, mil_ok = military_relevance(title, raw)
            if MIL_REQUIRED and not mil_ok:
                continue

            # Résumé FR
            resume_fr, is_tr, lang = summarize_fr(raw)

            score, level, tags = score_text(title, resume_fr)
            if mil_ok:
                score += mil_bonus
            if mil_ok and BOOST_DEFENSE:
                score += BOOST_DEFENSE
            level = "HIGH" if score >= 9 else "MEDIUM" if score >= 5 else "LOW"

            row = {
                "DateUTC": dt, "Date": dt.strftime("%Y-%m-%d"),
                "Source": source_title,
                "Titre": title, "Résumé": resume_fr, "Lien": link,
                "Score": int(score), "Niveau": level,
                "Tags": ", ".join(sorted(set(tags + mil_cats))),
                "Traduit": bool(is_tr)
            }

            if not is_duplicate_article(row, bucket):
                bucket.append(row)

        entries.extend(bucket)
        print(f"[INFO] {source_title}: {len(bucket)} articles retenus")

    if entries:
        df = pd.DataFrame(entries).sort_values(by=["Score", "DateUTC"], ascending=[False, False])
    else:
        df = pd.DataFrame(columns=["DateUTC","Date","Source","Titre","Résumé","Lien","Score","Niveau","Tags","Traduit"])

    generated_at = now_utc.strftime("%Y-%m-%d %H:%M UTC")
    html_page = build_html(df, generated_at)
    with open(os.path.join(OUT_DIR, OUT_FILE), "w", encoding="utf-8") as f:
        f.write(html_page)
    print(f"[OK] Généré: {OUT_DIR}/{OUT_FILE} — {len(df)} entrées")

if __name__ == "__main__":
    main()
