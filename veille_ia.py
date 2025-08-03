#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Veille IA – Militaire / Marine
================================
• Récupère une sélection de flux RSS orientés IA & Défense
• Filtre : mentions IA **ET** Défense/Militaire obligatoires, exclusion du bruit grand‑public
• Traduit (EN → FR) offline avec Argos Translate si OFFLINE_TRANSLATION=1
• Calcule :
    – score mots‑clés (densité pondérée)
    – score pertinence contextuel (autorité source × fraîcheur × IA/DEF co‑occur)
• Classe en catégories & tags
• Génère automatiquement `docs/index.html` (UI Tailwind + export CSV)
• Conçu pour tourner dans GitHub Actions et pousser sur la branche `gh-pages`.

Env‑vars clés
--------------
DAYS_WINDOW       Fenêtre (jours) pour la veille  → 30 par défaut
RELEVANCE_MIN     Score pertinence minimal pour garder un article (0‑1)
OFFLINE_TRANSLATION   "1" → active la traduction Argos EN→FR

Dépendances
-----------
feedparser   (⚙️ RSS)
argostranslate (optionnel si traduction offline souhaitée)
requests     (⚙️ fetch avec User‑Agent personnalisé)
dateutil     (⚙️ parse dates RSS peu standard)
"""

from __future__ import annotations

import os, re, html, unicodedata, time, calendar, hashlib, logging
from pathlib import Path
from datetime import datetime, timedelta, timezone
from dataclasses import dataclass, field
from typing import List, Dict, Optional, Tuple, Set

import requests
import feedparser
from dateutil import parser as dtparse

# ============================================================================
# ✨ CONFIGURATION
# ============================================================================
@dataclass
class Config:
    days_window:       int   = int(os.getenv("DAYS_WINDOW", "30"))
    relevance_min:     float = float(os.getenv("RELEVANCE_MIN", "0.40"))
    max_summary_chars: int   = int(os.getenv("MAX_SUMMARY_CHARS", "320"))
    offline_translation: bool = os.getenv("OFFLINE_TRANSLATION", "0") == "1"

    # I/O
    output_dir: Path = Path("docs")
    output_file: str = "index.html"

    # network
    request_timeout: int = 25
    max_retries: int = 3
    user_agent: str = "VeilleIA-Military/3.0 (+https://github.com/guillaume7625/veille-ia-marine)"

CFG = Config()

# ---------------------------------------------------------------------------
# Flux RSS suivis (👁️ n = 7)
# ---------------------------------------------------------------------------
RSS_SOURCES: Dict[str, Dict] = {
    # IA ‑ FR
    "ActuIA":              {"url": "https://www.actuia.com/feed/",                       "lang": "fr", "auth": 1.00},
    "Numerama":            {"url": "https://www.numerama.com/feed/",                     "lang": "fr", "auth": 0.95},
    # IA ‑ EN (général / business)
    "VentureBeat AI":      {"url": "https://venturebeat.com/category/ai/feed/",           "lang": "en", "auth": 1.05},
    # Défense / Naval / Cyber
    "C4ISRNet":            {"url": "https://www.c4isrnet.com/arc/outboundfeeds/rss/",     "lang": "en", "auth": 1.15},
    "Breaking Defense":    {"url": "https://breakingdefense.com/feed/",                  "lang": "en", "auth": 1.15},
    "Naval Technology":    {"url": "https://www.naval-technology.com/feed/",             "lang": "en", "auth": 1.10},
    "Cybersecurity Dive":  {"url": "https://www.cybersecuritydive.com/feeds/news/",       "lang": "en", "auth": 1.05},
}

# ---------------------------------------------------------------------------
# Mots‑clés sémantiques (+ poids) – IA & Défense
# ---------------------------------------------------------------------------
SEM_KW: Dict[str, Dict] = {
    "ai_core":         {"w":5, "t":["intelligence artificielle","ia","ai","artificial intelligence"]},
    "ml":              {"w":4, "t":["machine learning","deep learning","neural network","réseau neuronal","transformer","llm","gpt"]},
    "def_naval":       {"w":5, "t":["naval","marine","navy","frégate","destroyer","sous-marin","submarine","porte-avions"]},
    "def_sys":         {"w":4, "t":["radar","sonar","ew","electronic warfare","missile","torpedo","countermeasure"]},
    "c4isr":           {"w":5, "t":["c4isr","isr","command","control","c2","surveillance","reconnaissance"]},
    "cyber":           {"w":4, "t":["cyber","cybersécurité","ransomware","malware","intrusion","apt","zero day"]},
    "autonomous":      {"w":4, "t":["drone","uav","unmanned","autonomous","robot","swarm"]},
}

# ---------------------------------------------------------------------------
EXCLUSION_PATTERNS = [
    r"\b(gaming|jeux? vidéo|entertainment|cinéma)\b",
    r"\b(promo|bon plan|discount|%|€)\b",
    r"\b(smartphone|wearable|gadget|consumer)\b",
]

# ============================================================================
# 🪵 LOGGING
# ============================================================================
logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s | %(levelname)s | %(message)s",
                    handlers=[logging.StreamHandler()])
log = logging.getLogger("veille-ia")

# ============================================================================
# 📑 MODEL & HELPERS
# ============================================================================
@dataclass
class Article:
    title: str
    link: str
    summary: str
    source: str
    date: datetime
    translated: bool
    relevance: float = 0.0
    kw_score:  int   = 0
    priority:  str   = "LOW"
    category:  str   = "TECH"
    tags:      List[str] = field(default_factory=list)

    def hash(self) -> str:
        return hashlib.md5(f"{self.title}|{self.link}".encode()).hexdigest()

class Text:
    @staticmethod
    def strip_html(txt: str) -> str:
        return re.sub(r"<[^>]+>", " ", txt or "").strip()

    @staticmethod
    def norm(txt: str) -> str:
        t = unicodedata.normalize("NFKD", txt.lower())
        return "".join(ch for ch in t if not unicodedata.combining(ch))

    @staticmethod
    def detect_lang(txt: str) -> str:
        sample = " " + txt.lower() + " "
        fr = sum(k in sample for k in [" le "," la "," les "," des "])
        en = sum(k in sample for k in [" the "," and "," with "," from "])
        if fr > en: return "fr"
        if en > fr: return "en"
        return "unk"

# ---------------------------------------------------------------------------
class Translator:
    def __init__(self):
        self.available = False
        if CFG.offline_translation:
            try:
                from argostranslate import translate as at
                self._langs = at.get_installed_languages()
                self._en = next((l for l in self._langs if l.code == "en"), None)
                self._fr = next((l for l in self._langs if l.code == "fr"), None)
                self._tr = self._en.get_translation(self._fr) if self._en and self._fr else None
                self.available = self._tr is not None
                log.info("Argos Translate prêt ✅")
            except Exception as e:
                log.warning(f"Argos Translate indisponible ⚠️ : {e}")

    def en2fr(self, txt: str) -> Tuple[str,bool]:
        if not (self.available and txt):
            return txt, False
        try:
            res = self._tr.translate(txt)
            return (res or txt), (res.strip() != txt.strip())
        except Exception as e:
            log.debug(f"Traduction échouée : {e}")
            return txt, False

TR = Translator()

# ============================================================================
# 🔎 ANALYSE & SCORING
# ============================================================================
class Analyzer:
    def __init__(self):
        # index pour recherche rapide
        self.kw_index = [(Text.norm(t), cfg["w"]) for cfg in SEM_KW.values() for t in cfg["t"]]

    # --- filtres de base ----------------------------------------------------
    def _exclude(self, norm: str) -> bool:
        return any(re.search(p, norm) for p in EXCLUSION_PATTERNS)

    def _contains_ia(self, norm: str) -> bool:
        ia_terms = SEM_KW["ai_core"]["t"] + SEM_KW["ml"]["t"]
        return any(Text.norm(t) in norm for t in ia_terms)

    def _contains_def(self, norm: str) -> bool:
        def_terms = (SEM_KW["def_naval"]["t"] + SEM_KW["def_sys"]["t"] +
                     SEM_KW["c4isr"]["t"]      + SEM_KW["cyber"]["t"]    +
                     SEM_KW["autonomous"]["t"])
        return any(Text.norm(t) in norm for t in def_terms)

    # --- scoring -----------------------------------------------------------
    def kw_score(self, norm: str) -> int:
        return sum(w for k,w in self.kw_index if k in norm)

    def relevance(self, art: Article, auth: float) -> float:
        norm = Text.norm(f"{art.title} {art.summary}")
        sem = sum(0.1*w for k,w in self.kw_index if k in norm)
        age_h = (datetime.now(timezone.utc)-art.date).total_seconds()/3600
        fresh = max(0.5, 2**(-age_h/72))
        bonus = 1.3 if (self._contains_ia(norm) and self._contains_def(norm)) else 1.0
        return round(min(1.5, (sem*auth*fresh*bonus)/10), 3)

    # --- catégorie & tags --------------------------------------------------
    def category(self, norm: str) -> str:
        if "cyber" in norm: return "CYBER"
        if "naval" in norm or "marine" in norm: return "NAVAL"
        if "drone" in norm or "uav" in norm: return "AUTONOMOUS"
        if "c4isr" in norm or "isr" in norm: return "C4ISR"
        return "TECH"

    def tags(self, norm: str) -> List[str]:
        tags = []
        if "gpt" in norm or "llm" in norm: tags.append("LLM")
        if any(k in norm for k in ["drone","uav","swarm"]): tags.append("UAV")
        if "radar" in norm or "sonar" in norm: tags.append("SENSOR")
        if "cyber" in norm: tags.append("CYBER")
        return tags

    # --- traitement d'un item RSS -----------------------------------------
    def process(self, entry: feedparser.FeedParserDict, src: str, meta: Dict) -> Optional[Article]:
        title = (entry.get("title") or "").strip()
        link  = (entry.get("link")  or "").strip()
        raw   = entry.get("summary") or entry.get("description") or ""
        if not title or not link:
            return None

        clean = Text.strip_html(raw)
        dt    = self._parse_date(entry) or datetime.now(timezone.utc)
        if dt < datetime.now(timezone.utc)-timedelta(days=CFG.days_window):
            return None

        # langue & traduction ------------------------------------------------
        summary = clean
        translated = False
        if meta["lang"] == "en":
            summary, translated = TR.en2fr(clean)
        if len(summary) > CFG.max_summary_chars:
            summary = summary[:CFG.max_summary_chars-1].rsplit(" ",1)[0] + "…"

        norm = Text.norm(f"{title} {summary}")
        if self._exclude(norm):
            return None
        if not (self._contains_ia(norm) and self._contains_def(norm)):
            return None

        art = Article(title, link, summary, src, dt, translated)
        art.kw_score  = self.kw_score(norm)
        art.relevance = self.relevance(art, meta["auth"])
        if art.relevance < CFG.relevance_min:
            return None
        art.priority  = "HIGH" if art.kw_score>=15 else "MEDIUM" if art.kw_score>=8 else "LOW"
        art.category  = self.category(norm)
        art.tags      = self.tags(norm)
        return art

    @staticmethod
    def _parse_date(e) -> Optional[datetime]:
        for fld in ("published_parsed","updated_parsed"):
            if getattr(e,fld,None):
                return datetime.fromtimestamp(calendar.timegm(getattr(e,fld)), tz=timezone.utc)
        for fld in ("published","updated","pubDate"):
            if e.get(fld):
                try:
                    dt = dtparse.parse(e.get(fld))
                    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
                except Exception:
                    pass
        return None

AN = Analyzer()

# ============================================================================
# 🌐 COLLECTEUR RSS
# ============================================================================
class RSSCollector:
    def __init__(self):
        self.sess = requests.Session()
        self.sess.headers.update({"User-Agent": CFG.user_agent})

    def fetch(self, url: str) -> feedparser.FeedParserDict:
        for att in range(CFG.max_retries):
            try:
                r = self.sess.get(url, timeout=CFG.request_timeout)
                r.raise_for_status()
                return feedparser.parse(r.content)
            except Exception as e:
                log.warning(f"[{att+1}/{CFG.max_retries}] fetch error {url}: {e}")
                time.sleep(2**att)
        return feedparser.FeedParserDict(entries=[])

CL = RSSCollector()

# ============================================================================
# 🖥️ HTML REPORT (Tailwind v2 CDN)
# ============================================================================
class Reporter:
    def __init__(self, arts: List[Article]):
        self.a = sorted(arts, key=lambda x:(x.relevance,x.date,x.kw_score), reverse=True)
        CFG.output_dir.mkdir(parents=True, exist_ok=True)

    def write(self):
        log.info(f"Écriture rapport → {CFG.output_dir/CFG.output_file}")
        (CFG.output_dir/CFG.output_file).write_text(self._html(), encoding="utf-8")

    # ------------------------------------------------------------------ UI
    def _html(self) -> str:
        stats = {
            "tot": len(self.a),
            "high": sum(1 for x in self.a if x.priority=="HIGH"),
            "src": len({x.source for x in self.a}),
            "tr": sum(1 for x in self.a if x.translated),
            "avg": round(sum(x.relevance for x in self.a)/max(1,len(self.a)),3)
        }
        cats = sorted({x.category for x in self.a})
        rows = "\n".join(self._row(x) for x in self.a)
        now  = datetime.utcnow().strftime("%Y-%m-%d %H:%M UTC")
        return f"""<!doctype html>
