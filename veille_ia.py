#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Veille IA – Militaire (Marine) – full web + résumés FR offline (Argos)
- Fenêtre glissante configurable (DAYS_WINDOW)
- IA OBLIGATOIRE + Défense/Marine/Cyber OBLIGATOIRE (plus robuste)
- Traduction offline EN→FR via Argos si OFFLINE_TRANSLATION=1
- Scoring contextuel + pertinence (seuil RELEVANCE_MIN)
- UI Tailwind, export CSV
"""

import os
import re
import html as htmllib
import unicodedata
import calendar
import time
from hashlib import md5
from datetime import datetime, timezone, timedelta
import urllib.request
import feedparser

# ========================== Configuration ==========================

DAYS_WINDOW = int(os.getenv("DAYS_WINDOW", "30"))
OUT_DIR = "docs"
OUT_FILE = "index.html"
MAX_SUMMARY_FR_CHARS = int(os.getenv("MAX_SUMMARY_FR_CHARS", "280"))
OFFLINE_TRANSLATION = os.getenv("OFFLINE_TRANSLATION", "0") in {"1", "true", "True"}
RELEVANCE_MIN = float(os.getenv("RELEVANCE_MIN", "0.55"))
HALF_LIFE_DAYS = int(os.getenv("HALF_LIFE_DAYS", "15"))
UA = "VeilleIA/1.0 (+https://github.com/guillaume7625/veille-ia-marine)"

RSS_FEEDS = {
    "ActuIA": "https://www.actuia.com/feed/",
    "Numerama": "https://www.numerama.com/feed/",
    "AI News | VentureBeat": "https://venturebeat.com/category/ai/feed/",
    "C4ISRNet": "https://www.c4isrnet.com/arc/outboundfeeds/rss/",
    "Breaking Defense": "https://breakingdefense.com/feed/",
    "Naval Technology": "https://www.naval-technology.com/feed/",
    "Cybersecurity Dive - Latest News": "https://www.cybersecuritydive.com/feeds/news/",
}

# Sources explicitement "défense"
DEFENSE_SOURCES = {
    "C4ISRNet", "Breaking Defense", "Defense News", "Defense One",
    "Jane's Defence", "Naval Technology", "Naval News",
}

# Forçage traduction EN pour ces sources
EN_SOURCES = {
    "AI News | VentureBeat", "VentureBeat AI", "VentureBeat",
    "Breaking Defense", "Defense News", "Defense One",
    "C4ISRNet", "Naval Technology", "Cybersecurity Dive - Latest News",
}

# Pondérations basiques pour l'ancien score (affiché dans la colonne Score)
KEYWORDS_WEIGHTS = {
    # IA
    "artificial intelligence": 4, "intelligence artificielle": 4, "ia": 3, "ai": 3,
    "machine learning": 3, "apprentissage": 2, "deep learning": 3,
    "algorithme": 2, "transformer": 2, "llm": 3, "génératif": 2, "generative": 2,
    "agent": 2, "multi-agent": 2, "vision": 2, "nlp": 2, "inférence": 2, "inference": 2,
    # Défense/Marine/Cyber
    "marine": 5, "naval": 5, "navy": 5, "navire": 3, "frégate": 4, "sous-marin": 5, "maritime": 3,
    "armée": 3, "defense": 4, "defence": 4, "défense": 4, "pentagon": 4, "dod": 4,
    "otan": 4, "nato": 4, "air force": 4, "space force": 4, "usaf": 4, "usn": 4, "usmc": 4, "raf": 4,
    "cyber": 4, "cybersécurité": 4, "cyberdéfense": 5,
    "radar": 3, "sonar": 4, "drone": 4, "uav": 4, "aéronaval": 5,
    "brouillage": 4, "guerre électronique": 5, "electronic warfare": 5,
    "satellite": 3, "reconnaissance": 3, "isr": 4, "c4isr": 5, "c2": 4, "command": 3,
    "logistique": 3, "maintenance": 3, "mco": 3, "supply chain": 2,
    "entraînement": 2, "training": 2, "interoperability": 2, "readiness": 2, "modernisation": 2,
}

# IA obligatoire : on élargit
AI_HINTS = {
    "ia", "intelligence artificielle", "artificial intelligence",
    "ai", "ai/ml", "machine learning", "machine-learning", "apprentissage",
    "deep learning", "deep-learning", "algorithme", "transformer", "llm",
    "génératif", "generative", "agent", "multi-agent", "autonomous", "autonomy",
    "nlp", "vision", "inférence", "inference",
}

# Défense obligatoire : on inclut davantage d’anglais
DEFENSE_HINTS = {
    # français
    "marine", "naval", "navy", "frégate", "sous-marin", "sonar", "radar",
    "drone", "uav", "missile", "aéronaval", "c4isr", "isr", "ew", "guerre électronique",
    "otan", "nato", "armée", "forces", "c2", "command",
    "logistique", "maintenance", "mco", "supply chain", "entraînement", "training",
    "interoperability", "readiness", "modernisation",
    "cyber", "cybersécurité", "cyberdéfense", "ransomware", "intrusion",
    # anglais générique défense
    "defense", "defence", "military", "warfighter", "warfare", "pentagon", "dod",
    "air force", "space force", "army", "marine corps", "marines", "usaf", "usn", "usmc",
    "royal navy", "raf", "naval forces", "fleet", "task force",
}

# Anti-bruit
EXCLUSION_PATTERNS = [
    r"\b(deal|promo|bon\s?plan|meilleur prix|précommander?)\b",
    r"\b(gaming|jeu(x)? vidéo|streaming|people|cinéma)\b",
    r"\b(rumeur|leak|spoiler)\b",
    r"\b(smartphone|gadget|wearable)\b",
]

SOURCE_WEIGHTS = {
    "C4ISRNet": 1.18,
    "Breaking Defense": 1.18,
    "Naval Technology": 1.12,
    "AI News | VentureBeat": 1.05,
    "VentureBeat AI": 1.05,
    "Numerama": 1.00,
    "ActuIA": 1.00,
}

# ========================== Utilitaires ==========================

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

def contains_any(text: str, vocab: set[str]) -> bool:
    t = normalize(text)
    return any(k in t for k in vocab)

def entry_hash(title: str, link: str) -> str:
    return md5(f"{title}|{link}".encode("utf-8")).hexdigest()

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
                from email.utils import parsedate_to_datetime
                dt = parsedate_to_datetime(s)
                if not dt.tzinfo:
                    dt = dt.replace(tzinfo=timezone.utc)
                return dt.astimezone(timezone.utc)
            except Exception:
                pass
    return None

def parse_rss_with_headers(url: str, retries: int = 2, timeout: int = 25):
    for i in range(retries + 1):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": UA})
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                data = resp.read()
            return feedparser.parse(data)
        except Exception as e:
            if i == retries:
                print(f"[ERROR] RSS fetch failed ({url}): {e}")
                return feedparser.FeedParserDict(feed={}, entries=[])
            time.sleep(1.5 * (i + 1))

def detect_language_simple(text: str) -> str:
    if not text:
        return "unknown"
    t = " " + text.lower() + " "
    fr = [' le ', ' la ', ' les ', ' un ', ' une ', ' des ', ' du ', ' de ', ' qui ', ' que ', ' où ', ' est ', ' sont ']
    en = [' the ', ' and ', ' with ', ' from ', ' that ', ' this ', ' which ', ' what ', ' where ', ' when ', ' how ']
    f = sum(1 for m in fr if m in t)
    e = sum(1 for m in en if m in t)
    if f > e: return "fr"
    if e > f: return "en"
    return "unknown"

def translate_offline_en_to_fr(text: str) -> str:
    if not text or not OFFLINE_TRANSLATION:
        return text
    try:
        from argostranslate import translate as argos_translate
        # (1.9) charge les langues installées
        argos_translate.load_installed_languages()
        out = argos_translate.translate(text, "en", "fr")
        return out if out else text
    except Exception as e:
        print(f"[WARN] Argos translate failed: {e}")
        return text

def generate_french_summary(raw_text: str, max_chars: int = 280, *, force_en: bool = False):
    if not raw_text:
        return "", False, "unknown"
    clean = strip_html(raw_text)
    lang = detect_language_simple(clean)
    sentences = re.split(r"(?<=[.!?])\s+", clean)
    sentences = [s.strip() for s in sentences if len(s.strip()) > 15]
    base = " ".join(sentences[:2]) if sentences else clean
    if not force_en and lang == "fr":
        summary = base
        translated = False
    else:
        summary_en = base
        summary_fr = translate_offline_en_to_fr(summary_en)
        translated = (summary_fr.strip() != summary_en.strip())
        summary = summary_fr
    if len(summary) > max_chars:
        summary = summary[:max_chars - 1].rsplit(" ", 1)[0] + "…"
    return summary, translated, lang

# ====================== Scoring / Pertinence ======================

def split_sentences(txt: str) -> list[str]:
    if not txt: return []
    parts = re.split(r'(?<=[\.\!\?])\s+', txt)
    return [p.strip() for p in parts if len(p.strip()) > 0]

IA_CONTEXT = {
    "core": {"ia","intelligence artificielle","artificial intelligence","ai","machine learning","apprentissage","deep learning"},
    "applications": {"computer vision","nlp","reconnaissance","prédiction","anomaly"},
    "techniques": {"transformer","neuronal","neural","algorithme","fine-tuning","inférence","inference"},
    "emerging": {"llm","génératif","generative","multimodal","agent","multi-agent","edge computing","autonomous","autonomy"},
}
DEF_CONTEXT = {
    "operations": {"c4isr","isr","warfare","mission","tactical","command","c2","joint","pentagon","dod"},
    "plateformes": {"naval","marine","navy","uav","drone","frégate","sous-marin","sonar","radar","missile","aéronaval","air force","space force","army","usaf","usn","usmc"},
    "support": {"logistique","maintenance","mco","supply chain","training","entraînement","interoperability","readiness","modernisation"},
    "cyber": {"cyber","cybersécurité","ransomware","intrusion","zero-day","xdr","edr","soc","threat intelligence"},
}

def keyword_density(text: str, groups: dict[str,set[str]]) -> float:
    if not text: return 0.0
    t = normalize(text)
    words = max(50, len(t.split()))
    score = 0.0
    for weight, (_, terms) in enumerate(groups.items(), start=1):
        for k in terms:
            if k in t:
                score += 0.6 * weight
    return min(1.0, score / words * 8.0)

def co_occurrence_bonus(text: str) -> float:
    if not text: return 1.0
    bonus = 1.0
    for s in split_sentences(text):
        t = normalize(s)
        ia = any(k in t for g in IA_CONTEXT.values() for k in g)
        df = any(k in t for g in DEF_CONTEXT.values() for k in g)
        if ia and df:
            bonus += 0.08
    return min(bonus, 1.4)

def source_authority(src: str) -> float:
    w = SOURCE_WEIGHTS.get(src, 1.0)
    # petit coup de pouce si source défense
    if src in DEFENSE_SOURCES:
        w *= 1.08
    return w

def temporal_relevance(dt: datetime) -> float:
    if not isinstance(dt, datetime): return 0.9
    age_days = max(0.0, (datetime.now(timezone.utc) - dt).total_seconds() / 86400.0)
    return max(0.6, 0.5 ** (age_days / max(1, HALF_LIFE_DAYS)))

def matches_exclusion(text: str) -> bool:
    t = normalize(text)
    for pat in EXCLUSION_PATTERNS:
        if re.search(pat, t):
            def_ctx = any(k in t for g in DEF_CONTEXT.values() for k in g)
            if not def_ctx:
                return True
    return False

def calculate_relevance_score(text: str, src: str, dt: datetime) -> float:
    kd_ia = keyword_density(text, IA_CONTEXT)
    kd_def = keyword_density(text, DEF_CONTEXT)
    dens = (kd_ia * 0.6 + kd_def * 0.4)
    bonus = co_occurrence_bonus(text)
    srcw = source_authority(src)
    fresh = temporal_relevance(dt)
    score = dens * bonus * srcw * fresh
    return max(0.0, min(1.5, score))

# ========================== Catégorisation ==========================

def classify_category(text: str) -> str:
    t = normalize(text)
    if any(k in t for k in {"doctrine","policy","réglementation","regulation","budget","contract","contrat","procurement","acquisition"}):
        return "POLICY"
    if any(k in t for k in {"prototype","test","trial","essai","r&d","laboratoire","lab"}):
        return "DEVELOPMENT"
    if any(k in t for k in {"déployé","deployment","fielded","opérationnel","exercise","exercice","retour d'expérience","retour terrain"}):
        return "OPERATIONAL"
    if any(k in t for k in {"menace","threat","intrusion","ransomware","ew","electronic warfare","counter-uas","counter uas"}):
        return "THREAT"
    if any(k in t for k in {"partnership","alliance","accord","coopération","framework","mou","moa"}):
        return "PARTNERSHIP"
    if any(k in t for k in {"breakthrough","rupture","sota","state of the art","record","unprecedented"}):
        return "BREAKTHROUGH"
    return "DEVELOPMENT"

def generate_smart_tags(text: str) -> str:
    t = normalize(text)
    tags = set()
    m = re.search(r"\btrl\s?([1-9])\b", t)
    if m: tags.add(f"TRL{m.group(1)}")
    if any(k in t for k in DEF_CONTEXT["cyber"]): tags.add("Cyber")
    if any(k in t for k in DEF_CONTEXT["plateformes"]): tags.add("Naval/Plateformes")
    if any(k in t for k in DEF_CONTEXT["support"]): tags.add("Soutien/Log")
    if any(k in t for k in DEF_CONTEXT["operations"]): tags.add("C2/ISR")
    if any(k in t for k in IA_CONTEXT["emerging"]): tags.add("Génératif/LLM")
    if any(k in t for k in IA_CONTEXT["applications"]): tags.add("Applications")
    if any(k in t for k in IA_CONTEXT["techniques"]): tags.add("Techniques")
    return ", ".join(sorted(tags)) or "—"

# ===================== Score/Level (héritage) =====================

def compute_score(title: str, summary_fr: str):
    txt = normalize(f"{title or ''} {summary_fr or ''}")
    score = 0
    for k, w in KEYWORDS_WEIGHTS.items():
        if k in txt: score += w
    level = "HIGH" if score >= 9 else "MEDIUM" if score >= 5 else "LOW"
    return score, level

# ===================== Logique Défense Contextuelle =====================

_LOOSE_DEF_PAT = re.compile(
    r"\b(defen[cs]e|military|warfighter|warfare|pentagon|u\.?s\.?\s?(navy|army|air\s?force|space\s?force)|"
    r"usaf|usn|usmc|raf|royal\s?navy|dod)\b"
)

def is_defense_context(text_all: str, source_title: str) -> bool:
    """Retourne True si le contexte 'défense' est probable même sans mot-clé strict."""
    t = normalize(text_all)
    if contains_any(text_all, DEFENSE_HINTS):
        return True
    # Si source pure défense et on parle d'IA → OK
    if source_title in DEFENSE_SOURCES and contains_any(text_all, AI_HINTS):
        return True
    # Patrons anglais fréquents
    if _LOOSE_DEF_PAT.search(t):
        return True
    return False

# ============================ HTML ============================

def build_html(items: list[dict]):
    total = len(items)
    high = sum(1 for x in items if x["level"] == "HIGH")
    sources_count = len({x["source"] for x in items})
    translated_count = sum(1 for x in items if x["translated"])
    cats = sorted({x["category"] for x in items})

    def lv_badge(lv: str) -> str:
        return {"HIGH": "bg-red-600", "MEDIUM": "bg-orange-600", "LOW": "bg-green-600"}.get(lv, "bg-gray-500")

    rows = []
    for e in items:
        tr_badge = ' <span class="ml-2 px-2 py-0.5 rounded text-xs text-white" style="background:#6d28d9">🇫🇷 Traduit</span>' if e["translated"] else ""
        rows.append(
            f"<tr class='hover:bg-gray-50' data-level='{e['level']}' data-source='{htmllib.escape(e['source'])}' data-cat='{e['category']}'>"
            f"<td class='p-3 text-sm text-gray-600'>{e['date']}</td>"
            f"<td class='p-3 text-xs'><span class='bg-blue-100 text-blue-800 px-2 py-1 rounded'>{htmllib.escape(e['source'])}</span></td>"
            f"<td class='p-3'><a class='text-blue-700 hover:underline font-semibold' target='_blank' href='{htmllib.escape(e['link'])}'>{htmllib.escape(e['title'])}</a></td>"
            f"<td class='p-3 text-sm text-gray-800'>{htmllib.escape(e['summary'])}{tr_badge}</td>"
            f"<td class='p-3 text-center'><span class='bg-indigo-100 text-indigo-800 px-2 py-1 rounded text-sm font-bold'>{e['score']}</span></td>"
            f"<td class='p-3 text-center'><span class='text-white px-2 py-1 rounded text-xs {lv_badge(e['level'])}'>{e['level']}</span></td>"
            f"<td class='p-3 text-sm'><span class='px-2 py-1 rounded text-white text-xs' style='background:#0f766e'>{e['category']}</span></td>"
            f"<td class='p-3 text-center text-sm'><span class='bg-gray-100 text-gray-800 px-2 py-1 rounded'>{e['rscore']}</span></td>"
            f"<td class='p-3 text-sm'>{htmllib.escape(e['tags'])}</td>"
            "</tr>"
        )

    generated = datetime.utcnow().strftime("%Y-%m-%d %H:%M UTC")
    options_html = "".join(f"<option value='{c}'>{c}</option>" for c in cats)

    html_page = f"""<!doctype html>
