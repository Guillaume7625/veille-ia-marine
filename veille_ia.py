#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
Veille IA – Militaire : collecte, filtrage IA+Défense, traduction FR (Argos),
scoring contextuel, tags, génération HTML.
Sortie : docs/index.html
"""

from __future__ import annotations

import os
import re
import html
import time
import logging
import hashlib
import unicodedata
import calendar
from pathlib import Path
from dataclasses import dataclass
from typing import List, Dict, Optional, Set
from datetime import datetime, timezone, timedelta

import requests
import feedparser
from dateutil import parser as date_parser

# ========================= Config =========================

@dataclass
class Config:
    days_window: int = int(os.getenv("DAYS_WINDOW", "45"))
    relevance_min: float = float(os.getenv("RELEVANCE_MIN", "0.18"))
    max_summary_chars: int = int(os.getenv("MAX_SUMMARY_CHARS", "320"))
    offline_translation: bool = os.getenv("OFFLINE_TRANSLATION", "0") == "1"
    output_dir: Path = Path("docs")
    output_file: str = "index.html"
    request_timeout: int = 25
    max_retries: int = 3
    user_agent: str = "VeilleIA-Military/2.0 (+https://github.com/guillaume7625/veille-ia-marine)"

config = Config()

# ========================= Sources =========================

RSS_SOURCES: Dict[str, Dict] = {
    "ActuIA": {
        "url": "https://www.actuia.com/feed/",
        "language": "fr",
        "authority": 1.0,
    },
    "Numerama": {
        "url": "https://www.numerama.com/feed/",
        "language": "fr",
        "authority": 0.9,
    },
    "VentureBeat AI": {
        "url": "https://venturebeat.com/category/ai/feed/",
        "language": "en",
        "authority": 1.1,
    },
    "C4ISRNet": {
        "url": "https://www.c4isrnet.com/arc/outboundfeeds/rss/",
        "language": "en",
        "authority": 1.2,
    },
    "Breaking Defense": {
        "url": "https://breakingdefense.com/feed/",
        "language": "en",
        "authority": 1.15,
    },
    "Naval Technology": {
        "url": "https://www.naval-technology.com/feed/",
        "language": "en",
        "authority": 1.1,
    },
    "Cybersecurity Dive": {
        "url": "https://www.cybersecuritydive.com/feeds/news/",
        "language": "en",
        "authority": 1.05,
    },
}

# ========================= Vocabulaire / filtres =========================

# Mots-clés IA (noyau + techniques + usages)
AI_TERMS = {
    "intelligence artificielle", "ia", "ai", "artificial intelligence",
    "machine learning", "apprentissage automatique", "apprentissage",
    "deep learning", "neural network", "réseau neuronal",
    "transformer", "inference", "inférence",
    "llm", "large language model", "gpt", "generative", "génératif",
    "computer vision", "vision", "nlp", "natural language processing",
    "agent", "agents", "multi-agent", "autonomous", "autonome"
}

# Mots-clés Défense/Naval/Cyber/C2/ISR/log
DEF_TERMS = {
    # plateformes / opérations
    "marine", "naval", "navy", "maritime", "frégate", "destroyer",
    "sous-marin", "submarine", "porte-avions", "aircraft carrier",
    "drone", "uav", "uas", "uuv", "usv", "essaim", "swarm",
    "radar", "sonar", "lidar", "missile", "torpedo",
    "ew", "guerre électronique", "electronic warfare", "jamming",
    "c4isr", "c2", "isr", "command", "control",
    "warfighter", "battlefield", "tactical", "mission",

    # cyber
    "cyber", "cybersécurité", "cybersecurity", "ransomware", "intrusion", "apt",
    "soc", "xdr", "edr", "zero-day", "threat intelligence",

    # soutien/log/doctrine
    "logistique", "maintenance", "mco", "supply chain", "soutien",
    "training", "entraînement", "readiness", "interoperability",
    "modernisation", "doctrine", "policy", "budget", "procurement", "acquisition", "contract",
    "otan", "nato", "armée", "forces", "defense", "défense", "military", "pentagon"
}

# Poids simples par mots-clés pour le score "hérité"
KEYWORDS_WEIGHTS = {
    # IA
    "intelligence artificielle": 4, "ia": 3, "ai": 3,
    "machine learning": 3, "apprentissage": 2, "deep learning": 3,
    "algorithme": 2, "transformer": 2, "llm": 3, "génératif": 2, "generative": 2,
    "agent": 2, "multi-agent": 2, "vision": 2, "nlp": 2, "inférence": 2, "inference": 2,
    # Défense/Marine/Cyber
    "marine": 5, "naval": 5, "navy": 5, "navire": 3, "frégate": 4, "sous-marin": 5, "maritime": 3,
    "armée": 3, "defense": 4, "défense": 4, "military": 4, "pentagon": 4, "otan": 4, "nato": 4,
    "cyber": 4, "cybersécurité": 4, "cyberdéfense": 5,
    "radar": 3, "sonar": 4, "drone": 4, "uav": 4, "uas": 4, "uuv": 4, "usv": 4,
    "ew": 4, "guerre électronique": 5, "jamming": 3, "satellite": 3, "reconnaissance": 3,
    "c4isr": 5, "isr": 4, "c2": 4, "command": 3,
    "logistique": 3, "maintenance": 3, "mco": 3, "supply chain": 2,
    "entraînement": 2, "training": 2, "interoperability": 2, "readiness": 2, "modernisation": 2,
}

# Exclusions (bruit B2C / gaming / promo…)
EXCLUSION_PATTERNS = [
    r"\b(deal|promo|bon\s?plan|réduction|discount|sale|meilleur prix|précommander?)\b",
    r"\b(gaming|jeu(x)?\s?vidéo|streaming|people|cinéma|entertainment)\b",
    r"\b(smartphone|tablet|gadget|wearable|grand public|consumer)\b",
    r"\b(rumeur|leak|spoiler|speculation|gossip|celebrity)\b",
]

SOURCE_WEIGHTS = {
    "C4ISRNet": 1.20,
    "Breaking Defense": 1.15,
    "Naval Technology": 1.10,
    "VentureBeat AI": 1.05,
    "ActuIA": 1.00,
    "Numerama": 0.95,
}

# ========================= Logging =========================

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s",
    handlers=[logging.StreamHandler(), logging.FileHandler("veille.log", encoding="utf-8")]
)
logger = logging.getLogger(__name__)

# ========================= Utilitaires texte =========================

def normalize(text: str) -> str:
    if not text:
        return ""
    t = text.lower()
    t = unicodedata.normalize("NFKD", t)
    t = "".join(c for c in t if not unicodedata.combining(c))
    return t

def clean_html(text: str) -> str:
    if not text:
        return ""
    t = re.sub(r"<[^>]+>", " ", text)
    t = re.sub(r"\s+", " ", t)
    return t.strip()

def detect_language_simple(text: str) -> str:
    if not text:
        return "unknown"
    t = f" {text.lower()} "
    fr = [' le ', ' la ', ' les ', ' un ', ' une ', ' des ', ' du ', ' de ',
          ' qui ', ' que ', ' est ', ' sont ', ' avec ', ' dans ', ' pour ', ' sur ']
    en = [' the ', ' and ', ' with ', ' from ', ' that ', ' this ', ' which ',
          ' what ', ' where ', ' when ', ' how ', ' why ', ' can ', ' will ', ' would ', ' should ']
    fs = sum(1 for m in fr if m in t)
    es = sum(1 for m in en if m in t)
    if fs > es: return "fr"
    if es > fs: return "en"
    return "unknown"

# ========================= Traduction (Argos) =========================

class TranslationService:
    def __init__(self):
        self.available = False
        self._translator = None
        self._setup()

    def _setup(self):
        if not config.offline_translation:
            logger.info("Traduction offline désactivée (OFFLINE_TRANSLATION=0)")
            return
        try:
            from argostranslate import translate as argos_translate
            langs = argos_translate.get_installed_languages()
            en = next((l for l in langs if l.code == "en"), None)
            fr = next((l for l in langs if l.code == "fr"), None)
            if en and fr:
                self._translator = en.get_translation(fr)
                self.available = self._translator is not None
                logger.info("✅ Argos EN→FR prêt")
            else:
                logger.warning("⚠️ Modèles Argos EN/FR absents (le workflow doit les installer).")
        except Exception as e:
            logger.warning(f"⚠️ Argos indisponible: {e}")

    def translate_en_to_fr(self, text: str) -> tuple[str, bool]:
        if not text or not self.available or self._translator is None:
            return text, False
        try:
            out = self._translator.translate(text)
            if out and out.strip() != text.strip():
                return out, True
            return text, False
        except Exception as e:
            logger.warning(f"Erreur traduction: {e}")
            return text, False

# ========================= Analyse/Scoring =========================

def compute_keyword_score(text_norm: str) -> int:
    score = 0
    for k, w in KEYWORDS_WEIGHTS.items():
        if normalize(k) in text_norm:
            score += w
    return score

def classify_category(text_norm: str) -> str:
    if any(x in text_norm for x in ["policy", "doctrine", "réglementation", "regulation", "budget", "contract", "procurement", "acquisition"]):
        return "POLICY"
    if any(x in text_norm for x in ["deployment", "fielded", "operational", "opérationnel", "exercise", "exercice"]):
        return "OPERATIONAL"
    if any(x in text_norm for x in ["threat", "menace", "ransomware", "ew", "electronic warfare", "counter-uas", "counter uas"]):
        return "THREAT"
    if any(x in text_norm for x in ["prototype", "test", "trial", "essai", "lab", "laboratoire", "r&d"]):
        return "DEVELOPMENT"
    if any(x in text_norm for x in ["alliance", "partnership", "coopération", "framework", "mou"]):
        return "PARTNERSHIP"
    return "TECHNOLOGY"

def generate_tags(text_norm: str) -> List[str]:
    tags = set()
    if any(t in text_norm for t in ["llm", "gpt", "transformer", "génératif", "generative"]):
        tags.add("LLM/Génératif")
    if any(t in text_norm for t in ["computer vision", "vision", "image"]):
        tags.add("Vision Artificielle")
    if any(t in text_norm for t in ["drone", "uav", "autonomous", "autonome", "usv", "uuv"]):
        tags.add("Systèmes Autonomes")
    if any(t in text_norm for t in ["naval", "marine", "maritime", "submarine"]):
        tags.add("Naval")
    if any(t in text_norm for t in ["cyber", "ransomware", "malware", "soc"]):
        tags.add("Cybersécurité")
    if any(t in text_norm for t in ["c4isr", "c2", "isr", "command", "control"]):
        tags.add("C4ISR")
    return sorted(tags) if tags else ["—"]

def relevance_score(title: str, summary: str, source: str, dt: datetime) -> float:
    txt = normalize(f"{title} {summary}")
    # densité simple : occurrences IA et DEF
    ia_hits = sum(1 for t in AI_TERMS if normalize(t) in txt)
    def_hits = sum(1 for t in DEF_TERMS if normalize(t) in txt)
    dens = min(1.0, (0.07 * ia_hits + 0.05 * def_hits))
    # co-occurrence
    co = 1.3 if (ia_hits > 0 and def_hits > 0) else 1.0
    # autorité
    srcw = SOURCE_WEIGHTS.get(source, 1.0)
    # fraicheur (demi-vie 3 jours)
    age_h = max(0.0, (datetime.now(timezone.utc) - dt).total_seconds() / 3600.0)
    fresh = max(0.5, 2 ** (-age_h / 72.0))
    score = dens * co * srcw * fresh
    return float(round(min(1.5, max(0.0, score)), 6))

def is_excluded(text_norm: str) -> bool:
    for pat in EXCLUSION_PATTERNS:
        if re.search(pat, text_norm):
            # Laisse passer si contexte défense fort
            if any(t in text_norm for t in DEF_TERMS):
                continue
            return True
    return False

def has_ai_and_defense(text_norm: str) -> bool:
    ai_ok = any(t in text_norm for t in (normalize(x) for x in AI_TERMS))
    def_ok = any(t in text_norm for t in (normalize(x) for x in DEF_TERMS))
    return ai_ok and def_ok

# ========================= Modèle d'article =========================

@dataclass
class Article:
    title: str
    link: str
    summary: str
    source: str
    date: datetime
    language: str
    translated: bool
    keyword_score: int
    priority_level: str
    category: str
    relevance_score: float
    tags: List[str]

    @property
    def hash_id(self) -> str:
        return hashlib.md5(f"{self.title}|{self.link}".encode()).hexdigest()

# ========================= Collecteur RSS =========================

class RSSCollector:
    def __init__(self):
        self.session = requests.Session()
        self.session.headers.update({"User-Agent": config.user_agent})

    def fetch(self, url: str) -> feedparser.FeedParserDict:
        for attempt in range(config.max_retries):
            try:
                r = self.session.get(url, timeout=config.request_timeout)
                r.raise_for_status()
                feed = feedparser.parse(r.content)
                if getattr(feed, "bozo", False) and getattr(feed, "bozo_exception", None):
                    logger.warning(f"Feed partiellement malformé ({url}): {feed.bozo_exception}")
                return feed
            except requests.RequestException as e:
                logger.warning(f"[{attempt+1}/{config.max_retries}] Erreur réseau {url}: {e}")
                if attempt < config.max_retries - 1:
                    time.sleep(2 ** attempt)
        logger.error(f"Échec définitif {url}")
        return feedparser.FeedParserDict(feed={}, entries=[])

# ========================= Générateur HTML =========================

class HTMLGenerator:
    def __init__(self, articles: List[Article]):
        self.articles = articles

    def _stats(self) -> Dict:
        total = len(self.articles)
        high = sum(1 for a in self.articles if a.priority_level == "HIGH")
        translated = sum(1 for a in self.articles if a.translated)
        sources = len({a.source for a in self.articles})
        avg_rel = round(sum(a.relevance_score for a in self.articles) / max(1, total), 3)
        return dict(total=total, high=high, translated=translated, sources=sources, avg=avg_rel)

    def _header(self, stats: Dict) -> str:
        generated = datetime.utcnow().strftime("%Y-%m-%d %H:%M UTC")
        return f"""
