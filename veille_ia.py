#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
Veille IA – Militaire (Marine)
- Récupère des flux RSS IA / Défense / Naval / Cyber
- Filtre (IA ∧ Défense) avec logique "bridge" par catégorie de source
- Calcule un score de pertinence
- Génère docs/index.html (table, filtres, export CSV)
"""

import os
import re
import html
import time
import logging
from pathlib import Path
from dataclasses import dataclass
from typing import List, Dict, Optional, Tuple, Set
from datetime import datetime, timezone, timedelta

import requests
import feedparser
from dateutil import parser as date_parser


# ========================= Configuration =========================

@dataclass
class Config:
    days_window: int = int(os.getenv("DAYS_WINDOW", "60"))
    relevance_min: float = float(os.getenv("RELEVANCE_MIN", "0.28"))
    max_summary_chars: int = int(os.getenv("MAX_SUMMARY_CHARS", "300"))
    offline_translation: bool = os.getenv("OFFLINE_TRANSLATION", "0") == "1"
    output_dir: Path = Path("docs")
    output_file: str = "index.html"
    request_timeout: int = 25
    max_retries: int = 3
    user_agent: str = "VeilleIA-Military/2.1 (+https://github.com/Guillaume7625/veille-ia-marine)"


config = Config()


# ========================= Sources RSS =========================

# Chaque source possède: url, language, authority, category
# category ∈ {"tech","business","defense","naval","cyber"}
RSS_SOURCES: Dict[str, Dict] = {
    # IA / Tech FR
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

    # IA / Tech EN
    "VentureBeat AI": {
        "url": "https://venturebeat.com/category/ai/feed/",
        "language": "en",
        "authority": 1.05,
        "category": "business",
    },

    # Défense / Naval / Cyber EN
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


# ========================= Vocabulaire (ancres + boosts) =========================

ANCHORS_AI = {
    "ai", "intelligence artificielle", "artificial intelligence",
    "machine learning", "apprentissage automatique", "deep learning",
    "neural network", "réseau neuronal", "transformer",
    "llm", "large language model", "foundation model", "model foundation",
    "genai", "génératif", "diffusion", "inference", "inférence",
    "computer vision", "nlp", "natural language processing",
    "agent", "multi-agent", "autonomy", "autonomous", "autonome",
}

ANCHORS_DEF = {
    # Opérations / institutions
    "defense", "défense", "defence", "military", "warfighter", "battlefield",
    "nato", "otan", "dod", "department of defense", "mod", "ministry of defence",
    "pentagon", "doctrine", "procurement", "acquisition",
    "us navy", "royal navy", "air force", "space force", "army", "marines",
    # C2 / ISR / GE
    "c4isr", "isr", "c2", "command and control", "command & control",
    "electronic warfare", "ew", "jamming", "sigint", "elint",
    # Plateformes / armements
    "naval", "marine", "navy", "frigate", "frégate", "destroyer",
    "submarine", "sous-marin", "aircraft carrier", "porte-avions",
    "drone", "uav", "uas", "uuv", "usv", "ucav", "loyal wingman",
    "radar", "sonar", "missile", "hypersonic", "counter-uas", "counter uas",
    "countermeasure", "contre-mesure",
    # Cyber pilier défense
    "cyber", "cybersécurité", "cybersecurity", "apt", "ransomware", "malware",
    "phishing", "zero-day", "zeroday", "cve-", "xdr", "edr", "siem", "soc",
    "cisa", "anssi", "cert", "ddos",
}

# Boosts pondérés pour la pertinence
SEMANTIC_KEYWORDS = {
    "ai_core": {
        "weight": 6,
        "terms": [
            "intelligence artificielle", "ai", "machine learning", "deep learning",
            "neural network", "réseau neuronal", "transformer",
            "foundation model", "llm", "génératif", "genai", "diffusion",
            "inference", "inférence",
        ],
    },
    "ai_apps": {
        "weight": 4,
        "terms": [
            "computer vision", "vision", "nlp", "natural language processing",
            "speech recognition", "reconnaissance vocale",
            "multimodal", "agent", "multi-agent", "autonomous", "autonome",
            "edge inference",
        ],
    },
    "def_ops": {
        "weight": 6,
        "terms": [
            "defense", "défense", "defence", "military", "nato", "otan", "dod", "mod",
            "command and control", "c2", "isr", "c4isr",
            "electronic warfare", "ew", "jamming", "warfighter", "battlefield",
            "doctrine", "procurement", "acquisition",
        ],
    },
    "def_platforms": {
        "weight": 5,
        "terms": [
            "naval", "marine", "navy", "frégate", "frigate", "destroyer",
            "sous-marin", "submarine", "porte-avions", "aircraft carrier",
            "drone", "uav", "uas", "uuv", "usv", "ucav", "loyal wingman",
            "radar", "sonar", "missile", "hypersonic", "counter-uas", "countermeasure",
        ],
    },
    "cyber_def": {
        "weight": 5,
        "terms": [
            "cyber", "cybersécurité", "cybersecurity", "apt", "ransomware", "malware",
            "phishing", "zero-day", "zeroday", "cve-", "xdr", "edr", "siem", "soc",
            "cisa", "anssi", "cert", "ddos",
        ],
    },
}

# Exclusions (grand public / divertissement / e-commerce)
EXCLUSION_PATTERNS = [
    r"\b(deal|promo|bon\s?plan|réduction|discount|sale|prix|achat|pre[-\s]?order|pré[-\s]?commande)\b",
    r"\b(gaming|jeux?\s?vidéo|streaming|cinéma|people|célébrité|gossip)\b",
    r"\b(smartphone|iphone|android|tablet|gadget|wearable|earbuds?)\b",
    r"\b(disney\+|netflix|prime video|spotify|youtube|tiktok)\b",
    r"\b(métro|resto|voyage|tourisme|loisirs)\b",
]

# Whitelist (laisse passer si défense/ops fort, même si un mot d'exclusion apparaît)
EXCLUSION_WHITELIST = {
    "c4isr", "isr", "command and control", "c2", "warfighter", "battlefield",
    "nato", "otan", "dod", "mod", "pentagon", "us navy", "royal navy",
}


# ========================= Logging =========================

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s",
    handlers=[logging.StreamHandler()],
)
logger = logging.getLogger(__name__)


# ========================= Modèle Article =========================

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
        import hashlib
        return hashlib.md5(f"{self.title}|{self.link}".encode()).hexdigest()


# ========================= Utilitaires texte =========================

class TextProcessor:
    @staticmethod
    def clean_html(text: str) -> str:
        if not text:
            return ""
        t = re.sub(r"<[^>]+>", " ", text)
        t = re.sub(r"\s+", " ", t).strip()
        return t

    @staticmethod
    def norm(s: str) -> str:
        if not s:
            return ""
        import unicodedata
        t = s.lower()
        t = unicodedata.normalize("NFKD", t)
        t = "".join(c for c in t if not unicodedata.combining(c))
        t = re.sub(r"\s+", " ", t).strip()
        return t

    @staticmethod
    def detect_language_simple(text: str) -> str:
        if not text:
            return "unknown"
        t = " " + text.lower() + " "
        fr = [' le ', ' la ', ' les ', ' un ', ' une ', ' des ', ' du ', ' de ',
              ' qui ', ' que ', ' est ', ' sont ', ' avec ', ' dans ', ' pour ', ' sur ']
        en = [' the ', ' and ', ' with ', ' from ', ' that ', ' this ', ' which ',
              ' what ', ' can ', ' will ', ' would ', ' should ', ' have ', ' has ']
        f = sum(1 for m in fr if m in t)
        e = sum(1 for m in en if m in t)
        if f > e:
            return "fr"
        if e > f:
            return "en"
        return "unknown"


# ========================= Traduction (facultatif) =========================

class TranslationService:
    """Traduction offline via Argos si installé. Sinon, laisse le texte tel quel."""
    def __init__(self):
        self.available = False
        self._setup()

    def _setup(self) -> None:
        if not config.offline_translation:
            return
        try:
            from argostranslate import translate as argos_translate
            langs = argos_translate.get_installed_languages()
            en = next((l for l in langs if l.code == "en"), None)
            fr = next((l for l in langs if l.code == "fr"), None)
            if en and fr:
                self._translation = en.get_translation(fr)
                self.available = self._translation is not None
                logger.info("✅ Argos EN→FR disponible")
            else:
                logger.warning("⚠️ Argos installé mais modèles EN/FR absents")
        except Exception as e:
            logger.warning(f"⚠️ Argos indisponible: {e}")
            self.available = False

    def en_to_fr(self, text: str) -> Tuple[str, bool]:
        if not text or not self.available:
            return text, False
        try:
            out = self._translation.translate(text)
            if out and out.strip() and out.strip() != text.strip():
                return out, True
        except Exception as e:
            logger.warning(f"Erreur traduction: {e}")
        return text, False


# ========================= Analyseur contenu =========================

class ContentAnalyzer:
    def __init__(self):
        self.translator = TranslationService()

    # --- détection ---
    def _has_ai(self, text_norm: str) -> bool:
        return any(k in text_norm for k in ANCHORS_AI)

    def _has_defense(self, text_norm: str) -> bool:
        return any(k in text_norm for k in ANCHORS_DEF)

    def _same_sentence_cooc(self, text: str) -> bool:
        if not text:
            return False
        sents = re.split(r"(?<=[\.\!\?])\s+", text)
        for s in sents:
            t = TextProcessor.norm(s)
            if any(a in t for a in ANCHORS_AI) and any(d in t for d in ANCHORS_DEF):
                return True
        return False

    # --- scoring ---
    def _keyword_score(self, text_norm: str) -> int:
        score = 0
        # ancres IA / DEF valent 3 chacune (limitées pour éviter l'emballement)
        score += min(10, 3 * sum(1 for k in ANCHORS_AI if k in text_norm))
        score += min(10, 3 * sum(1 for k in ANCHORS_DEF if k in text_norm))
        # boosts taxonomiques pondérés
        for _, data in SEMANTIC_KEYWORDS.items():
            w = data["weight"]
            for term in data["terms"]:
                if TextProcessor.norm(term) in text_norm:
                    score += w
        return score

    def _relevance_score(self, article: Article, authority: float) -> float:
        txt = TextProcessor.norm(f"{article.title} {article.summary}")
        sem = 0.0
        for _, data in SEMANTIC_KEYWORDS.items():
            w = data["weight"]
            for term in data["terms"]:
                if TextProcessor.norm(term) in txt:
                    sem += 0.1 * w

        # fraîcheur (demi-vie ~3 jours)
        age_h = max(0.0, (datetime.now(timezone.utc) - article.date).total_seconds() / 3600.0)
        freshness = max(0.5, 2 ** (-age_h / 72.0))

        # bonus co-occurrence si même phrase
        co = 1.0
        if self._same_sentence_cooc(f"{article.title}. {article.summary}"):
            co = 1.10

        score = (sem * authority * freshness * co) / 10.0
        return max(0.0, min(1.5, score))

    # --- exclusion ---
    def _is_excluded_noise(self, text_norm: str) -> bool:
        for pat in EXCLUSION_PATTERNS:
            if re.search(pat, text_norm):
                if not any(w in text_norm for w in EXCLUSION_WHITELIST):
                    return True
        return False

    # --- date ---
    def _parse_date(self, entry) -> Optional[datetime]:
        for fld in ("published_parsed", "updated_parsed"):
            par = getattr(entry, fld, None)
            if par:
                try:
                    import calendar
                    ts = calendar.timegm(par)
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

    # --- pipeline article ---
    def process_entry(self, entry, src_name: str, meta: Dict) -> Optional[Article]:
        title = (entry.get("title") or "").strip()
        link = (entry.get("link") or "").strip()
        raw = entry.get("summary") or entry.get("description") or ""
        if not title or not link:
            return None

        clean = TextProcessor.clean_html(raw)
        detected = TextProcessor.detect_language_simple(f"{title} {clean}")

        # traduction EN -> FR si disponible
        summary = clean
        translated = False
        if meta.get("language") == "en" or detected == "en":
            summary, translated = self.translator.en_to_fr(clean)

        # Limiter la taille du résumé
        if len(summary) > config.max_summary_chars:
            summary = summary[: config.max_summary_chars - 1].rsplit(" ", 1)[0] + "…"

        # Date
        dt = self._parse_date(entry) or datetime.now(timezone.utc)

        # Texte normalisé pour les tests/score
        full = f"{title} {summary}"
        norm = TextProcessor.norm(full)

        # Exclusions bruit
        if self._is_excluded_noise(norm):
            return None

        has_ai = self._has_ai(norm)
        has_def = self._has_defense(norm)
        is_def_source = meta.get("category") in ("defense", "naval", "cyber")
        is_ai_source = meta.get("category") in ("tech", "business")

        # Règle d'acceptation "bridge"
        ok_pair = has_ai and has_def
        ok_bridge_from_def = is_def_source and has_ai
        ok_bridge_from_ai = is_ai_source and has_def
        if not (ok_pair or ok_bridge_from_def or ok_bridge_from_ai):
            return None

        article = Article(
            title=title,
            link=link,
            summary=summary,
            source=src_name,
            date=dt,
            language=detected,
            translated=translated,
            category=meta.get("category", "GENERAL").upper(),
        )

        article.keyword_score = self._keyword_score(norm)
        article.relevance_score = self._relevance_score(article, meta.get("authority", 1.0))

        # Priorité simple
        if article.keyword_score >= 18:
            article.priority_level = "HIGH"
        elif article.keyword_score >= 10:
            article.priority_level = "MEDIUM"
        else:
            article.priority_level = "LOW"

        # Tags rapides
        tags = []
        if "cyber" in norm or "ransomware" in norm:
            tags.append("Cyber")
        if "naval" in norm or "marine" in norm or "navy" in norm:
            tags.append("Naval")
        if "c4isr" in norm or "isr" in norm or "c2" in norm:
            tags.append("C2/ISR")
        if "llm" in norm or "génératif" in norm or "genai" in norm:
            tags.append("Génératif/LLM")
        article.tags = tags

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
        cats = sorted({a.category for a in self.articles}) or []
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
  {self._scripts()}
</body>
</html>
"""


