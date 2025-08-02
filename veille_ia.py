#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Veille IA – Militaire (full web, GitHub Actions + Pages) – Version Hardened
- Fenêtre glissante paramétrable (env DAYS_WINDOW)
- Filtrage strict: tout doit concerner l'IA + intérêt militaire (combat & soutien)
- Dédup (titre + lien + similarité)
- Résumés FR automatiques (offline Argos EN→FR) + badge
- UI Tailwind : filtres (recherche, niveau, source, période) + stats + export CSV
- Déploiement statique dans docs/index.html
Aucune télémétrie, aucun tracker tiers.
"""

import os
import re
import json
import html
import unicodedata
import calendar
from hashlib import md5
from datetime import datetime, timezone, timedelta
from difflib import SequenceMatcher
from pathlib import Path

import feedparser
import pandas as pd

# ------------------------------------------------------------
# Configuration
# ------------------------------------------------------------

# Feeds principalement IA + défense/cyber/naval
RSS_FEEDS = [
    # IA FR
    "https://www.actuia.com/feed/",
    "https://www.numerama.com/feed/",
    # IA EN (qualité)
    "https://venturebeat.com/category/ai/feed/",
    "https://spectrum.ieee.org/rss/topic/artificial-intelligence",
    # Défense / Naval / Cyber
    "https://www.c4isrnet.com/arc/outboundfeeds/rss/",
    "https://breakingdefense.com/feed/",
    "https://www.naval-technology.com/feed/",
    "https://www.cybersecuritydive.com/feeds/news/",
]

DAYS_WINDOW = int(os.getenv("DAYS_WINDOW", "30"))
OUT_DIR = "docs"
OUT_FILE = "index.html"
MAX_SUMMARY_CHARS_RAW = 800  # tronque le résumé source avant traitement

# Exigences de contenu
REQUIRE_AI = True
MIL_REQUIRED = True

# Scoring mots-clés (accent-insensible)
KEYWORDS_WEIGHTS = {
    # IA
    "intelligence artificielle": 3, "ia": 3, "ai ": 3, "ai-": 3, "gpt": 2, "llm": 2,
    "agent": 2, "agents": 2, "vision": 1, "autonome": 2, "autonomous": 2,
    "machine learning": 2, "deep learning": 2, "ml ": 2, "nlp": 1, "predictif": 1, "predictive": 1,

    # Défense/Militaire générique
    "defense": 3, "défense": 3, "armée": 3, "forces": 2, "otan": 2, "nato": 2,

    # Cyber/C2
    "cyber": 3, "cybersécurité": 3, "cybersecurity": 3, "c2": 2, "command and control": 2,
    "c4isr": 3, "ew": 2, "guerre électronique": 3, "electronic warfare": 3,

    # Naval/Marine
    "marine": 3, "naval": 3, "navy": 3, "frégate": 2, "sous-marin": 3, "aéronaval": 3,

    # ISR/Capteurs/Plateformes
    "isr": 2, "reconnaissance": 2, "radar": 2, "sonar": 2, "uav": 3, "drone": 3,

    # Spatial/Comms
    "satellite": 2, "satcom": 2, "constellation": 1,
}

# Thématiques militaires (catégorisation "utile aux forces")
CATS = {
    "Marine": {"marine", "naval", "navy", "frégate", "sous-marin", "aéronaval"},
    "Cyber": {"cyber", "cybersécurité", "cybersecurity", "ransomware", "zero-day", "dfir"},
    "Combat/ISR": {"isr", "targeting", "kill chain", "sensor fusion", "uav", "drone",
                   "radar", "sonar", "ew", "guerre électronique", "electronic warfare"},
    "Soutien/Logistique": {"maintenance", "logistique", "supply chain", "additive manufacturing",
                           "mro", "prognostic", "predictive"},
    "C2/Commandement": {"c2", "c4isr", "command", "control", "interoperability", "joint"},
    "Spatial": {"satellite", "satcom", "constellation", "space force", "orbit", "oisl"},
}

# Traduction offline (Argos EN→FR)
OFFLINE_TRANSLATION = True
ARGOS_FROM_LANG = "en"
ARGOS_TO_LANG = "fr"
TRANSLATION_CACHE_FILE = ".cache/translations.json"

# ------------------------------------------------------------
# Utilitaires texte
# ------------------------------------------------------------

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
    fr_markers = [' le ', ' la ', ' les ', ' un ', ' une ', ' des ', ' du ', ' de ', ' et ', ' est ', ' sont ']
    en_markers = [' the ', ' and ', ' are ', ' was ', ' were ', ' with ', ' from ', ' that ', ' this ']
    fr = sum(m in t for m in fr_markers)
    en = sum(m in t for m in en_markers)
    return "fr" if fr > en else "en" if en > fr else "unknown"

# ------------------------------------------------------------
# Filtrage IA + intérêt militaire
# ------------------------------------------------------------

AI_HINTS = {
    "intelligence artificielle", "ia", " ai", "gpt", "llm", "agent", "agents",
    "machine learning", "deep learning", "ml ", "nlp", "autonome", "autonomous",
    "computer vision", "predictif", "predictive", "genai", "generative"
}

def is_ai_related(title: str, summary: str) -> bool:
    txt = normalize(f"{title} {summary}")[:2000]
    return any(h in txt for h in AI_HINTS)

def military_relevance(title: str, summary: str):
    """Retourne (score_boost, list_categories, ok)"""
    txt = normalize(f"{title} {summary}")
    cats = []
    score = 0
    for cat, keys in CATS.items():
        if any(normalize(k) in txt for k in keys):
            cats.append(cat)
    if cats:
        # bonus de base + petit bonus si >1 catégories
        score = 3 + max(0, len(cats) - 1)
        ok = True
    else:
        ok = False
    return score, cats, ok

# ------------------------------------------------------------
# Scoring générique
# ------------------------------------------------------------

def score_text(title: str, summary: str):
    txt = normalize(f"{title or ''} {summary or ''}")
    tags, score = [], 0
    for k, w in KEYWORDS_WEIGHTS.items():
        if normalize(k) in txt:
            score += w
            tags.append(k)
    # tags uniques
    seen = set()
    tags = [t for t in tags if not (t in seen or seen.add(t))]
    level = "HIGH" if score >= 12 else "MEDIUM" if score >= 6 else "LOW"
    return score, level, tags

# ------------------------------------------------------------
# Dates RSS
# ------------------------------------------------------------

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

# ------------------------------------------------------------
# Déduplication
# ------------------------------------------------------------

def generate_content_hash(title: str, link: str, summary: str) -> str:
    normalized_title = normalize(title)
    normalized_link = link.strip().lower()
    normalized_summary = normalize(summary[:120])
    content = f"{normalized_title}|{normalized_link}|{normalized_summary}"
    return md5(content.encode("utf-8")).hexdigest()[:16]

def calculate_similarity(a: str, b: str) -> float:
    if not a or not b:
        return 0.0
    return SequenceMatcher(None, a.lower(), b.lower()).ratio()

def is_duplicate_article(new_entry: dict, entries: list, threshold: float = 0.92) -> bool:
    h = new_entry["hash"]
    if any(h == e["hash"] for e in entries):
        return True
    for e in entries:
        if calculate_similarity(new_entry["title"], e["title"]) > threshold:
            return True
    return False

# ------------------------------------------------------------
# Traduction offline Argos + cache
# ------------------------------------------------------------

_translation_cache = None
_argos_ready = False

def _ensure_cache_dir():
    Path(".cache").mkdir(parents=True, exist_ok=True)

def load_translation_cache():
    global _translation_cache
    if _translation_cache is not None:
        return _translation_cache
    _ensure_cache_dir()
    p = Path(TRANSLATION_CACHE_FILE)
    if p.exists():
        try:
            _translation_cache = json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            _translation_cache = {}
    else:
        _translation_cache = {}
    return _translation_cache

def save_translation_cache():
    try:
        _ensure_cache_dir()
        Path(TRANSLATION_CACHE_FILE).write_text(
            json.dumps(load_translation_cache(), ensure_ascii=False, indent=2),
            encoding="utf-8"
        )
    except Exception as e:
        print(f"[WARN] Unable to save translation cache: {e}")

def _hash_text(s: str) -> str:
    return md5((s or "").encode("utf-8")).hexdigest()

def init_argos_translator() -> bool:
    global _argos_ready
    if _argos_ready:
        return True
    try:
        import argostranslate.package as pkg
        if ("en", "fr") in {(p.from_code, p.to_code) for p in pkg.get_installed_packages()}:
            _argos_ready = True
            return True
        print("[WARN] Argos EN→FR model not installed.")
        return False
    except Exception as e:
        print(f"[WARN] Argos init failed: {e}")
        return False

def translate_offline_en_to_fr(text: str) -> str:
    if not text or not OFFLINE_TRANSLATION:
        return text
    cache = load_translation_cache()
    h = _hash_text(text)
    if h in cache:
        return cache[h]
    if not init_argos_translator():
        cache[h] = text
        return text
    try:
        import argostranslate.translate as argt
        tr = argt.translate(text, ARGOS_FROM_LANG, ARGOS_TO_LANG)
        if tr and tr != text:
            cache[h] = tr
            return tr
    except Exception as e:
        print(f"[WARN] Offline translation failed: {e}")
    cache[h] = text
    return text

def generate_french_summary(raw_text: str) -> tuple:
    """
    Génère un résumé court FR. Retour: (summary_fr, is_translated, detected_lang)
    """
    if not raw_text:
        return "", False, "unknown"

    clean = re.sub(r"<[^>]+>", " ", raw_text)
    clean = re.sub(r"\s+", " ", clean).strip()
    clean = clean[:MAX_SUMMARY_CHARS_RAW]

    det = detect_language_simple(clean)
    # extractif : 2 phrases
    sentences = re.split(r"(?<=[.!?])\s+", clean)
    sentences = [s.strip() for s in sentences if len(s.strip()) > 10]
    core = " ".join(sentences[:2]) if sentences else clean

    translated = False
    if det == "en" and OFFLINE_TRANSLATION:
        tr = translate_offline_en_to_fr(core)
        if tr and tr != core:
            core = tr
            translated = True

    if len(core) > 280:
        core = core[:279].rsplit(" ", 1)[0] + "…"

    return core, translated, det

# ------------------------------------------------------------
# HTML
# ------------------------------------------------------------

def build_html(df: pd.DataFrame, generated_at: str):
    total = len(df)
    high = int((df["Niveau"] == "HIGH").sum()) if total else 0
    sources_count = df["Source"].nunique() if total else 0
    translated_count = int(df.get("Traduit", pd.Series([False]*total)).sum()) if total else 0

    # lignes
    def level_color(level: str) -> str:
        if level == "HIGH":
            return "bg-red-600"
        if level == "MEDIUM":
            return "bg-orange-600"
        return "bg-green-600"

    row_tpl = (
        "<tr data-level='{level}' data-source='{source}' data-date='{date}'>"
        "<td class='p-3 text-sm text-gray-600'>{date}</td>"
        "<td class='p-3'><span class='bg-blue-100 text-blue-800 px-3 py-1 rounded-full text-xs font-medium'>{source}</span></td>"
        "<td class='p-3'><a href='{link}' target='_blank' class='text-blue-700 hover:text-blue-900 font-semibold hover:underline block'>{title}</a></td>"
        "<td class='p-3 text-sm text-gray-700 summary-cell' title='{summary_raw_esc}'>{summary}{badge}</td>"
        "<td class='p-3 text-center'><span class='bg-indigo-100 text-indigo-800 px-3 py-1 rounded-full text-sm font-bold'>{score}</span></td>"
        "<td class='p-3 text-center'><span class='px-2 py-1 rounded text-white {color}'>{level}</span></td>"
        "<td class='p-3 text-sm'>{tags}</td>"
        "</tr>"
    )

    rows = []
    for _, r in df.iterrows():
        badge = " <span class='translation-badge text-white px-2 py-1 rounded text-xs ml-1'>🇫🇷 Traduit</span>" if r.get("Traduit") else ""
        rows.append(
            row_tpl.format(
                level=r.get("Niveau",""),
                source=r.get("Source",""),
                date=r.get("Date",""),
                link=r.get("Lien",""),
                title=r.get("Titre",""),
                summary=html.escape(r.get("Résumé","")),
                summary_raw_esc=html.escape(r.get("Résumé brut","") or r.get("Résumé","")),
                badge=badge,
                score=int(r.get("Score",0)),
                color=level_color(r.get("Niveau","")),
                tags=html.escape(r.get("Tags","")),
            )
        )
    rows_html = "\n".join(rows) if rows else "<tr><td colspan='7' class='text-center py-6'>Aucune entrée pour la période.</td></tr>"

    html_out = f"""<!doctype html>