<header class="bg-blue-900 text-white">
  <div class="max-w-7xl mx-auto px-4 py-6 flex items-center justify-between">
    <div class="flex items-center gap-3">
      <span class="text-2xl">⚓</span>
      <div>
        <h1 class="text-2xl font-bold">Veille IA – Militaire</h1>
        <div class="text-blue-200 text-sm">
          Fenêtre {config.days_window} jours • Généré : {generated} • Seuil pertinence : {config.relevance_min}
        </div>
      </div>
    </div>
    <button id="btnCsv" class="bg-blue-600 hover:bg-blue-500 px-4 py-2 rounded">Exporter CSV</button>
  </div>
</header>
"""

    def _filters(self) -> str:
        cats = sorted({a.category for a in self.articles})
        cat_opts = "".join(f"<option value='{html.escape(c)}'>{html.escape(c)}</option>" for c in cats)
        return f"""
<main class="max-w-7xl mx-auto px-4 py-6">
  <div class="grid grid-cols-1 md:grid-cols-5 gap-4 mb-6">
    <div class="bg-white rounded shadow p-4 text-center">
      <div class="text-3xl font-bold text-blue-700">{len(self.articles)}</div>
      <div class="text-gray-600">Articles</div>
    </div>
    <div class="bg-white rounded shadow p-4 text-center">
      <div class="text-3xl font-bold text-red-600">{sum(1 for a in self.articles if a.priority_level=='HIGH')}</div>
      <div class="text-gray-600">Priorité Haute</div>
    </div>
    <div class="bg-white rounded shadow p-4 text-center">
      <div class="text-3xl font-bold text-green-600">{len({a.source for a in self.articles})}</div>
      <div class="text-gray-600">Sources actives</div>
    </div>
    <div class="bg-white rounded shadow p-4 text-center">
      <div class="text-3xl font-bold text-purple-600">{sum(1 for a in self.articles if a.translated)}</div>
      <div class="text-gray-600">Traduit FR</div>
    </div>
    <div class="bg-white rounded shadow p-4 text-center">
      <div class="text-sm text-gray-600">Pertinence moyenne</div>
      <div class="text-xl font-semibold">{round(sum(a.relevance_score for a in self.articles)/max(1,len(self.articles)),3)}</div>
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
        {cat_opts}
      </select>
    </div>
  </div>