<html lang='fr'>
<head><meta charset='utf-8'><meta name='viewport' content='width=device-width,initial-scale=1'>
<title>Veille IA – Militaire</title>
<link href='https://cdn.jsdelivr.net/npm/tailwindcss@2.2.19/dist/tailwind.min.css' rel='stylesheet'>
<style>.summary{{max-height:4.5rem;overflow:hidden;display:-webkit-box;-webkit-line-clamp:3;-webkit-box-orient:vertical}}</style></head>
<body class='bg-gray-50'>
<header class='bg-blue-900 text-white'><div class='max-w-7xl mx-auto px-4 py-6 flex justify-between'>
  <div><h1 class='text-2xl font-bold'>Veille IA – Militaire</h1>
  <div class='text-blue-200 text-sm'>Fenêtre {CFG.days_window} j • Généré : {now} • Seuil pertinence : {CFG.relevance_min}</div></div>
  <button id='csv' class='bg-blue-600 hover:bg-blue-500 px-4 py-2 rounded'>Exporter CSV</button>
</div></header>
<main class='max-w-7xl mx-auto px-4 py-6'>
  <div class='grid grid-cols-1 md:grid-cols-5 gap-4 mb-6'>
    {self._stat("Articles", stats['tot'], "blue")}
    {self._stat("Priorité Haute", stats['high'], "red")}
    {self._stat("Sources actives", stats['src'], "green")}
    {self._stat("Traduit FR", stats['tr'], "purple")}
    <div class='bg-white rounded shadow p-4 text-center'><div class='text-sm text-gray-600'>Pertinence moyenne</div><div class='text-xl font-semibold'>{stats['avg']}</div></div>
  </div>
  <div class='bg-white rounded shadow p-4 mb-4'>
    <div class='grid grid-cols-1 md:grid-cols-4 gap-4'>
      <input id='q' type='search' placeholder='Recherche…' class='border rounded px-3 py-2'>
      <select id='lvl' class='border rounded px-3 py-2'><option value=''>Niveau (tous)</option><option>HIGH</option><option>MEDIUM</option><option>LOW</option></select>
      <input id='src' type='search' placeholder='Filtrer par source…' class='border rounded px-3 py-2'>
      <select id='cat' class='border rounded px-3 py-2'><option value=''>Catégorie (toutes)</option>{''.join(f"<option>{c}</option>" for c in cats)}</select>
    </div>
  </div>
  <div class='bg-white rounded shadow overflow-x-auto'>
    <table class='min-w-full'>
      <thead class='bg-blue-50'><tr>{''.join(f'<th class="p-3 text-left">{h}</th>' for h in ['Date','Source','Article','Résumé (FR)','Score','Niveau','Catégorie','Pertinence','Tags'])}</tr></thead>
      <tbody id='tbody'>{rows}</tbody>
    </table>
  </div>