# ========================= Orchestration =========================

def main():
    logger.info(f"CFG days_window={config.days_window} relevance_min={config.relevance_min} offline_translation={config.offline_translation}")

    collector = RSSCollector()
    analyzer = ContentAnalyzer()
    cutoff = datetime.now(timezone.utc) - timedelta(days=config.days_window)

    seen_hashes: Set[str] = set()
    kept_articles: List[Article] = []
    stats = {"seen": 0, "excluded": 0, "no_pair": 0, "too_old": 0, "low_rel": 0, "kept": 0}

    for src_name, meta in RSS_SOURCES.items():
        url = meta["url"]
        feed = collector.fetch(url)
        entries = getattr(feed, "entries", []) or []
        logger.info(f"Source {src_name}: {len(entries)} entrées")
        for entry in entries:
            stats["seen"] += 1
            art = analyzer.process_entry(entry, src_name, meta)
            if not art:
                stats["no_pair"] += 1
                continue
            if art.date < cutoff:
                stats["too_old"] += 1
                continue
            if art.hash_id in seen_hashes:
                continue
            # seuil de pertinence
            if art.relevance_score < config.relevance_min:
                stats["low_rel"] += 1
                continue

            seen_hashes.add(art.hash_id)
            kept_articles.append(art)
            stats["kept"] += 1

    # Tri: pertinence desc, date desc, score desc
    kept_articles.sort(key=lambda a: (a.relevance_score, a.date, a.keyword_score), reverse=True)

    # Génération HTML
    config.output_dir.mkdir(parents=True, exist_ok=True)
    page = HTMLGenerator(kept_articles).build()
    (config.output_dir / config.output_file).write_text(page, encoding="utf-8")

    logger.info(f"[STATS] seen={stats['seen']} too_old={stats['too_old']} low_rel={stats['low_rel']} no_pair={stats['no_pair']} kept={stats['kept']}")
    logger.info(f"✅ Rapport écrit dans {config.output_dir / config.output_file} – {len(kept_articles)} entrées")


if __name__ == "__main__":
    main()