"""

    def _table(self) -> str:
        def level_badge(lv: str) -> str:
            return {"HIGH": "bg-red-600", "MEDIUM": "bg-orange-600", "LOW": "bg-green-600"}.get(lv, "bg-gray-600")
        rows = []
        for a in self.articles:
            t_badge = ' <span class="ml-2 px-2 py-0.5 rounded text-xs text-white" style="background:#6d28d9">🇫🇷 Traduit</span>' if a.translated else ""
            rows.append(
                "<tr class='hover:bg-gray-50' "
                f"data-level='{a.priority_level}' data-source='{html.escape(a.source)}' data-cat='{html.escape(a.category)}'>"
                f"<td class='p-3 text-sm text-gray-600'>{a.date.strftime('%Y-%m-%d')}</td>"
                f"<td class='p-3 text-xs'><span class='bg-blue-100 text-blue-800 px-2 py-1 rounded'>{html.escape(a.source)}</span></td>"
                f"<td class='p-3'><a class='text-blue-700 hover:underline font-semibold' target='_blank' href='{html.escape(a.link)}'>{html.escape(a.title)}</a></td>"
                f"<td class='p-3 text-sm text-gray-800'>{html.escape(a.summary)}{t_badge}</td>"
                f"<td class='p-3 text-center'><span class='bg-indigo-100 text-indigo-800 px-2 py-1 rounded text-sm font-bold'>{a.keyword_score}</span></td>"
                f"<td class='p-3 text-center'><span class='text-white px-2 py-1 rounded text-xs {level_badge(a.priority_level)}'>{a.priority_level}</span></td>"
                f"<td class='p-3 text-sm'><span class='px-2 py-1 rounded text-white text-xs' style='background:#0f766e'>{html.escape(a.category)}</span></td>"
                f"<td class='p-3 text-center text-sm'><span class='bg-gray-100 text-gray-800 px-2 py-1 rounded'>{round(a.relevance_score,3)}</span></td>"
                f"<td class='p-3 text-sm'>{html.escape(', '.join(a.tags) if a.tags else '—')}</td>"
                "</tr>"
            )
        return f"""
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
        {''.join(rows)}
      </tbody>
    </table>
  </div>