<html lang="fr"><head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>Veille IA – Militaire</title>
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
  <div class="max-w-7xl mx-auto px-4 py-6 flex items-center justify-between">
    <div class="flex items-center gap-3">
      <span class="text-2xl">⚓</span>
      <div>
        <h1 class="text-2xl font-bold">Veille IA – Militaire</h1>
        <div class="text-blue-200 text-sm">Fenêtre {DAYS_WINDOW} jours • Généré : {generated}</div>
      </div>
    </div>
    <div class="flex items-center gap-2">
      <button id="btnCsv" class="bg-blue-600 hover:bg-blue-500 px-4 py-2 rounded">Exporter CSV</button>
    </div>
  </div>
</header>

<main class="max-w-7xl mx-auto px-4 py-6">
  <div class="grid grid-cols-1 md:grid-cols-5 gap-4 mb-6">
    <div class="bg-white rounded shadow p-4 text-center">
      <div class="text-3xl font-bold text-blue-700">{total}</div>
      <div class="text-gray-600">Articles</div>
    </div>
    <div class="bg-white rounded shadow p-4 text-center">
      <div class="text-3xl font-bold text-red-600">{sum(1 for x in items if x["level"] == "HIGH")}</div>
      <div class="text-gray-600">Priorité Haute</div>
    </div>
    <div class="bg-white rounded shadow p-4 text-center">
      <div class="text-3xl font-bold text-green-600">{sources_count}</div>
      <div class="text-gray-600">Sources actives</div>
    </div>
    <div class="bg-white rounded shadow p-4 text-center">
      <div class="text-3xl font-bold text-purple-600">{translated_count}</div>
      <div class="text-gray-600">Traduit FR</div>
    </div>
    <div class="bg-white rounded shadow p-4 text-center">
      <div class="text-sm text-gray-600">Seuil pertinence</div>
      <div class="text-xl font-semibold">{RELEVANCE_MIN}</div>
    </div>
  </div>

  <div class="bg-white rounded shadow p-4 mb-4">
    <div class="grid grid-cols-1 md:grid-cols-4 gap-4">
      <input id="q" type="search" placeholder="Recherche (titre, résumé, tags)…" class="border rounded px-3 py-2">
      <select id="level" class="border rounded px-3 py-2">
        <option value="">Niveau (tous)</option>
        <option value="HIGH">HIGH</option>
        <option value="MEDIUM">MEDIUM</option>
        <option value="LOW">LOW</option>
      </select>
      <input id="source" type="search" placeholder="Filtrer par source…" class="border rounded px-3 py-2">
      <select id="cat" class="border rounded px-3 py-2">
        <option value="">Catégorie (toutes)</option>
        {options_html}
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
          <th class="text-left p-3">Catégorie</th>
          <th class="text-left p-3">Pertinence</th>
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
  const cat = document.getElementById("cat");

  function applyFilters() {{
    const qv = (q.value || "").toLowerCase();
    const lv = level.value;
    const sv = (source.value || "").toLowerCase();
    const cv = cat.value;
    rows.forEach(tr => {{
      const t = tr.innerText.toLowerCase();
      const rl = tr.getAttribute("data-level") || "";
      const rs = (tr.getAttribute("data-source") || "").toLowerCase();
      const rc = tr.getAttribute("data-cat") || "";
      let ok = true;
      if (qv && !t.includes(qv)) ok = false;
      if (lv && rl !== lv) ok = false;
      if (sv && !rs.includes(sv)) ok = false;
      if (cv && rc !== cv) ok = false;
      tr.style.display = ok ? "" : "none";
    }});
  }}
  [q, level, source, cat].forEach(el => el.addEventListener("input", applyFilters));

  document.getElementById("btnCsv").addEventListener("click", () => {{
    const header = ["Titre","Lien","Date","Source","Résumé","Niveau","Score","Catégorie","Pertinence","Tags"];
    const table = document.querySelector("#tbody");
    const data = [];
    for (const tr of table.querySelectorAll("tr")) {{
      if (tr.style.display === "none") continue;
      const tds = tr.querySelectorAll("td");
      if (tds.length < 9) continue;
      const titre = tds[2].innerText.trim();
      const lienA = tds[2].querySelector("a");
      const lien = lienA ? lienA.getAttribute("href") : "";
      const row = [
        titre, lien,
        tds[0].innerText.trim(),
        tds[1].innerText.trim(),
        tds[3].innerText.trim(),
        tds[5].innerText.trim(),
        tds[4].innerText.trim(),
        tds[6].innerText.trim(),
        tds[7].innerText.trim(),
        tds[8].innerText.trim()
      ];
      data.push(row);
    }}
    const csv = [header, ...data]
      .map(r => r.map(x => '"' + String((x==null ? "" : x)).replace(/"/g,'""') + '"').join(","))
      .join("\\n");
    const blob = new Blob([csv], {{type: "text/csv;charset=utf-8"}});
    const url = URL.createObjectURL(blob);
    const a = document.createElement("a");
    a.href = url; a.download = "veille_ia_militaire.csv"; a.click();
    URL.revokeObjectURL(url);
  }});
}})();
</script>
</body></html>
"""
    os.makedirs(OUT_DIR, exist_ok=True)
    with open(os.path.join(OUT_DIR, OUT_FILE), "w", encoding="utf-8") as f:
        f.write(html_page)
    print(f"[OK] Généré: {OUT_DIR}/{OUT_FILE} – {total} entrées – sources actives: {sources_count}")

# ============================ Main ============================

def main():
    print(f"[DEBUG] OFFLINE_TRANSLATION={OFFLINE_TRANSLATION}, DAYS_WINDOW={DAYS_WINDOW}, RELEVANCE_MIN={RELEVANCE_MIN}")
    os.makedirs(OUT_DIR, exist_ok=True)
    cutoff = datetime.now(timezone.utc) - timedelta(days=DAYS_WINDOW)

    items = []
    seen_set = set()

    total_seen = 0
    total_kept = 0

    for src_name, url in RSS_FEEDS.items():
        print(f"📡 {src_name} -> {url}")
        feed = parse_rss_with_headers(url)
        source_title = feed.feed.get("title", src_name)

        seen = ai_ok = def_ok = kept = 0

        for e in feed.entries:
            seen += 1; total_seen += 1
            dt = parse_entry_datetime(e)
            if not dt or dt < cutoff:
                continue

            title = (e.get("title") or "").strip()
            link = (e.get("link") or "").strip()
            raw = e.get("summary") or e.get("description") or ""
            if not title or not link:
                continue

            text_all = f"{title}. {strip_html(raw)}"

            if matches_exclusion(text_all):
                print(f"[DEBUG FILTER] Exclusion (bruit) : {title}")
                continue

            if not contains_any(text_all, AI_HINTS):
                print(f"[DEBUG FILTER] Pas IA : {title}")
                continue
            ai_ok += 1

            if not is_defense_context(text_all, source_title):
                print(f"[DEBUG FILTER] Pas DEF/MAR/CYB : {title}")
                continue
            def_ok += 1

            h = entry_hash(title, link)
            if h in seen_set:
                continue
            seen_set.add(h)

            force = (source_title in EN_SOURCES) or (src_name in EN_SOURCES)
            summary_fr, translated, _ = generate_french_summary(raw, max_chars=MAX_SUMMARY_FR_CHARS, force_en=force)

            rel = calculate_relevance_score(text_all, source_title, dt)
            if rel < RELEVANCE_MIN:
                print(f"[DEBUG FILTER] Pertinence {rel:.3f} < {RELEVANCE_MIN} : {title}")
                continue

            cat = classify_category(text_all)
            tags = generate_smart_tags(text_all)
            score, level = compute_score(title, summary_fr)

            items.append(dict(
                date=dt.strftime("%Y-%m-%d"),
                source=source_title,
                title=title,
                link=link,
                summary=summary_fr,
                translated=translated,
                score=score,
                level=level,
                rscore=round(rel, 3),
                category=cat,
                tags=tags
            ))
            kept += 1; total_kept += 1

        print(f"[SRC] {src_name} -> vus:{seen} IA_ok:{ai_ok} DEF_ok:{def_ok} gardés:{kept}")

    items.sort(key=lambda x: (x["rscore"], x["date"], x["score"]), reverse=True)
    print("=== DEBUG RECAP ===")
    for name, url in RSS_FEEDS.items():
        # Ce bloc ne connaît plus les vus par source après la boucle,
        # mais on a déjà imprimé la ligne [SRC] ci-dessus pour chaque source.
        pass
    print(f"Articles récupérés (tous flux) : {total_seen}")
    print(f"Articles conservés (après filtres) : {total_kept}")

    build_html(items)

if __name__ == "__main__":
    main()
