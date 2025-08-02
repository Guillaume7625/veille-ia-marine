#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Veille IA – Militaire / Marine (GitHub Actions + Pages)
- Fenêtre glissante paramétrable (env DAYS_WINDOW, défaut 30 jours)
- Filtrage obligatoire : IA + (défense/marine/cyber…)
- Traduction offline EN→FR (Argos) des résumés anglais uniquement
- Déduplication (hash titre+lien)
- UI Tailwind + filtres simples + export CSV (sans template string JS)
- Déploiement vers docs/index.html
Aucune télémétrie.
"""

import os
import re
import html
import hashlib
import calendar
from datetime import datetime, timezone, timedelta

import feedparser

# ========= CONFIG =========
DAYS_WINDOW = int(os.getenv("DAYS_WINDOW", "30"))
OUT_DIR = "docs"
OUT_FILE = "index.html"
MAX_SUMMARY_CHARS = 500
OFFLINE_TRANSLATION = os.getenv("OFFLINE_TRANSLATION", "true").lower() in {"1", "true", "yes"}
ARGOS_FROM_LANG = "en"
ARGOS_TO_LANG = "fr"

# Flux RSS (tu peux en ajouter/retirer)
RSS_FEEDS = {
    "Numerama":             "https://www.numerama.com/feed/",
    "ActuIA":               "https://www.actuia.com/feed/",
    "VentureBeat AI":       "https://venturebeat.com/category/ai/feed/",
    "C4ISRNET":             "https://www.c4isrnet.com/arc/outboundfeeds/rss/",
    "Breaking Defense":     "https://breakingdefense.com/feed/",
    "Naval Technology":     "https://www.naval-technology.com/feed/",
    # "Defense News":       "https://www.defensenews.com/arc/outboundfeeds/rss/",
    # "Naval News":         "https://www.navalnews.com/feed/",
}

# Vocabulaires
AI_HINTS = {
    " ai ", "artificial intelligence", "intelligence artificielle",
    "machine learning", "deep learning", "gpt", "llm", "model", "modèle",
    "neural", "agent", "multi-agent", "computer vision", "nlp", "rlhf",
}

DEFENSE_HINTS = {
    "défense","defense","marine","naval","navy","otan","nato","armée","forces",
    "cyber","cybersécurité","cybersecurity","uav","drone","ew","electronic warfare",
    "sonar","radar","missile","satellite","space force","c4isr","isr","command",
    "datalink","interoperability","multi-domain","sous-marin","frégate","aéronaval",
}

NOISE_TERMS = {
    "jeux vidéo","gameplay","deal du jour","meilleur prix","précommande","précommander",
    "série netflix","prime video","battlefield","nintendo","playstation","xbox",
    "bons plans","promo","cinéma","people","test jeu",
}

# ========= Utils texte =========
def strip_html(text: str) -> str:
    if not text:
        return ""
    text = re.sub(r"<[^>]+>", " ", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text

def normalize(txt: str) -> str:
    return re.sub(r"\s+", " ", (txt or "").lower()).strip()

def contains_any(text: str, vocab: set) -> bool:
    t = normalize(text)
    return any(k in t for k in vocab)

# ========= Détection langue & traduction (offline) =========
def is_english(text: str) -> bool:
    """Heuristique simple suffisante pour résumés presse."""
    if not text or len(text) < 24:
        return False
    t = f" {text.lower()} "
    en_markers = [" the ", " and ", " with ", " from ", " this ", " that ", " will ", " would ", " can ", " could ", " has ", " have "]
    return sum(t.count(w) for w in en_markers) > 1

_ARGOS_TRANSLATION = None

def init_argos_translation():
    """Charge la traduction EN->FR si disponible (installée par le workflow)."""
    global _ARGOS_TRANSLATION
    if _ARGOS_TRANSLATION is not None:
        return _ARGOS_TRANSLATION
    if not OFFLINE_TRANSLATION:
        _ARGOS_TRANSLATION = None
        return None
    try:
        from argostranslate import translate
        langs = translate.get_installed_languages()
        from_lang = next((l for l in langs if l.code == ARGOS_FROM_LANG), None)
        to_lang = next((l for l in langs if l.code == ARGOS_TO_LANG), None)
        if from_lang and to_lang:
            _ARGOS_TRANSLATION = from_lang.get_translation(to_lang)
        else:
            _ARGOS_TRANSLATION = None
    except Exception as e:
        print(f"[Argos] init error: {e}")
        _ARGOS_TRANSLATION = None
    return _ARGOS_TRANSLATION

def translate_en_to_fr_offline(text: str) -> str:
    if not OFFLINE_TRANSLATION or not text:
        return text
    tr = init_argos_translation()
    if not tr:
        return text
    try:
        return tr.translate(text)
    except Exception as e:
        print(f"[Argos] translation error: {e}")
        return text

def summarize_fr(raw_text: str) -> tuple[str, bool]:
    """
    Retourne (résumé_FR, traduit_bool)
    - si EN -> traduit en FR
    - si FR -> inchangé
    - tronque à MAX_SUMMARY_CHARS
    """
    txt = strip_html(raw_text)
    if is_english(txt):
        txt = translate_en_to_fr_offline(txt)
        translated = True
    else:
        translated = False

    # Résumé extractif simple : 1-2 phrases
    sents = re.split(r'(?<=[\.\!\?])\s+', txt)
    sents = [s.strip() for s in sents if len(s.strip()) > 10]
    summary = " ".join(sents[:2]) if sents else txt
    if len(summary) > MAX_SUMMARY_CHARS:
        summary = summary[:MAX_SUMMARY_CHARS-1].rsplit(" ", 1)[0] + "…"
    return summary, translated

# ========= Scoring / dédup =========
def compute_score(title: str, summary: str) -> tuple[int, str]:
    base = 1 if contains_any(title + " " + summary, AI_HINTS) else 0
    bonus = 2 if contains_any(title + " " + summary, DEFENSE_HINTS) else 0
    score = base + bonus
    level = "HIGH" if score >= 3 else "MEDIUM" if score == 2 else "LOW"
    return score, level

def entry_hash(title: str, link: str) -> str:
    return hashlib.md5((normalize(title) + "|" + (link or "")).encode("utf-8")).hexdigest()

# ========= RSS helpers =========
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
            try:
                return datetime.fromisoformat(s)
            except Exception:
                pass
    return None

# ========= HTML =========
def build_html(items: list[dict]):
    total = len(items)
    high = sum(1 for x in items if x["level"] == "HIGH")
    sources_count = len({x["source"] for x in items})
    translated_count = sum(1 for x in items if x["translated"])

    def lv_badge(lv: str) -> str:
        return {"HIGH":"bg-red-600","MEDIUM":"bg-orange-600","LOW":"bg-green-600"}.get(lv,"bg-gray-500")

    rows = []
    for e in items:
        tr_badge = ' <span class="ml-2 px-2 py-0.5 rounded text-xs text-white" style="background:#6d28d9">🇫🇷 Traduit</span>' if e["translated"] else ""
        rows.append(
            f"<tr class='hover:bg-gray-50' data-level='{e['level']}' data-source='{html.escape(e['source'])}'>"
            f"<td class='p-3 text-sm text-gray-600'>{e['date']}</td>"
            f"<td class='p-3 text-xs'><span class='bg-blue-100 text-blue-800 px-2 py-1 rounded'>{html.escape(e['source'])}</span></td>"
            f"<td class='p-3'><a class='text-blue-700 hover:underline font-semibold' target='_blank' href='{html.escape(e['link'])}'>{html.escape(e['title'])}</a></td>"
            f"<td class='p-3 text-sm text-gray-800'>{html.escape(e['summary'])}{tr_badge}</td>"
            f"<td class='p-3 text-center'><span class='bg-indigo-100 text-indigo-800 px-2 py-1 rounded text-sm font-bold'>{e['score']}</span></td>"
            f"<td class='p-3 text-center'><span class='text-white px-2 py-1 rounded text-xs {lv_badge(e['level'])}'>{e['level']}</span></td>"
            f"<td class='p-3 text-sm'>{html.escape(e['tags'])}</td>"
            "</tr>"
        )

    generated = datetime.utcnow().strftime("%Y-%m-%d %H:%M UTC")

    # f-string: doubles accolades {{ }} pour les blocs CSS/JS
    html_page = f"""<!doctype html>
