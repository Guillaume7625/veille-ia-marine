#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Veille IA – Militaire (GitHub Actions + Pages)
- IA OBLIGATOIRE + intérêt militaire (combat OU soutien/logistique/formation/industrie…)
- Fenêtre glissante 7 jours
- Déduplication (URL + Jaccard sur titres) + nettoyage auto > 7 jours
- Résumés courts (2 phrases) dans la langue source (pas de traduction)
- UI moderne (Tailwind) avec filtres et export CSV
- Santé: docs/feed_health.json
Aucune télémétrie, aucun tracker tiers.
"""

import os
import re
import json
import time
import unicodedata
import calendar
import hashlib
from datetime import datetime, timezone, timedelta

import feedparser
import pandas as pd

# ========================= Configuration =========================

# Fenêtre de collecte / nettoyage
MAX_ARTICLE_AGE_DAYS = int(os.getenv("DAYS_WINDOW", "7"))

# Sortie GitHub Pages (publier docs/)
OUT_DIR = "docs"
OUT_FILE = "index.html"
MAX_SUMMARY_CHARS = 280

# IA obligatoire & intérêt militaire obligatoire
REQUIRE_AI = True
MIL_REQUIRED = True
BOOST_DEFENSE = 2  # bonus si IA + (marine/cyber/C2/ISR/logistique…)

# Déduplication
ENABLE_DEDUPLICATION = True
JACCARD_TITLE_THRESHOLD = 0.80  # seuil Jaccard titres

# Sources RSS (nom lisible -> URL)
RSS_SOURCES = {
    # IA FR
    "ActuIA": "https://www.actuia.com/feed/",
    "Usine Digitale – IA": "https://www.usine-digitale.fr/rss/technos/intelligence-artificielle/",
    "IA-Data": "https://www.ia-data.fr/feed/",
    "Numerama – Tech": "https://www.numerama.com/feed/",
    # IA EN
    "VentureBeat – AI": "https://venturebeat.com/category/ai/feed/",
    "MIT Tech Review – AI": "https://feeds.feedburner.com/mittechnologyreview/artificial-intelligence",
    "IEEE Spectrum – AI": "https://spectrum.ieee.org/rss/topic/artificial-intelligence",
    # Défense / Naval / Cyber (filtrées ensuite par IA obligatoire)
    "C4ISRNet": "https://www.c4isrnet.com/arc/outboundfeeds/rss/",
    "Breaking Defense": "https://breakingdefense.com/feed/",
    "Naval Technology": "https://www.naval-technology.com/feed/",
    "Cybersecurity Dive": "https://www.cybersecuritydive.com/feeds/news/",
}

# -------------------- Vocabulaires & filtres ---------------------

# Détection IA par REGEX (sur texte normalisé, sans accents)
AI_PATTERNS = [
    r"\bintelligence artificielle\b",
    r"\bia\b", r"\bgenerative ai\b",
    r"\bmachine learning\b", r"\bdeep learning\b",
    r"\bneural network(s)?\b", r"\bcomputer vision\b",
    r"\blarge language model(s)?\b", r"\bllm(s)?\b",
    r"\btransformer(s)?\b",
    r"\bapprentissage automatique\b", r"\bapprentissage profond\b",
    r"\bmodele de langage\b", r"\binference\b",
    r"\bagent(s)? autonome(s)?\b", r"\bagent(s)?\b",
]

# Combat / ISR / effets
MIL_COMBAT_HINTS = {
    "c2", "jadc2", "command and control", "battle management", "bms",
    "c4isr", "isr", "renseignement", "osint", "ew", "guerre electronique",
    "anti-jam", "tactical data link", "link-16", "satcom", "pnt", "gnss",
    "drone", "uav", "ucav", "loitering", "munitions", "targeting",
    "sonar", "asw", "radar", "missile", "counter-uas", "electronic warfare",
    "sensor fusion", "multi-domain", "kill chain", "force protection",
    "cbrn", "nbc", "manpads", "sam", "datalink", "interoperability", "joint fires",
}

# Soutien / non-combat
MIL_SUPPORT_HINTS = {
    "logistique", "supply chain", "maintenance", "mco", "predictive maintenance",
    "mro", "planification", "readiness", "formation", "entrainement",
    "simulation", "wargaming", "doctrine", "procurement",
    "acquisition", "export control", "itar", "ethique", "ihl", "loac",
    "souverainete", "cloud de confiance", "secnumcloud", "securite des donnees",
    "cyberdefense", "cyber defense", "detection", "reponse a incident", "hardening",
}

# Domaines privilégiés
MARITIME_HINTS = {"marine", "naval", "navy", "fregate", "corvette", "uuv", "auv", "mcm", "sous-marin"}
CYBER_HINTS    = {"cyber", "cybersecurite", "cybersecurity", "ransomware", "intrusion"}
SPACE_HINTS    = {"satellite", "constellation", "leo", "meo", "geostationary", "geostationnaire", "rf sensing"}
C2_HINTS       = {"c2", "jadc2", "mission command", "tactical cloud", "edge", "mesh network"}

# Bruit pop culture / e-commerce à exclure
BLACKLIST_KEYWORDS = {
    "jeu video", "jeux video", "gaming", "nintendo", "playstation", "xbox", "steam",
    "prime video", "netflix", "disney+", "serie", "cinema", "film", "bande-originale",
    "deal", "promo", "bon plan", "meilleur prix", "precommander", "precommande",
    "people", "gossip", "show", "trailer",
}

# Poids de tags (pour le score de base)
KEYWORDS_WEIGHTS = {
    "marine": 5, "naval": 5, "navire": 3, "fregate": 4, "sous-marin": 5, "maritime": 3,
    "armee": 3, "defense": 4, "defence": 4, "otan": 4, "nato": 4, "doctrine": 3,
    "souverainete": 4, "cyber": 4, "cybersecurite": 4, "cyberdefense": 5,
    "radar": 3, "sonar": 4, "drone": 4, "uav": 4, "aeronaval": 5,
    "brouillage": 4, "guerre electronique": 5, "satellite": 3, "reconnaissance": 3,
    "ia generative": 2, "modele souverain": 3, "renseignement": 4, "osint": 3,
}

# ========================= Utilitaires ===========================

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
    # harmoniser quelques caractères
    s = s.replace("’", "'").replace("œ", "oe")
    return s

def is_ai_related(title: str, summary: str) -> bool:
    """Match IA par regex sur texte normalisé (réduit les faux positifs)."""
    t = normalize(f"{title} {summary}")
    return any(re.search(p, t) for p in AI_PATTERNS)

def _contains_any(text: str, bag) -> bool:
    t = normalize(text)
    return any(k in t for k in bag)

def is_noise(title: str, summary: str) -> bool:
    return _contains_any(f"{title} {summary}", BLACKLIST_KEYWORDS)

def military_relevance(title: str, summary: str):
    """
    Retourne (score_militaire, catégories, is_relevant)
    - catégories dans {combat/ISR, soutien, marine, cyber, espace, C2}
    """
    txt = f"{title} {summary}"
    score = 0
    cats = []

    def hit(words, w, label):
        nonlocal score, cats
        if _contains_any(txt, words):
            score += w
            cats.append(label)

    hit(MIL_COMBAT_HINTS, 3, "combat/ISR")
    hit(MIL_SUPPORT_HINTS, 4, "soutien")
    hit(MARITIME_HINTS,   4, "marine")
    hit(CYBER_HINTS,      3, "cyber")
    hit(SPACE_HINTS,      2, "espace")
    hit(C2_HINTS,         3, "C2")

    is_relevant = score > 0
    # tags uniques en conservant l'ordre
    seen = set()
    cats = [c for c in cats if not (c in seen or seen.add(c))]
    return score, cats, is_relevant

def score_text(title: str, summary: str):
    txt = normalize(f"{title or ''} {summary or ''}")
    tags, score = [], 0
    for k, w in KEYWORDS_WEIGHTS.items():
        nk = normalize(k)
        if nk in txt:
            score += w
            tags.append(k)
    seen = set()
    tags = [t for t in tags if not (t in seen or seen.add(t))]
    level = "HIGH" if score >= 9 else "MEDIUM" if score >= 5 else "LOW"
    return score, level, tags

def parse_entry_datetime(entry):
    # 1) struct_time
    for attr in ("published_parsed", "updated_parsed"):
        t = getattr(entry, attr, None)
        if t:
            try:
                return datetime.fromtimestamp(calendar.timegm(t), tz=timezone.utc)
            except Exception:
                pass
    # 2) strings
    for attr in ("published", "updated", "pubDate"):
        s = entry.get(attr, "")
        if s:
            ts = pd.to_datetime(s, utc=True, errors="coerce")
            if pd.notnull(ts):
                return ts.to_pydatetime()
    return None

def parse_rss_with_retry(url: str, tries: int = 3):
    """Parse RSS avec en-tête UA et retry exponentiel."""
    for i in range(tries):
        try:
            return feedparser.parse(url, request_headers={"User-Agent": "VeilleIA/1.0"})
        except Exception as e:
            if i == tries - 1:
                print(f"[ERROR] Final failure for {url}: {e}")
            else:
                time.sleep(2 ** i)
    return {"entries": [], "feed": {}}

def summarize_brief(text: str, max_chars: int = 280) -> str:
    """Résumé court (2 phrases max) dans la langue source (FR ou EN)."""
    if not text:
        return ""
    clean = strip_html(text)
    sents = re.split(r"(?<=[\.\!\?])\s+", clean)
    sents = [s.strip() for s in sents if len(s.strip()) > 15]
    out = " ".join(sents[:2]) or clean[:200]
    if len(out) > max_chars:
        out = out[: max_chars - 1].rsplit(" ", 1)[0] + "…"
    return out

# ----------------- Déduplication & nettoyage ---------------------

def jaccard_title(a: str, b: str) -> float:
    ta = {w for w in re.findall(r"\w+", normalize(a)) if len(w) > 2}
    tb = {w for w in re.findall(r"\w+", normalize(b)) if len(w) > 2}
    if not ta or not tb:
        return 0.0
    return len(ta & tb) / len(ta | tb)

def is_duplicate_article(new_entry: dict, existing_entries: list) -> bool:
    if not ENABLE_DEDUPLICATION:
        return False
    for ex in existing_entries:
        # 1) URL identique
        if new_entry.get("Lien") and ex.get("Lien") and new_entry["Lien"] == ex["Lien"]:
            return True
        # 2) Titre très proche (Jaccard)
        if jaccard_title(new_entry.get("Titre",""), ex.get("Titre","")) >= JACCARD_TITLE_THRESHOLD:
            return True
    return False

def cleanup_old_entries(entries: list) -> list:
    cutoff = datetime.now(timezone.utc) - timedelta(days=MAX_ARTICLE_AGE_DAYS)
    out, removed = [], 0
    for e in entries:
        dt = e.get("DateUTC")
        if isinstance(dt, datetime):
            if dt >= cutoff:
                out.append(e)
            else:
                removed += 1
        else:
            out.append(e)
    if removed:
        print(f"🧹 {removed} articles supprimés (> {MAX_ARTICLE_AGE_DAYS} jours)")
    return out

# ------------------------- Rendu HTML ----------------------------

from html import escape as html_escape

def generate_modern_html(df: pd.DataFrame, generated_at: str, out_dir: str, out_file: str):
    total = len(df)
    high = int((df["Niveau"] == "HIGH").sum()) if total else 0
    sources_count = len(set(df["Source"])) if total else 0

    # Construit les lignes du tableau (sécurisé)
    rows = []
    for _, r in df.iterrows():
        level = str(r.get("Niveau", ""))
        level_cls = "level-high" if level == "HIGH" else "level-medium" if level == "MEDIUM" else "level-low"
        row = (
            '<tr class="table-row" data-level="{lvl}" data-source="{src}">'
            '<td class="p-4 text-sm text-gray-600">{date}</td>'
            '<td class="p-4"><span class="bg-blue-100 text-blue-800 px-3 py-1 rounded-full text-xs font-medium">{src}</span></td>'
            '<td class="p-4"><a href="{link}" target="_blank" rel="noopener noreferrer" '
            'class="text-blue-700 hover:text-blue-900 font-semibold hover:underline block">{title}</a></td>'
            '<td class="p-4 text-sm text-gray-700 summary-cell" title="{sumfull}">{summary}</td>'
            '<td class="p-4 text-center"><span class="bg-indigo-100 text-indigo-800 px-3 py-1 rounded-full text-sm font-bold">{score}</span></td>'
            '<td class="p-4 text-center"><span class="px-3 py-1 rounded-full text-white text-xs font-bold {lvlcls}">{lvl}</span></td>'
            '<td class="p-4 text-sm">{tags}</td>'
            '</tr>'
        ).format(
            lvl=html_escape(level),
            src=html_escape(str(r.get("Source",""))),
            date=html_escape(str(r.get("Date",""))),
            link=html_escape(str(r.get("Lien",""))),
            title=html_escape(str(r.get("Titre",""))),
            sumfull=html_escape(str(r.get("Résumé",""))),
            summary=html_escape(str(r.get("Résumé",""))),
            score=int(r.get("Score",0)),
            lvlcls=level_cls,
            tags=html_escape(str(r.get("Tags",""))),
        )
        rows.append(row)

    html_template = """<!DOCTYPE html>