</main>
<script>(function(){
  const rows=[...document.querySelectorAll('#tbody tr')];
  const q=document.getElementById('q');const lvl=document.getElementById('lvl');const src=document.getElementById('src');const cat=document.getElementById('cat');
  function filter(){const qv=q.value.toLowerCase(),lv=lvl.value,sv=src.value.toLowerCase(),cv=cat.value;rows.forEach(r=>{const t=r.innerText.toLowerCase(),rl=r.dataset.level,rs=r.dataset.src.toLowerCase(),rc=r.dataset.cat;let ok=true;if(qv&&!t.includes(qv))ok=false;if(lv&&rl!==lv)ok=false;if(sv&&!rs.includes(sv))ok=false;if(cv&&rc!==cv)ok=false;r.style.display=ok?'':'none';});}
  [q,lvl,src,cat].forEach(el=>el.addEventListener('input',filter));
  document.getElementById('csv').addEventListener('click',()=>{const head=["Titre","Lien","Date","Source","Résumé","Niveau","Score","Catégorie","Pertinence","Tags"];const data=rows.filter(r=>r.style.display!=='none').map(r=>[...r.querySelectorAll('td')].map(td=>td.innerText.trim()));const csv=[head,...data].map(l=>l.map(x=>`"${x.replace(/"/g,'""')}"`).join(',')).join('\n');const blob=new Blob([csv],{type:'text/csv'});const a=document.createElement('a');a.href=URL.createObjectURL(blob);a.download='veille_ia_militaire.csv';a.click();});})();</script>
