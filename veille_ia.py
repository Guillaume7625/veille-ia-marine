#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
Veille IA – Militaire (Marine)
- Collecte des flux RSS IA/Défense
- Résumés FR (nettoyage, 2 phrases max, traduction offline EN->FR si dispo)
- Filtre strict : IA OBLIGATOIRE + co-occurrence IA/DEF dans le titre ou une même phrase
- Scoring (mots-clés + fraîcheur + autorité source)
- HTML Tailwind avec filtres & export CSV (docs/index.html)
"""

import os
import re
import html
import time
import logging
import hashlib
from pathlib import Path
from dataclasses import dataclass
from typing import List, Dict, Optional, Tuple, Set
from datetime import datetime, timezone, timedelta

import requests
import feedparser
from dateutil import parser as date_parser

# =========================== Config ===========================

@dataclass
class Config:
    days_window: int = int(os.getenv("DAYS_WINDOW", "45"))
    relevance_min: float = float(os.getenv("RELEVANCE_MIN", "0.28"))
    max_summary_chars: int = int(os.getenv("MAX_SUMMARY_CHARS", "300"))
    offline_translation: bool = os.getenv("OFFLINE_TRANSLATION", "0") == "1"
    output_dir: Path = Path("docs")
    output_file: str = "index.html"
    request_timeout: int = 25
    max_retries: int = 3
    user_agent: str = "VeilleIA-Military/2.1 (+https://github.com/guillaume7625/veille-ia-marine)"

config = Config()

# ======================== Sources RSS =========================

RSS_SOURCES: Dict[str, Dict] = {
    "ActuIA": {
        "url": "https://www.actuia.com/feed/",
        "language": "fr",
        "authority": 1.00,
        "category": "tech",
    },
    "Numerama": {
        "url": "https://www.numerama.com/feed/",
        "language": "fr",
        "authority": 0.95,
        "category": "tech",
    },
    "VentureBeat AI": {
        "url": "https://venturebeat.com/category/ai/feed/",
        "language": "en",
        "authority": 1.05,
        "category": "business",
    },
    "C4ISRNet": {
        "url": "https://www.c4isrnet.com/arc/outboundfeeds/rss/",
        "language": "en",
        "authority": 1.15,
        "category": "defense",
    },
    "Breaking Defense": {
        "url": "https://breakingdefense.com/feed/",
        "language": "en",
        "authority": 1.15,
        "category": "defense",
    },
    "Naval Technology": {
        "url": "https://www.naval-technology.com/feed/",
        "language": "en",
        "authority": 1.10,
        "category": "naval",
    },
    "Cybersecurity Dive": {
        "url": "https://www.cybersecuritydive.com/feeds/news/",
        "language": "en",
        "authority": 1.05,
        "category": "cyber",
    },
}

# =================== Vocabulaire & patterns ===================

SEMANTIC_KEYWORDS = {
    "ai_core": {
        "weight": 5,
        "terms": [
            "intelligence artificielle", "ia", "ai", "artificial intelligence",
        ],
    },
    "ml_techniques": {
        "weight": 4,
        "terms": [
            "machine learning", "deep learning", "neural network",
            "apprentissage automatique", "réseau neuronal",
            "transformer", "inference", "inférence",
        ],
    },
    "ai_applications": {
        "weight": 3,
        "terms": [
            "computer vision", "vision par ordinateur",
            "nlp", "natural language processing", "traitement du langage",
            "speech recognition", "reconnaissance vocale",
        ],
    },
    "generative_ai": {
        "weight": 4,
        "terms": [
            "llm", "large language model", "gpt", "generative ai", "génératif",
            "diffusion model", "gan",
        ],
    },
    "naval_platforms": {
        "weight": 5,
        "terms": [
            "marine", "naval", "navy", "frégate", "fregate", "destroyer",
            "sous-marin", "submarine", "corvette", "porte-avions",
            "aircraft carrier", "maritime",
        ],
    },
    "defense_systems": {
        "weight": 4,
        "terms": [
            "radar", "sonar", "lidar", "ew", "electronic warfare", "guerre électronique",
            "missile", "torpedo", "countermeasure", "contre-mesure",
        ],
    },
    "c4isr": {
        "weight": 5,
        "terms": [
            "c4isr", "c2", "command", "control", "isr",
            "intelligence surveillance reconnaissance",
            "situational awareness", "sensor fusion", "reconnaissance",
        ],
    },
    "cyber_defense": {
        "weight": 4,
        "terms": [
            "cyber", "cybersécurité", "cybersecurity", "cyber defense", "cyberdéfense",
            "ransomware", "malware", "intrusion", "apt", "zero day", "soc",
            "threat intelligence",
        ],
    },
    "autonomous_systems": {
        "weight": 4,
        "terms": [
            "drone", "uav", "uas", "usv", "uuv", "unmanned",
            "autonomous", "autonome", "swarm", "essaim",
        ],
    },
}

# Exclusions bruit
EXCLUSION_PATTERNS = [
    r"\b(deal|promo|bon\s?plan|reduction|discount|sale)\b",
    r"\b(gaming|jeu(x)?\s?vidéo|streaming|people|cinéma|entertainment)\b",
    r"\b(smartphone|tablet|gadget|consumer|grand public)\b",
    r"\b(rumeur|rumor|leak|spoiler|speculation)\b",
]

# Patterns stricts IA / Défense
AI_PATTERNS = [
    r"\b(ai|ia)\b",
    r"\b(machine learning|apprentissage automatique)\b",
    r"\b(deep learning|réseau(?:x)? neuronal(?:aux)?|neural network(?:s)?)\b",
    r"\b(llm|large language model(?:s)?)\b",
    r"\b(génératif|generative ai|diffusion model|gan)\b",
    r"\b(computer vision|vision par ordinateur)\b",
    r"\b(nlp|traitement du langage|natural language processing)\b",
    r"\b(inférence|inference)\b",
]
DEF_PATTERNS = [
    r"\b(naval|marine|navy|frégate|fregate|destroyer|sous-?marin|submarine|corvette|porte-?avions|aircraft carrier|maritime)\b",
    r"\b(c4isr|c2|isr|command|control|surveillance|reconnaissance|situational awareness|sensor fusion)\b",
    r"\b(radar|sonar|lidar|ew|electronic warfare|guerre électronique|missile|torpedo|counter-?measure)\b",
    r"\b(cyber|cybersécurité|cybersecurity|ransomware|malware|intrusion|apt|zero[- ]?day|soc|threat intelligence)\b",
    r"\b(drone|uav|uas|usv|uuv|unmanned|autonom(?:e|ous)|swarm|essaim)\b",
]

AI_PATTERNS_RE = [re.compile(p, re.IGNORECASE) for p in AI_PATTERNS]
DEF_PATTERNS_RE = [re.compile(p, re.IGNORECASE) for p in DEF_PATTERNS]

# Nettoyage trailers “The post … appeared first on …”
POST_FOOTER_RE = re.compile(
    r"(?:The post|Le post|L[’']?après|L’après)[^.]{0,200}"
    r"(?:appeared first on|est apparu(?:e)? en premier sur).*$",
    flags=re.IGNORECASE,
)

# ========================== Logging ===========================

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s",
    handlers=[logging.StreamHandler()],
)
logger = logging.getLogger(__name__)

# =========================== Utils ============================

def normalize_text(text: str) -> str:
    if not text:
        return ""
    import unicodedata
    t = text.lower()
    t = unicodedata.normalize("NFKD", t)
    t = "".join(c for c in t if not unicodedata.combining(c))
    return t

def strip_html(text: str) -> str:
    if not text:
        return ""
    t = re.sub(r"<[^>]+>", " ", text)
    t = re.sub(r"\s+", " ", t).strip()
    return t

def clean_rss_boilerplate(text: str) -> str:
    if not text:
        return ""
    t = html.unescape(text)
    t = strip_html(t)
    t = POST_FOOTER_RE.sub("", t)
    t = re.sub(r"\s+", " ", t).strip()
    return t

def split_sentences(text: str) -> List[str]:
    if not text:
        return []
    parts = re.split(r"(?<=[\.\!\?])\s+", text)
    return [p.strip() for p in parts if p.strip()]

def detect_language_simple(text: str) -> str:
    if not text:
        return "unknown"
    t = f" {text.lower()} "
    fr = [' le ', ' la ', ' les ', ' un ', ' une ', ' des ', ' du ', ' de ',
          ' qui ', ' que ', ' est ', ' sont ', ' avec ', ' dans ', ' pour ']
    en = [' the ', ' and ', ' with ', ' from ', ' that ', ' this ', ' which ',
          ' what ', ' can ', ' will ', ' would ', ' should ', ' have ', ' has ']
    fs = sum(1 for m in fr if m in t)
    es = sum(1 for m in en if m in t)
    if fs > es:
        return "fr"
    if es > fs:
        return "en"
    return "unknown"

# ======================= Modèle d'article =====================

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
    category: str = "TECHNOLOGY"
    tags: List[str] = None

    def __post_init__(self):
        if self.tags is None:
            self.tags = []

    @property
    def hash_id(self) -> str:
        return hashlib.md5(f"{self.title}|{self.link}".encode("utf-8")).hexdigest()

# ===================== Traduction offline =====================

class TranslationService:
    def __init__(self):
        self.available = False
        self.translation = None
        self._setup()

    def _setup(self):
        if not config.offline_translation:
            logger.info("Traduction offline désactivée (OFFLINE_TRANSLATION=0).")
            return
        try:
            from argostranslate import translate as argos_translate
            langs = argos_translate.get_installed_languages()
            en = next((l for l in langs if l.code == "en"), None)
            fr = next((l for l in langs if l.code == "fr"), None)
            if en and fr:
                self.translation = en.get_translation(fr)
                self.available = self.translation is not None
                if self.available:
                    logger.info("✅ Argos EN→FR prêt.")
                else:
                    logger.warning("⚠️ Argos EN→FR non initialisé.")
            else:
                logger.warning("⚠️ Modèles Argos EN/FR absents (le workflow peut les installer).")
        except Exception as e:
            logger.warning(f"⚠️ Argos indisponible: {e}")

    def translate_en_to_fr(self, text: str) -> Tuple[str, bool]:
        if not text or not self.available or self.translation is None:
            return text, False
        try:
            out = self.translation.translate(text)
            if out and out.strip() and out.strip() != text.strip():
                return out, True
            return text, False
        except Exception as e:
            logger.warning(f"Erreur traduction: {e}")
            return text, False

# ====================== Analyse de contenu =====================

class ContentAnalyzer:
    def __init__(self):
        self.translator = TranslationService()

    # --- Détection IA / Défense ---
    def _has_ai(self, text: str) -> bool:
        t = text or ""
        return any(p.search(t) for p in AI_PATTERNS_RE)

    def _has_defense(self, text: str) -> bool:
        t = text or ""
        return any(p.search(t) for p in DEF_PATTERNS_RE)

    def _cooccurs_ai_def_in_title_or_sentence(self, title: str, summary: str) -> bool:
        scopes = [title] + split_sentences(summary)
        for scope in scopes:
            s = scope.lower()
            if self._has_ai(s) and self._has_defense(s):
                return True
        return False

    # --- Exclusions bruit (avec exception si contexte défense fort) ---
    def is_excluded(self, text_norm: str) -> bool:
        for pattern in EXCLUSION_PATTERNS:
            if re.search(pattern, text_norm):
                defense_ctx_terms = (
                    SEMANTIC_KEYWORDS["naval_platforms"]["terms"] +
                    SEMANTIC_KEYWORDS["defense_systems"]["terms"] +
                    SEMANTIC_KEYWORDS["c4isr"]["terms"]
                )
                if not any(normalize_text(t) in text_norm for t in defense_ctx_terms):
                    return True
        return False

    # --- Scores & classements ---
    def _keyword_score(self, text: str) -> int:
        t = normalize_text(text)
        score = 0
        for _, data in SEMANTIC_KEYWORDS.items():
            w = data["weight"]
            for term in data["terms"]:
                if normalize_text(term) in t:
                    score += w
        return score

    def _relevance_score(self, article: Article, authority: float) -> float:
        t = normalize_text(f"{article.title} {article.summary}")
        sem = 0.0
        for _, data in SEMANTIC_KEYWORDS.items():
            w = data["weight"]
            for term in data["terms"]:
                if normalize_text(term) in t:
                    sem += 0.1 * w
        # fraîcheur (demi-vie ~3 jours)
        age_h = max(0.0, (datetime.now(timezone.utc) - article.date).total_seconds() / 3600.0)
        freshness = max(0.5, 2 ** (-age_h / 72.0))
        # bonus co-occurrence
        co = 1.3 if (self._has_ai(t) and self._has_defense(t)) else 1.0
        score = (sem * authority * freshness * co) / 10.0
        return max(0.0, min(1.5, score))

    def classify_category(self, text: str) -> str:
        t = text.lower()
        if re.search(r"\b(policy|réglementation|regulation|budget|appropriation|spending|bill|award|contract|option year|procurement|acquisition)\b", t):
            return "POLICY"
        if re.search(r"\b(prototype|trial|essai|r&d|laboratoire|lab|research|paper)\b", t):
            return "DEVELOPMENT"
        if re.search(r"\b(deployment|deployed|fielded|opérationnel|operational|exercise|exercice)\b", t):
            return "OPERATIONAL"
        if re.search(r"\b(threat|menace|intrusion|ransomware|ew|electronic warfare|counter-uas|counter uas)\b", t):
            return "THREAT"
        if re.search(r"\b(partnership|alliance|accord|coopération|framework|mou|moa)\b", t):
            return "PARTNERSHIP"
        return "TECHNOLOGY"

    def generate_tags(self, article: Article) -> List[str]:
        t = f"{article.title} {article.summary}".lower()
        tags = set()
        if re.search(r"\b(llm|large language model|génératif|generative ai|diffusion model|gan)\b", t):
            tags.add("LLM/Génératif")
        if re.search(r"\b(computer vision|vision par ordinateur)\b", t):
            tags.add("Vision Artificielle")
        if re.search(r"\b(nlp|traitement du langage|natural language processing)\b", t):
            tags.add("NLP")

        if re.search(r"\b(naval|marine|navy|sous-?marin|submarine|destroyer|frégate|fregate|maritime)\b", t):
            tags.add("Naval")
        if re.search(r"\b(c4isr|c2|isr|command|control|surveillance|reconnaissance)\b", t):
            tags.add("C4ISR")
        if re.search(r"\b(cyber|cybersécurité|cybersecurity|ransomware|malware|intrusion)\b", t):
            tags.add("Cybersécurité")
        if re.search(r"\b(drone|uav|uas|usv|uuv|unmanned|autonom(?:e|ous)|swarm|essaim)\b", t):
            tags.add("Systèmes Autonomes")

        if re.search(r"\b(prototype|research|laboratoire|laboratory|paper)\b", t):
            tags.add("R&D")
        if re.search(r"\b(deployment|deployed|fielded|operational|opérationnel|exercise|exercice)\b", t):
            tags.add("Opérationnel")

        return sorted(tags) if tags else ["—"]

    # --- Parsing date ---
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

    # --- Pipeline article ---
    def process_entry(self, entry, src_name: str, meta: Dict) -> Optional[Article]:
        title = (entry.get("title") or "").strip()
        link = (entry.get("link") or "").strip()
        raw = entry.get("summary") or entry.get("description") or ""
        if not title or not link:
            return None

        # Nettoyage + 2 phrases max
        clean = clean_rss_boilerplate(raw)
        sentences = split_sentences(clean)
        base = " ".join(sentences[:2]) if sentences else clean

        # Détection & traduction
        detected = detect_language_simple(f"{title} {base}")
        src_lang = meta.get("language", "unknown")
        summary = base
        translated = False
        if (src_lang == "en") or (detected == "en"):
            summary, translated = self.translator.translate_en_to_fr(summary)

        # Limitation longueur
        if len(summary) > config.max_summary_chars:
            summary = summary[:config.max_summary_chars - 1].rsplit(" ", 1)[0] + "…"

        # Date
        dt = self._parse_date(entry) or datetime.now(timezone.utc)

        # Filtres
        title_l = title.lower()
        summary_l = summary.lower()
        norm_all = normalize_text(f"{title} {summary}")

        if self.is_excluded(norm_all):
            return None
        # IA obligatoire + co-occurrence IA/DEF locale (titre ou même phrase)
        if not self._has_ai(norm_all):
            return None
        if not self._cooccurs_ai_def_in_title_or_sentence(title_l, summary_l):
            return None

        # Construction article
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
        article.keyword_score = self._keyword_score(f"{title} {summary}")
        article.relevance_score = self._relevance_score(article, meta.get("authority", 1.0))
        article.category = self.classify_category(f"{title} {summary}")
        article.tags = self.generate_tags(article)

        # Priorité
        if article.keyword_score >= 15:
            article.priority_level = "HIGH"
        elif article.keyword_score >= 8:
            article.priority_level = "MEDIUM"
        else:
            article.priority_level = "LOW"

        return article

# ===================== Collecteur RSS réseau ====================

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

# ======================= Générateur HTML =======================

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
  <style>
    .summary-cell {{
      max-height: 4.5rem;
      overflow: hidden;
      display: -webkit-box;
      -webkit-line-clamp: 3;
      -webkit-box-orient: vertical;
      line-height: 1.5;
    }}
  </style>
</head>
<body class="bg-gray-50">
  {self._header(self._stats())}
  {self._filters()}
  {self._table()}
  {self._scripts()}
</body>
</html>
"""

# =========================== Main ==============================

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
            if art.relevance_score < config.relevance_min:
                continue
            if art.hash_id in seen:
                continue
            seen.add(art.hash_id)
            kept.append(art)

    # Tri : pertinence desc, date desc, keyword_score desc
    kept.sort(key=lambda a: (a.relevance_score, a.date, a.keyword_score), reverse=True)

    # Génération HTML
    config.output_dir.mkdir(parents=True, exist_ok=True)
    html_page = HTMLGenerator(kept).build()
    (config.output_dir / config.output_file).write_text(html_page, encoding="utf-8")

    logger.info(f"Articles récupérés : {total_seen} • conservés : {len(kept)}")
    logger.info(f"✅ Rapport écrit dans {config.output_dir / config.output_file}")

if __name__ == "__main__":
    main()