</main>
"""

    def _scripts(self) -> str:
        return """
<script>
(function() {
  const rows = Array.from(document.querySelectorAll("#tbody tr"));
  const q = document.getElementById("q");
  const level = document.getElementById("level");
  const source = document.getElementById("source");
  const cat = document.getElementById("cat");

  function applyFilters() {
    const qv = (q.value || "").toLowerCase();
    const lv = level.value;
    const sv = (source.value || "").toLowerCase();
    const cv = cat.value;
    rows.forEach(tr => {
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
    });
  }
  [q, level, source, cat].forEach(el => el.addEventListener("input", applyFilters));

  document.getElementById("btnCsv").addEventListener("click", () => {
    const header = ["Titre","Lien","Date","Source","Résumé","Niveau","Score","Catégorie","Pertinence","Tags"];
    const table = document.querySelector("#tbody");
    const data = [];
    for (const tr of table.querySelectorAll("tr")) {
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
    }
    const csv = [header, ...data].map(r => r.map(x => '"' + String((x==null ? "" : x)).replace(/"/g,'""') + '"').join(",")).join("\\n");
    const blob = new Blob([csv], {type: "text/csv;charset=utf-8"});
    const url = URL.createObjectURL(blob);
    const a = document.createElement("a");
    a.href = url; a.download = "veille_ia_militaire.csv"; a.click();
    URL.revokeObjectURL(url);
  });
})();
</script>
"""

    def build(self) -> str:
        return f"""<!doctype html>
<html lang="fr">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Veille IA – Militaire</title>
  <link href="https://cdn.jsdelivr.net/npm/tailwindcss@2.2.19/dist/tailwind.min.css" rel="stylesheet">
</head>
<body class="bg-gray-50">
  {self._header(self._stats())}
  {self._filters()}
  {self._table()}
  {self._scripts()}
</body>
</html>
"""

# ========================= Orchestration =========================

def parse_entry_datetime(entry) -> Optional[datetime]:
    for fld in ("published_parsed", "updated_parsed"):
        t = getattr(entry, fld, None)
        if t:
            try:
                ts = calendar.timegm(t)
                return datetime.fromtimestamp(ts, tz=timezone.utc)
            except Exception:
                pass
    for fld in ("published", "updated", "pubDate"):
        s = entry.get(fld, "")
        if s:
            try:
                return date_parser.parse(s).astimezone(timezone.utc)
            except Exception:
                pass
    return None

def main():
    logger.info(f"CFG days_window={config.days_window} relevance_min={config.relevance_min} offline_translation={config.offline_translation}")

    collector = RSSCollector()
    translator = TranslationService()

    cutoff = datetime.now(timezone.utc) - timedelta(days=config.days_window)
    seen: Set[str] = set()
    kept: List[Article] = []
    total_seen = 0

    for src_name, meta in RSS_SOURCES.items():
        url = meta["url"]
        feed = collector.fetch(url)
        entries = getattr(feed, "entries", []) or []
        logger.info(f"Source {src_name}: {len(entries)} entrées")
        total_seen += len(entries)

        for e in entries:
            # Date
            dt = parse_entry_datetime(e) or datetime.now(timezone.utc)
            if dt < cutoff:
                continue

            # Titre / lien / contenu
            title = (e.get("title") or "").strip()
            link = (e.get("link") or "").strip()
            raw = e.get("summary") or e.get("description") or ""
            if not title or not link:
                continue

            # Nettoyage + langue
            clean = clean_html(raw)
            detected_lang = detect_language_simple(f"{title} {clean}")

            # Traduction si la source est EN ou si détection EN
            summary = clean
            translated = False
            if config.offline_translation and (meta.get("language") == "en" or detected_lang == "en"):
                summary, translated = translator.translate_en_to_fr(clean)

            # Coupe les résumés trop longs (après trad)
            if len(summary) > config.max_summary_chars:
                summary = summary[:config.max_summary_chars - 1].rsplit(" ", 1)[0] + "…"

            # Texte pour filtres
            text_norm = normalize(f"{title} {summary}")

            # Exclusion "bruit"
            if is_excluded(text_norm):
                continue

            # Règle de base : IA + Défense obligatoires
            if not has_ai_and_defense(text_norm):
                continue

            # Scoring & garde-fous
            kw_score = compute_keyword_score(text_norm)
            cat = classify_category(text_norm)
            rscore = relevance_score(title, summary, src_name, dt)
            if rscore < config.relevance_min:
                continue
            tags = generate_tags(text_norm)

            art = Article(
                title=title,
                link=link,
                summary=summary,
                source=src_name,
                date=dt,
                language=detected_lang,
                translated=translated,
                keyword_score=kw_score,
                priority_level=("HIGH" if kw_score >= 15 else "MEDIUM" if kw_score >= 8 else "LOW"),
                category=cat,
                relevance_score=rscore,
                tags=tags
            )

            # dédup (titre|lien)
            if art.hash_id in seen:
                continue
            seen.add(art.hash_id)
            kept.append(art)

    # Tri : pertinence desc, date desc, score desc
    kept.sort(key=lambda a: (a.relevance_score, a.date, a.keyword_score), reverse=True)

    # Génération HTML
    config.output_dir.mkdir(parents=True, exist_ok=True)
    html_page = HTMLGenerator(kept).build()
    (config.output_dir / config.output_file).write_text(html_page, encoding="utf-8")

    logger.info(f"Articles récupérés : {total_seen} • conservés : {len(kept)}")
    logger.info(f"✅ Rapport écrit dans {config.output_dir / config.output_file}")

if __name__ == "__main__":
    main()