</body></html>"""

    def _stat(self, label: str, val: int, col: str) -> str:
        return f"<div class='bg-white rounded shadow p-4 text-center'><div class='text-3xl font-bold text-{col}-700'>{val}</div><div class='text-gray-600'>{label}</div></div>"

    def _row(self, a: Article) -> str:
        tr = " 🇫🇷" if a.translated else ""
        level_col = {"HIGH":"red","MEDIUM":"orange","LOW":"green"}[a.priority]
        return (
            f"<tr class='hover:bg-gray-50' data-level='{a.priority}' data-src='{html.escape(a.source)}' data-cat='{a.category}'>"
            f"<td class='p-3 text-sm text-gray-600'>{a.date.strftime('%Y-%m-%d')}</td>"
            f"<td class='p-3 text-xs'><span class='bg-blue-100 text-blue-800 px-2 py-1 rounded'>{html.escape(a.source)}</span></td>"
            f"<td class='p-3'><a class='text-blue-700 hover:underline font-semibold' href='{html.escape(a.link)}' target='_blank'>{html.escape(a.title)}</a></td>"
            f"<td class='p-3 text-sm summary'>{html.escape(a.summary)}{tr}</td>"
            f"<td class='p-3 text-center'><span class='bg-indigo-100 text-indigo-800 px-2 py-1 rounded text-sm'>{a.kw_score}</span></td>"
            f"<td class='p-3 text-center'><span class='text-white px-2 py-1 rounded text-xs bg-{level_col}-600'>{a.priority}</span></td>"
            f"<td class='p-3 text-sm'><span class='px-2 py-1 rounded text-white text-xs bg-teal-700'>{a.category}</span></td>"
            f"<td class='p-3 text-center text-sm'><span class='bg-gray-100 text-gray-800 px-2 py-1 rounded'>{a.relevance}</span></td>"
            f"<td class='p-3 text-sm'>{html.escape(', '.join(a.tags) or '—')}</td></tr>"
        )

# ============================================================================
# 🚀 MAIN PIPELINE
# ============================================================================

def main():
    log.info(f"Start • window={CFG.days_window}d • min={CFG.relevance_min}")

    collected: List[Article] = []
    seen: Set[str] = set()

    for src, meta in RSS_SOURCES.items():
        feed = CL.fetch(meta["url"])
        entries = feed.entries or []
        log.info(f"{src}: {len(entries)} entrées")
        for e in entries:
            art = AN.process(e