<html lang="fr">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Veille IA – Militaire</title>
<link href="https://cdn.jsdelivr.net/npm/tailwindcss@2.2.19/dist/tailwind.min.css" rel="stylesheet">
<style>
  .gradient-bg {{ background: linear-gradient(135deg, #1e3a8a 0%, #3730a3 100%); }}
  .summary-cell {{
    max-height: 4.5rem; overflow: hidden; display: -webkit-box;
    -webkit-line-clamp: 3; -webkit-box-orient: vertical; line-height: 1.5;
  }}
  .translation-badge {{ background: linear-gradient(45deg, #7c3aed, #5b21b6); }}
</style>
</head>
<body class="bg-gray-50">
<header class="gradient-bg text-white py-6">
  <div class="max-w-6xl mx-auto px-4 flex items-center justify-between">
    <div class="flex items-center gap-3">
      <span class="text-2xl">⚓</span>
      <div>
        <h1 class="text-2xl font-bold">Veille IA – Militaire</h1>
        <div class="text-blue-200 text-sm">Fenêtre {DAYS_WINDOW} jours • Généré : {generated_at}</div>
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
      <div class="text-3xl font-bold text-blue-700">{sources_count}</div>
      <div class="text-gray-600">Sources actives</div>
    </div>
    <div class="bg-white rounded shadow p-4 text-center">
      <div class="text-3xl font-bold text-purple-700">{translated_count}</div>
      <div class="text-gray-600">Articles traduits</div>
    </div>
  </div>

  <div class="bg-white rounded shadow p-4 mb-4">
    <div class="grid grid-cols-1 md:grid-cols-4 gap-4">
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
      <select id="days" class="border rounded px-3 py-2">
        <option value="0">Période (toutes)</option>
        <option value="7">7 jours</option>
        <option value="14">14 jours</option>
        <option value="30" selected>30 jours</option>
        <option value="60">60 jours</option>
        <option value="90">90 jours</option>
      </select>
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
        {rows_html}
      </tbody>
    </table>
  </div>
</main>

<script>
const rows   = Array.from(document.querySelectorAll("#tbody tr"));
const q      = document.getElementById("q");
const level  = document.getElementById("level");
const source = document.getElementById("source");
const days   = document.getElementById("days");

function withinPeriod(tr, daysBack) {{
  if (!daysBack || daysBack === "0") return true;
  const d = tr.getAttribute("data-date");
  if (!d) return true;
  const rowTime = new Date(d + "T00:00:00Z").getTime();
  const now     = Date.now();
  const delta   = (now - rowTime) / (1000 * 3600 * 24);
  return delta <= parseInt(daysBack, 10);
}}

function applyFilters() {{
  const qv = (q?.value || "").toLowerCase();
  const lv = level?.value || "";
  const sv = (source?.value || "").toLowerCase();
  const dv = days?.value || "0";

  rows.forEach(tr => {{
    const t  = tr.innerText.toLowerCase();
    const rl = tr.getAttribute("data-level") || "";
    const rs = (tr.getAttribute("data-source") || "").toLowerCase();
    let ok = true;

    if (qv && !t.includes(qv)) ok = false;
    if (lv && rl !== lv) ok = false;
    if (sv && !rs.includes(sv)) ok = false;
    if (!withinPeriod(tr, dv)) ok = false;

    tr.style.display = ok ? "" : "none";
  }});
}}
[q, level, source, days].forEach(el => el && el.addEventListener("input", applyFilters));
applyFilters();

// Export CSV
const data = {export_json};
document.getElementById("btnCsv").addEventListener("click", () => {{
  const header = ["Titre","Lien","Date","Source","Résumé","Niveau","Score","Tags"];
  const rows = data.map(a => [a.titre, a.lien, a.date, a.source, a.resume, a.niveau, a.score, a.tags]);
  const csv = [header, ...rows].map(r => r.map(x => `"${(x||"").replace(/"/g,'""')}"`).join(",")).join("\\n");
  const blob = new Blob([csv], {{type: "text/csv;charset=utf-8"}});
  const url = URL.createObjectURL(blob);
  const a = document.createElement("a");
  a.href = url; a.download = "veille_ia_militaire.csv"; a.click();
  URL.revokeObjectURL(url);
}});
</script>
</body>
</html>
"""
    return html_out

# ------------------------------------------------------------
# Main
# ------------------------------------------------------------

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
        for entry in feed.entries:
            dt = parse_entry_datetime(entry)
            if not dt or dt < cutoff:
                continue
            title = (entry.get("title") or "").strip()
            link  = (entry.get("link") or "").strip()
            raw_summary = strip_html(entry.get("summary") or entry.get("description") or "")
            if not title or not link:
                continue

            # Filtrage : IA obligatoire
            if REQUIRE_AI and not is_ai_related(title, raw_summary):
                continue

            # Pertinence militaire (combat + soutien)
            mil_score, mil_cats, mil_ok = military_relevance(title, raw_summary)
            if MIL_REQUIRED and not mil_ok:
                continue

            # Résumé FR (offline) + score
            summary_fr, is_translated, _ = generate_french_summary(raw_summary)
            base_score, level, tags = score_text(title, summary_fr)
            score = base_score + mil_score

            level = "HIGH" if score >= 12 else "MEDIUM" if score >= 6 else "LOW"
            cats_str = " | ".join(sorted(set(mil_cats)))

            h = generate_content_hash(title, link, summary_fr)
            new = {
                "DateUTC": dt,
                "Date": dt.strftime("%Y-%m-%d"),
                "Source": source_title,
                "Titre": title,
                "Résumé": summary_fr,
                "Résumé brut": raw_summary,
                "Lien": link,
                "Score": int(score),
                "Niveau": level,
                "Tags": cats_str,
                "Traduit": bool(is_translated),
                "hash": h,
            }
            if not is_duplicate_article(new, entries):
                entries.append(new)

    # DataFrame & tri
    if entries:
        df = pd.DataFrame(entries).sort_values(by=["Score", "DateUTC"], ascending=[False, False])
    else:
        df = pd.DataFrame(columns=["DateUTC","Date","Source","Titre","Résumé","Résumé brut","Lien","Score","Niveau","Tags","Traduit","hash"])

    # Export JSON (pour CSV)
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

    # HTML
    generated_at = now_utc.strftime("%Y-%m-%d %H:%M UTC")
    html_out = build_html(df, generated_at)
    html_out = html_out.replace("{export_json}", export_json)

    with open(os.path.join(OUT_DIR, OUT_FILE), "w", encoding="utf-8") as f:
        f.write(html_out)

    save_translation_cache()
    print(f"OK • {len(df)} articles • écrit : {OUT_DIR}/{OUT_FILE}")

if __name__ == "__main__":
    main()
