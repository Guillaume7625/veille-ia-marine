#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Veille IA – Marine nationale (full web, GitHub Actions + Pages) – Version RSS Only
- 7 jours glissants
- Dédup (titre + lien)
- Scoring "défense/marine"
- UI Tailwind + filtres + stats + export CSV
Aucune télémétrie, aucun tracker tiers ajouté.
"""

import os
import re
import json
import unicodedata
import calendar
from hashlib import md5
from datetime import datetime, timezone, timedelta

import feedparser
import pandas as pd

# ---------------- Configuration ----------------

RSS_FEEDS = [
    # IA FR
    "https://www.actuia.com/feed/",
    "https://www.usine-digitale.fr/rss/technos/intelligence-artificielle/",
    "https://www.ia-data.fr/feed/",
    "https://www.numerama.com/feed/",
    # IA EN
    "https://venturebeat.com/category/ai/feed/",
    "https://feeds.feedburner.com/mittechnologyreview/artificial-intelligence",
    "https://spectrum.ieee.org/rss/topic/artificial-intelligence",
    # Défense / Naval / Cyber
    "https://www.c4isrnet.com/arc/outboundfeeds/rss/",
    "https://breakingdefense.com/feed/",
    "https://www.naval-technology.com/feed/",
    "https://www.cybersecuritydive.com/feeds/news/",
]

DAYS_WINDOW = int(os.getenv("DAYS_WINDOW", "7"))
OUT_DIR = "docs"
OUT_FILE = "index.html"   # GitHub Pages servira docs/index.html
MAX_SUMMARY_CHARS = 500

# Scoring par mots-clés (accent-insensible)
KEYWORDS_WEIGHTS = {
    "marine": 5, "naval": 5, "navire": 3, "frégate": 4, "sous-marin": 5, "maritime": 3,
    "armée": 3, "defense": 4, "défense": 4, "otan": 4, "nato": 4, "doctrine": 3,
    "souveraineté": 4, "cyber": 4, "cybersécurité": 4, "cyberdéfense": 5,
    "radar": 3, "sonar": 4, "drone": 4, "uav": 4, "aéronaval": 5,
    "brouillage": 4, "guerre électronique": 5, "satellite": 3, "reconnaissance": 3,
    "mistral": 2, "ia générative": 2, "modèle souverain": 3, "renseignement": 4, "osint": 3
}

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

def score_text(title: str, summary: str):
    txt = normalize(f"{title or ''} {summary or ''}")
    tags, score = [], 0
    for k, w in KEYWORDS_WEIGHTS.items():
        nk = normalize(k)
        if nk in txt:
            score += w
            tags.append(k)
    # unique tags keeping order
    seen = set()
    tags = [t for t in tags if not (t in seen or seen.add(t))]
    level = "HIGH" if score >= 9 else "MEDIUM" if score >= 5 else "LOW"
    return score, level, tags

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

def main():
    os.makedirs(OUT_DIR, exist_ok=True)
    now_utc = datetime.now(timezone.utc)
    cutoff = now_utc - timedelta(days=DAYS_WINDOW)

    entries, seen = [], set()

    # 1) RSS uniquement
    for url in RSS_FEEDS:
        try:
            feed = feedparser.parse(url)
        except Exception as e:
            print(f"[WARN] RSS parse failed: {url} -> {e}")
            continue
        source_title = feed.feed.get("title", url)
        for entry in feed.entries:
            dt = parse_entry_datetime(entry)
            if not dt or dt < cutoff:
                continue
            title = (entry.get("title") or "").strip()
            link  = (entry.get("link")  or "").strip()
            summary = strip_html(entry.get("summary") or entry.get("description") or "")[:MAX_SUMMARY_CHARS]
            h = md5((title + "|" + link).encode("utf-8")).hexdigest()
            if h in seen or not title or not link:
                continue
            seen.add(h)
            score, level, tags = score_text(title, summary)
            entries.append({
                "DateUTC": dt, "Date": dt.strftime("%Y-%m-%d"),
                "Source": source_title, "Titre": title, "Résumé": summary, "Lien": link,
                "Score": score, "Niveau": level, "Tags": ", ".join(tags)
            })

    # DataFrame & tri
    if entries:
        df = pd.DataFrame(entries).sort_values(by=["Score", "DateUTC"], ascending=[False, False])
    else:
        df = pd.DataFrame(columns=["DateUTC","Date","Source","Titre","Résumé","Lien","Score","Niveau","Tags"])

    # Stats
    total = len(df)
    high = int((df["Niveau"] == "HIGH").sum()) if total else 0
    med  = int((df["Niveau"] == "MEDIUM").sum()) if total else 0
    low  = int((df["Niveau"] == "LOW").sum()) if total else 0

    # JSON for CSV export
    export_items = []
    for _, r in df.iterrows():
        export_items.append({
            "titre": r.get("Titre",""),
            "lien": r.get("Lien",""),
            "date": r.get("Date",""),
            "source": r.get("Source",""),
            "resume": r.get("Résumé",""),
            "niveau": r.get("Niveau",""),
            "score": int(r.get("Score",0)),
            "tags": r.get("Tags",""),
        })
    export_json = json.dumps(export_items, ensure_ascii=False)

    generated_at = now_utc.strftime("%Y-%m-%d %H:%M UTC")

    # HTML (Tailwind CDN) — version sans f-string complexe
    row_tpl = (
        "<tr data-level='{level}' data-source='{source}'>"
        "<td>{date}</td><td>{source}</td><td>{title}</td><td>{summary}</td>"
        "<td>{score}</td><td><span class='px-2 py-1 rounded text-white {color}'>{level}</span></td>"
        "<td>{tags}</td><td><a href='{link}' target='_blank' class='text-blue-700 underline'>Lien</a></td>"
        "</tr>"
    )

    rows = []
    for _, r in df.iterrows():
        level = r["Niveau"]
        color = "bg-red-600" if level == "HIGH" else "bg-orange-600" if level == "MEDIUM" else "bg-green-600"
        rows.append(row_tpl.format(
            level=level,
            source=r["Source"],
            date=r["Date"],
            title=r["Titre"],
            summary=r["Résumé"],
            score=int(r["Score"]),
            tags=r["Tags"],
            link=r["Lien"],
            color=color
        ))
    rows_html = "\n".join(rows) if rows else (
        "<tr><td colspan='8' class='text-center py-6'>Aucune entrée sur la période.</td></tr>"
    )

    html = f"""<!doctype html>