<html lang="fr">
<head>
<meta charset="UTF-8"><meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>🎯 Veille IA Militaire – 7 jours</title>
<script src="https://cdn.tailwindcss.com"></script>
<style>
  .gradient-bg {{ background: linear-gradient(135deg, #1e3a8a 0%, #3730a3 100%); }}
  .summary-cell {{ max-height: 4.5rem; overflow: hidden; display: -webkit-box; -webkit-line-clamp: 3; -webkit-box-orient: vertical; line-height: 1.5; }}
  .table-row:hover {{ background-color: #f1f5f9; transition: background-color .2s; }}
  .level-high {{ background: linear-gradient(45deg,#dc2626,#b91c1c); }}
  .level-medium {{ background: linear-gradient(45deg,#d97706,#b45309); }}
  .level-low {{ background: linear-gradient(45deg,#059669,#047857); }}
</style>
</head>
<body class="bg-gray-50 min-h-screen">
<header class="gradient-bg text-white shadow-2xl">
  <div class="max-w-6xl mx-auto px-6 py-8 flex items-center justify-between">
    <div>
      <h1 class="text-3xl md:text-4xl font-bold flex items-center">⚓&nbsp;Veille IA Militaire</h1>
      <p class="text-blue-100 mt-1">Fenêtre {window} jours • Généré : {generated}</p>
    </div>
    <div class="text-right">
      <div class="text-3xl font-bold">{total}</div>
      <div class="text-blue-100 text-sm">Articles analysés</div>
    </div>
  </div>
</header>

<main class="max-w-6xl mx-auto px-6 py-8">
  <div class="grid grid-cols-1 md:grid-cols-3 gap-6 mb-8">
    <div class="bg-white rounded-xl shadow p-6">
      <div class="text-sm text-gray-600">Priorité Haute</div>
      <div class="text-3xl font-bold text-red-600">{high}</div>
    </div>
    <div class="bg-white rounded-xl shadow p-6">
      <div class="text-sm text-gray-600">Sources actives</div>
      <div class="text-3xl font-bold text-blue-700">{sources}</div>
    </div>
    <div class="bg-white rounded-xl shadow p-6">
      <button id="btnCsv" class="w-full bg-blue-600 hover:bg-blue-500 text-white px-4 py-3 rounded-lg">Exporter CSV</button>
    </div>
  </div>

  <div class="bg-white rounded-xl shadow p-4 mb-4">
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

  <div class="bg-white rounded-xl shadow overflow-x-auto">
    <table class="min-w-full">
      <thead class="bg-gray-800 text-white">
        <tr>
          <th scope="col" class="text-left p-3">Date</th>
          <th scope="col" class="text-left p-3">Source</th>
          <th scope="col" class="text-left p-3">Article</th>
          <th scope="col" class="text-left p-3">Résumé</th>
          <th scope="col" class="text-center p-3">Score</th>
          <th scope="col" class="text-center p-3">Niveau</th>
          <th scope="col" class="text-left p-3">Tags</th>
        </tr>
      </thead>
      <tbody id="tbody">
        __ROWS__
      </tbody>
    </table>
  </div>
</main>

<script>
// Filtres
const rows = Array.from(document.querySelectorAll("#tbody tr"));
const q = document.getElementById("q"), level = document.getElementById("level"), source = document.getElementById("source");
function applyFilters(){
  const qv=(q.value||"").toLowerCase(), lv=level.value, sv=(source.value||"").toLowerCase();
  rows.forEach(tr=>{
    const t=tr.innerText.toLowerCase(), rl=tr.getAttribute("data-level"), rs=(tr.getAttribute("data-source")||"").toLowerCase();
    let ok=true; if(qv && !t.includes(qv)) ok=false; if(lv && rl!==lv) ok=false; if(sv && !rs.includes(sv)) ok=false;
    tr.style.display= ok ? "" : "none";
  });
}
[q,level,source].forEach(el=>el.addEventListener("input",applyFilters));

// Export CSV (respecte filtres visuels)
document.getElementById("btnCsv").addEventListener("click", () => {
  const clean = s => (s||"").replace(/\\r?\\n+/g," ").trim();
  const header=["Titre","Lien","Date","Source","Résumé","Niveau","Score","Tags"];
  const visible=[...document.querySelectorAll("#tbody tr")].filter(tr=>tr.style.display!=="none");
  const rows=visible.map(tr=>{
    const tds=[...tr.querySelectorAll("td")].map(td=>clean(td.innerText));
    return [tds[2], tr.querySelector("a")?.href || "", tds[0], tds[1], tds[3], tds[5], tds[4], tds[6]];
  });
  const csv=[header,...rows].map(r=>r.map(x=>`"${(x||"").replace(/"/g,'""')}"`).join(",")).join("\\n");
  const blob=new Blob([csv],{type:"text/csv;charset=utf-8"}); const url=URL.createObjectURL(blob);
  const a=document.createElement("a"); a.href=url; a.download="veille_ia_militaire.csv"; a.click(); URL.revokeObjectURL(url);
});
</script>
</body></html>
"""
    html = html_template.replace("__ROWS__", "\n".join(rows) if rows else '<tr><td colspan="7" class="text-center p-6">Aucune entrée.</td></tr>')
    html = (html
            .replace("{window}", str(MAX_ARTICLE_AGE_DAYS))
            .replace("{generated}", generated_at)
            .replace("{total}", str(total))
            .replace("{high}", str(high))
            .replace("{sources}", str(sources_count))
    )
    os.makedirs(out_dir, exist_ok=True)
    with open(os.path.join(out_dir, out_file), "w", encoding="utf-8") as f:
        f.write(html)

# =========================== Main ================================

def main():
    now_utc = datetime.now(timezone.utc)
    cutoff = now_utc - timedelta(days=MAX_ARTICLE_AGE_DAYS)

    collected = []

    for source_name, url in RSS_SOURCES.items():
        feed = parse_rss_with_retry(url, tries=3)
        entries = getattr(feed, "entries", None) or feed.get("entries", [])
        for entry in entries:
            dt = parse_entry_datetime(entry)
            if not dt or dt < cutoff:
                continue

            title = (entry.get("title") or "").strip()
            link  = (entry.get("link")  or "").strip()
            raw_summary = entry.get("summary") or entry.get("description") or ""
            summary = summarize_brief(raw_summary, MAX_SUMMARY_CHARS)

            if not title or not link:
                continue

            # Filtres : bruit, IA obligatoire, intérêt militaire obligatoire
            if is_noise(title, raw_summary):
                continue
            if REQUIRE_AI and not is_ai_related(title, raw_summary):
                continue

            mil_score, mil_cats, mil_ok = military_relevance(title, raw_summary)
            if MIL_REQUIRED and not mil_ok:
                continue

            # Scoring & tags
            score, level, tags = score_text(title, summary)
            if mil_ok:
                score += mil_score
            if mil_ok and BOOST_DEFENSE:
                score += BOOST_DEFENSE
            level = "HIGH" if score >= 9 else "MEDIUM" if score >= 5 else "LOW"

            collected.append({
                "DateUTC": dt,
                "Date": dt.strftime("%Y-%m-%d"),
                "Source": source_name,
                "Titre": title,
                "Résumé": summary,
                "Lien": link,
                "Score": int(score),
                "Niveau": level,
                "Tags": ", ".join(tags + mil_cats) if tags or mil_cats else "",
            })

    # Déduplication + nettoyage
    unique = []
    for e in collected:
        if not is_duplicate_article(e, unique):
            unique.append(e)
    entries = cleanup_old_entries(unique)

    # DataFrame & tri
    if entries:
        df = pd.DataFrame(entries).sort_values(by=["Score", "DateUTC"], ascending=[False, False])
    else:
        df = pd.DataFrame(columns=["DateUTC","Date","Source","Titre","Résumé","Lien","Score","Niveau","Tags"])

    # Rendu
    generated_at = now_utc.strftime("%Y-%m-%d %H:%M UTC")
    os.makedirs(OUT_DIR, exist_ok=True)
    # Santé simple
    health = {
        "generated_at": generated_at,
        "counts_by_source": (df["Source"].value_counts().to_dict() if len(df) else {}),
        "total": int(len(df)),
        "window_days": int(MAX_ARTICLE_AGE_DAYS),
    }
    with open(os.path.join(OUT_DIR, "feed_health.json"), "w", encoding="utf-8") as f:
        json.dump(health, f, ensure_ascii=False, indent=2)

    generate_modern_html(df, generated_at, OUT_DIR, OUT_FILE)
    print(f"✅ Généré {len(df)} items → {OUT_DIR}/{OUT_FILE}")

if __name__ == "__main__":
    main()
