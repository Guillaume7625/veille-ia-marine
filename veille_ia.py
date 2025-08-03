#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Système de Veille IA Militaire - Version Optimisée
Collecte, analyse et génère un rapport HTML (docs/index.html) des actualités IA/Défense.

- Fenêtre glissante configurable (DAYS_WINDOW)
- Filtrage strict : IA OBLIGATOIRE + Défense/Marine/Cyber OBLIGATOIRE
- Traduction offline EN→FR via Argos (OFFLINE_TRANSLATION=1)
- Pertinence contextuelle + scoring mots-clés
- Déduplication (titre|lien)
- UI Tailwind + filtres + export CSV
"""

import os
import re
import html
import time
import hashlib
import logging
from pathlib import Path
from datetime import datetime, timezone, timedelta
from dataclasses import dataclass
from typing import List, Dict, Set, Optional, Tuple

import feedparser
import requests
from dateutil import parser as date_parser

# ========================= Configuration =========================

@dataclass
class Config:
    days_window: int = int(os.getenv("DAYS_WINDOW", "30"))
    relevance_min: float = float(os.getenv("RELEVANCE_MIN", "0.40"))
    max_summary_chars: int = int(os.getenv("MAX_SUMMARY_CHARS", "300"))
    offline_translation: bool = os.getenv("OFFLINE_TRANSLATION", "0") == "1"
    output_dir: Path = Path("docs")
    output_file: str = "index.html"
    request_timeout: int = 25
    max_retries: int = 3
    user_agent: str = "VeilleIA-Military/2.0 (+https://github.com/guillaume7625/veille-ia-marine)"

config = Config()

# Sources RSS (nom lisible -> métadonnées)
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
        "authority": 1.05,
    },
    "C4ISRNet": {
        "url": "https://www.c4isrnet.com/arc/outboundfeeds/rss/",
        "language": "en",
        "authority": 1.15,
    },
    "Breaking Defense": {
        "url": "https://breakingdefense.com/feed/",
        "language": "en",
        "authority": 1.15,
    },
    "Naval Technology": {
        "url": "https://www.naval-technology.com/feed/",
        "language": "en",
        "authority": 1.10,
    },
    "Cybersecurity Dive": {
        "url": "https://www.cybersecuritydive.com/feeds/news/",
        "language": "en",
        "authority": 1.05,
    },
}

# Vocabulaire sémantique (IA & Défense)
SEMANTIC_KEYWORDS = {
    "ai_core": {
        "weight": 5,
        "terms": [
            "intelligence artificielle", "ia", "ai", "artificial intelligence"
        ],
    },
    "ml_techniques": {
        "weight": 4,
        "terms": [
            "machine learning", "deep learning", "neural network",
            "apprentissage automatique", "réseau neuronal",
            "transformer", "attention mechanism", "inference", "inférence"
        ],
    },
    "ai_applications": {
        "weight": 3,
        "terms": [
            "computer vision", "nlp", "natural language processing",
            "speech recognition", "vision artificielle",
            "traitement du langage", "reconnaissance vocale"
        ],
    },
    "generative_ai": {
        "weight": 4,
        "terms": [
            "llm", "large language model", "gpt", "generative ai", "génératif",
            "diffusion model", "gan"
        ],
    },
    "naval_platforms": {
        "weight": 5,
        "terms": [
            "marine", "naval", "navy", "frégate", "fregate", "destroyer",
            "sous-marin", "submarine", "corvette", "porte-avions", "aircraft carrier",
            "maritime"
        ],
    },
    "defense_systems": {
        "weight": 4,
        "terms": [
            "radar", "sonar", "lidar", "ew", "electronic warfare", "guerre électronique",
            "missile", "torpedo", "countermeasure", "contre-mesure"
        ],
    },
    "c4isr": {
        "weight": 5,
        "terms": [
            "c4isr", "c2", "command", "control", "isr",
            "intelligence surveillance reconnaissance",
            "situational awareness", "sensor fusion"
        ],
    },
    "cyber_defense": {
        "weight": 4,
        "terms": [
            "cyber", "cybersécurité", "cybersecurity", "cyber defense", "cyberdéfense",
            "ransomware", "malware", "intrusion", "apt", "zero day", "soc", "threat intelligence"
        ],
    },
    "autonomous_systems": {
        "weight": 4,
        "terms": [
            "drone", "uav", "unmanned", "autonomous", "autonome",
            "robot", "robotique", "swarm", "essaim", "usv", "uuv"
        ],
    },
    "logistics_support": {
        "weight": 3,
        "terms": [
            "logistique", "logistics", "maintenance", "mco",
            "supply chain", "soutien", "predictive maintenance", "maintenance prédictive"
        ],
    },
}

# Patterns d'exclusion
EXCLUSION_PATTERNS = [
    r"\b(deal|promo|bon\s?plan|reduction|discount|sale)\b",
    r"\b(gaming|jeu(x)?\s?vidéo|streaming|entertainment)\b",
    r"\b(smartphone|tablet|gadget|consumer|grand public)\b",
    r"\b(rumeur|rumor|leak|spoiler|speculation)\b",
    r"\b(celebrity|people|gossip)\b",
]

# ========================= Logging =========================

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s",
    handlers=[logging.StreamHandler()],
)
logger = logging.getLogger(__name__)

# ========================= Modèle d'article =========================

@dataclass
class Article:
    title: str
    link: str
    summary: str
    source: str
    date: datetime
    language: str
    translated: bool = False
    relevance_score: float = 0.0
    keyword_score: int = 0
    priority_level: str = "LOW"
    category: str = "GENERAL"
    tags: List[str] = None

    def __post_init__(self):
        if self.tags is None:
            self.tags = []

    @property
    def hash_id(self) -> str:
        return hashlib.md5(f"{self.title}|{self.link}".encode()).hexdigest()

# ========================= Utils texte =========================

class TextProcessor:
    @staticmethod
    def clean_html(text: str) -> str:
        if not text:
            return ""
        text = re.sub(r"<[^>]+>", " ", text)
        text = re.sub(r"\s+", " ", text).strip()
        return text

    @staticmethod
    def normalize(text: str) -> str:
        if not text:
            return ""
        import unicodedata
        t = text.lower()
        t = unicodedata.normalize("NFKD", t)
        t = "".join(c for c in t if not unicodedata.combining(c))
        return t

    @staticmethod
    def detect_language(text: str) -> str:
        if not text:
            return "unknown"
        t = f" {text.lower()} "
        fr = [' le ', ' la ', ' les ', ' un ', ' une ', ' des ', ' du ', ' de ',
              ' qui ', ' que ', ' est ', ' sont ', ' avec ', ' dans ', ' pour ']
        en = [' the ', ' and ', ' with ', ' from ', ' that ', ' this ', ' which ',
              ' what ', ' can ', ' will ', ' would ', ' should ', ' have ', ' has ']
        fs = sum(1 for m in fr if m in t)
        es = sum(1 for m in en if m in t)
        if fs > es: return "fr"
        if es > fs: return "en"
        return "unknown"

# ========================= Traduction Argos =========================

class TranslationService:
    def __init__(self):
        self.available = False
        self.translation = None
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
                self.translation = en.get_translation(fr)
                self.available = self.translation is not None
                logger.info("✅ Service Argos prêt (EN→FR)")
            else:
                logger.warning("⚠️ Modèles Argos EN/FR non installés (le workflow les installe).")
        except Exception as e:
            logger.warning(f"⚠️ Argos indisponible: {e}")

    def translate_en_to_fr(self, text: str) -> Tuple[str, bool]:
        if not text or not self.available or self.translation is None:
            return text, False
        try:
            out = self.translation.translate(text)
            if out and out.strip() != text.strip():
                return out, True
            return text, False
        except Exception as e:
            logger.warning(f"Erreur traduction: {e}")
            return text, False

# ========================= Analyseur contenu =========================

class ContentAnalyzer:
    def __init__(self):
        self.translator = TranslationService()

    def _parse_date(self, entry) -> Optional[datetime]:
        for fld in ("published_parsed", "updated_parsed"):
            if hasattr(entry, fld) and getattr(entry, fld):
                import calendar
                try:
                    ts = calendar.timegm(getattr(entry, fld))
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

    def _is_excluded(self, text_norm: str) -> bool:
        for pat in EXCLUSION_PATTERNS:
            if re.search(pat, text_norm):
                # Laisse passer si contexte défense fort
                def_ctx_terms = (
                    SEMANTIC_KEYWORDS["naval_platforms"]["terms"] +
                    SEMANTIC_KEYWORDS["defense_systems"]["terms"] +
                    SEMANTIC_KEYWORDS["c4isr"]["terms"]
                )
                if not any(t in text_norm for t in def_ctx_terms):
                    return True
        return False

    def _has_ai(self, text_norm: str) -> bool:
        terms = (
            SEMANTIC_KEYWORDS["ai_core"]["terms"] +
            SEMANTIC_KEYWORDS["ml_techniques"]["terms"] +
            SEMANTIC_KEYWORDS["generative_ai"]["terms"] +
            SEMANTIC_KEYWORDS["ai_applications"]["terms"]
        )
        return any(t in text_norm for t in (TextProcessor.normalize(k) for k in terms))

    def _has_defense(self, text_norm: str) -> bool:
        terms = (
            SEMANTIC_KEYWORDS["naval_platforms"]["terms"] +
            SEMANTIC_KEYWORDS["defense_systems"]["terms"] +
            SEMANTIC_KEYWORDS["c4isr"]["terms"] +
            SEMANTIC_KEYWORDS["cyber_defense"]["terms"] +
            SEMANTIC_KEYWORDS["autonomous_systems"]["terms"]
        )
        return any(t in text_norm for t in (TextProcessor.normalize(k) for k in terms))

    def _keyword_score(self, text_norm: str) -> int:
        score = 0
        for cat, data in SEMANTIC_KEYWORDS.items():
            w = data["weight"]
            for term in data["terms"]:
                if TextProcessor.normalize(term) in text_norm:
                    score += w
        return score

    def _relevance_score(self, article: Article, authority: float) -> float:
        txt = TextProcessor.normalize(f"{article.title} {article.summary}")
        sem = 0.0
        for cat, data in SEMANTIC_KEYWORDS.items():
            w = data["weight"]
            for term in data["terms"]:
                if TextProcessor.normalize(term) in txt:
                    sem += 0.1 * w
        # fraîcheur (demi-vie 3 jours)
        age_h = max(0.0, (datetime.now(timezone.utc) - article.date).total_seconds() / 3600.0)
        freshness = max(0.5, 2 ** (-age_h / 72.0))
        # co-occurrence IA+DEF bonus
        ai = self._has_ai(txt)
        df = self._has_defense(txt)
        co = 1.3 if (ai and df) else 1.0
        score = (sem * authority * freshness * co) / 10.0
        return max(0.0, min(1.5, score))

    def process_entry(self, entry, src_name: str, meta: Dict) -> Optional[Article]:
        title = (entry.get("title") or "").strip()
        link  = (entry.get("link") or "").strip()
        raw   = entry.get("summary") or entry.get("description") or ""
        if not title or not link:
            return None

        clean = TextProcessor.clean_html(raw)
        detected = TextProcessor.detect_language(f"{title} {clean}")
        src_lang = meta.get("language", "unknown")

        # Traduction (si EN)
        summary = clean
        translated = False
        if src_lang == "en" or detected == "en":
            summary, translated = self.translator.translate_en_to_fr(clean)

        # Limiter la taille
        if len(summary) > config.max_summary_chars:
            summary = summary[:config.max_summary_chars - 1].rsplit(" ", 1)[0] + "…"

        # Date
        dt = self._parse_date(entry) or datetime.now(timezone.utc)

        # Filtrages
        full = f"{title} {summary}"
        norm = TextProcessor.normalize(full)

        if self._is_excluded(norm):
            return None
        if not self._has_ai(norm) or not self._has_defense(norm):
            return None

        # Article
        article = Article(
            title=title,
            link=link,
            summary=summary,
            source=src_name,
            date=dt,
            language=detected,
            translated=translated,
        )
        # Scores
        article.keyword_score   = self._keyword_score(norm)
        article.relevance_score = self._relevance_score(article, meta.get("authority", 1.0))

        # Priorité
        if article.keyword_score >= 15:
            article.priority_level = "HIGH"
        elif article.keyword_score >= 8:
            article.priority_level = "MEDIUM"
        else:
            article.priority_level = "LOW"

        return article

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
                if feed.bozo and getattr(feed, "bozo_exception", None):
                    logging.warning(f"Feed partiellement malformé: {feed.bozo_exception}")
                return feed
            except requests.RequestException as e:
                logging.warning(f"[{attempt+1}/{config.max_retries}] Erreur réseau {url}: {e}")
                if attempt < config.max_retries - 1:
                    time.sleep(2 ** attempt)
        logging.error(f"Échec définitif {url}")
        return feedparser.FeedParserDict(feed={}, entries=[])

# ========================= Génération HTML =========================

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

<main class="max-w-7xl mx-auto px-4 py-6">
  <div class="grid grid-cols-1 md:grid-cols-5 gap-4 mb-6">
    <div class="bg-white rounded shadow p-4 text-center">
      <div class="text-3xl font-bold text-blue-700">{stats['total']}</div>
      <div class="text-gray-600">Articles</div>
    </div>
    <div class="bg-white rounded shadow p-4 text-center">
      <div class="text-3xl font-bold text-red-600">{stats['high']}</div>
      <div class="text-gray-600">Priorité Haute</div>
    </div>
    <div class="bg-white rounded shadow p-4 text-center">
      <div class="text-3xl font-bold text-green-600">{stats['sources']}</div>
      <div class="text-gray-600">Sources actives</div>
    </div>
    <div class="bg-white rounded shadow p-4 text-center">
      <div class="text-3xl font-bold text-purple-600">{stats['translated']}</div>
      <div class="text-gray-600">Traduit FR</div>
    </div>
    <div class="bg-white rounded shadow p-4 text-center">
      <div class="text-sm text-gray-600">Pertinence moyenne</div>
      <div class="text-xl font-semibold">{stats['avg']}</div>
    </div>
  </div>
"""

    def _filters(self) -> str:
        cats = sorted({a.category for a in self.articles})
        cat_opts = "".join(f"<option value='{html.escape(c)}'>{html.escape(c)}</option>" for c in cats)
        return f"""
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
        stats = self._stats()
        return f"""<!doctype html>
<html lang="fr">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Veille IA – Militaire</title>
  <link href="https://cdn.jsdelivr.net/npm/tailwindcss@2.2.19/dist/tailwind.min.css" rel="stylesheet">
</head>
<body class="bg-gray-50">
  {self._header(stats)}
  {self._filters()}
  {self._table()}
</main>
{self._scripts()}
</body>
</html>
"""

# ========================= Orchestration main =========================

def main():
    logger.info(f"CFG days_window={config.days_window} relevance_min={config.relevance_min} offline_translation={config.offline_translation}")
    collector = RSSCollector()
    analyzer = ContentAnalyzer()

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

        for entry in entries:
            art = analyzer.process_entry(entry, src_name, meta)
            if not art:
                continue
            if art.date < cutoff:
                continue
            # score de pertinence minimal
            if art.relevance_score < config.relevance_min:
                continue
            # dédup
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