<html lang="fr">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Veille IA – Marine nationale (7 jours)</title>
<link href="https://cdn.jsdelivr.net/npm/tailwindcss@2.2.19/dist/tailwind.min.css" rel="stylesheet">
</head>
<body class="bg-gray-50">
<header class="bg-blue-900 text-white py-6">
  <div class="max-w-6xl mx-auto px-4 flex items-center justify-between">
    <div class="flex items-center gap-3">
      <span class="text-2xl">⚓</span>
      <div>
        <h1 class="text-2xl font-bold">Veille IA – Marine nationale</h1>
        <div class="text-blue-200 text-sm">Fenêtre roulante 7 jours • Généré : {generated_at}</div>
      </div>
    </div>
    <button id="btnCsv" class="bg-blue-600 hover:bg-blue-500 px-4 py-2 rounded">Exporter CSV</button>
  </div>
</header>

<main class="max-w-6xl mx-auto px-4 py-6">
  <div class="grid grid-cols-1 md:grid-cols-4 gap-4 mb-6">
    <div class="bg-white rounded shadow p-4 text-center">
      <div class="text-3xl font-bold text-blue-700">{total}</div>
      <div class="text-gray-600">Articles</div>
    </div>
    <div class="bg-white rounded shadow p-4 text-center">
      <div class="text-3xl font-bold text-red-600">{high}</div>
      <div class="text-gray-600">Priorité Haute</div>
    </div>
    <div class="bg-white rounded shadow p-4 text-center">
      <div class="text-3xl font-bold text-orange-600">{med}</div>
      <div class="text-gray-600">Priorité Moyenne</div>
    </div>
    <div class="bg-white rounded shadow p-4 text-center">
      <div class="text-3xl font-bold text-green-600">{low}</div>
      <div class="text-gray-600">Priorité Faible</div>
    </div>
  </div>

  <div class="bg-white rounded shadow p-4 mb-4">
    <div class="grid grid-cols-1 md:grid-cols-3 gap-4">
      <input id="q" type="search" placeholder="Recherche (titre, résumé, tags)…"
             class="border rounded px-3 py-2">
      <select id="level" class="border rounded px-3 py-2">
        <option value="">Niveau (tous)</option>
        <option value="HIGH">HIGH</option>
        <option value="MEDIUM">MEDIUM</option>
        <option value="LOW">LOW</option>
      </select>
      <input id="source" type="search" placeholder="Filtrer par source…"
             class="border rounded px-3 py-2">
    </div>
  </div>

  <div class="bg-white rounded shadow overflow-x-auto">
    <table class="min-w-full">
      <thead class="bg-blue-50">
        <tr>
          <th class="text-left p-3">Date</th>
          <th class="text-left p-3">Source</th>
          <th class="text-left p-3">Titre</th>
          <th class="text-left p-3">Résumé</th>
          <th class="text-left p-3">Score</th>
          <th class="text-left p-3">Niveau</th>
          <th class="text-left p-3">Tags</th>
          <th class="text-left p-3">Lien</th>
        </tr>
      </thead>
      <tbody id="tbody">
        {rows_html}
      </tbody>
    </table>
  </div>
</main>

<script>
const rows = Array.from(document.querySelectorAll("#tbody tr"));
const q = document.getElementById("q");
const level = document.getElementById("level");
const source = document.getElementById("source");
const data = {export_json};

function applyFilters() {{
  const qv = (q.value || "").toLowerCase();
  const lv = level.value;
  const sv = (source.value || "").toLowerCase();
  rows.forEach(tr => {{
    const t = tr.innerText.toLowerCase();
    const rl = tr.getAttribute("data-level");
    const rs = (tr.getAttribute("data-source") || "").toLowerCase();
    let ok = true;
    if (qv && !t.includes(qv)) ok = false;
    if (lv && rl !== lv) ok = false;
    if (sv && !rs.includes(sv)) ok = false;
    tr.style.display = ok ? "" : "none";
  }});
}}
[q, level, source].forEach(el => el.addEventListener("input", applyFilters));

document.getElementById("btnCsv").addEventListener("click", () => {{
  const header = ["Titre","Lien","Date","Source","Résumé","Niveau","Score","Tags"];
  const rows = data.map(a => [a.titre, a.lien, a.date, a.source, a.resume, a.niveau, a.score, a.tags]);
  const csv = [header, ...rows].map(r => r.map(x => `"${(x||"").replace(/"/g,'""')}"`).join(",")).join("\\n");
  const blob = new Blob([csv], {{type: "text/csv;charset=utf-8"}});
  const url = URL.createObjectURL(blob);
  const a = document.createElement("a");
  a.href = url;
  a.download = "veille_ia_marine.csv";
  a.click();
  URL.revokeObjectURL(url);
}});
</script>
</body>
</html>
"""
    with open(os.path.join(OUT_DIR, OUT_FILE), "w", encoding="utf-8") as f:
        f.write(html)

if __name__ == "__main__":
    main()