<html lang="fr"><head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
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
        <h1 class="text-2xl font-bold">Veille IA – Militaire</h1>
        <div class="text-blue-200 text-sm">Fenêtre {DAYS_WINDOW} jours • Généré : {generated}</div>
      </div>
    </div>
    <button id="btnCsv" class="bg-blue-600 hover:bg-blue-500 px-4 py-2 rounded">Exporter CSV</button>
  </div>
</header>

<main class="max-w-6xl mx-auto px-4 py-6">
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
        {"".join(rows)}
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

  // Export CSV (sans template string JS pour éviter les f-strings Python)
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
</body></html>
"""
    with open(os.path.join(OUT_DIR, OUT_FILE), "w", encoding="utf-8") as f:
        f.write(html_page)
    print(f"[OK] Généré: {OUT_DIR}/{OUT_FILE} – {total} entrées")

# ========= Collecte & rendu =========
def main():
    os.makedirs(OUT_DIR, exist_ok=True)
    cutoff = datetime.now(timezone.utc) - timedelta(days=DAYS_WINDOW)

    items = []
    seen = set()

    for src, url in RSS_FEEDS.items():
        print(f"📡 {src}")
        try:
            feed = feedparser.parse(url)
        except Exception as e:
            print(f"[WARN] RSS parse failed: {url} -> {e}")
            continue

        for e in feed.entries:
            dt = parse_entry_datetime(e)
            if not dt or dt < cutoff:
                continue

            title = (e.get("title") or "").strip()
            link = (e.get("link") or "").strip()
            raw_summary = e.get("summary") or e.get("description") or ""
            if not title or not link:
                continue

            # Bruit hors scope
            if contains_any(title + " " + raw_summary, NOISE_TERMS):
                continue

            # IA + Défense obligatoires
            if not contains_any(title + " " + raw_summary, AI_HINTS):
                continue
            if not contains_any(title + " " + raw_summary, DEFENSE_HINTS):
                continue

            # Dédup simple (titre+lien)
            h = entry_hash(title, link)
            if h in seen:
                continue
            seen.add(h)

            # Résumé FR (traduction EN→FR si nécessaire)
            summary_fr, translated = summarize_fr(raw_summary)

            # Score
            score, level = compute_score(title, summary_fr)

            items.append(dict(
                date=dt.strftime("%Y-%m-%d"),
                source=src,
                title=title,
                link=link,
                summary=summary_fr,
                score=score,
                level=level,
                tags="ia, defense" if contains_any(title + " " + summary_fr, DEFENSE_HINTS) else "ia",
                translated=translated
            ))

    # Tri: score desc puis date desc
    items.sort(key=lambda x: (x["score"], x["date"]), reverse=True)
    build_html(items)

if __name__ == "__main__":
    main()
