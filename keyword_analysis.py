"""
Keyword density analysis for resume and cover letter.
Verifies JD keywords appear in generated documents.
"""

import re
import sqlite3
from collections import Counter
from pathlib import Path
from typing import Dict, List, Set

from db_setup import DB_PATH

# ─────────────────────────────────────────────
#  SHARED BULLET-OPENER VOCABULARY
#  Single source of truth: pipeline.py lints generated bullets against these and
#  this module scores them. Two different lists meant the generator rejected an
#  opener the report then praised.
# ─────────────────────────────────────────────

# Activity verbs — they describe motion, not outcome, and push the metric out of
# the first line. "Built/Implemented/Developed/Created" belong here: they say what
# was made, never what it changed.
WEAK_OPENERS = {
    'worked', 'assisted', 'helped', 'participated', 'supported', 'contributed',
    'involved', 'learned', 'studied', 'explored', 'researched', 'analyzed',
    'reviewed', 'tested', 'documented', 'maintained', 'updated', 'managed',
    'utilized', 'used', 'tasked', 'collaborated', 'aided', 'performed',
    'conducted', 'made', 'handled', 'responsible',
    'built', 'implemented', 'developed', 'created', 'wrote',
}

# Impact verbs — each one implies a measurable delta.
STRONG_OPENERS = {
    'accelerated', 'architected', 'automated', 'consolidated', 'converted',
    'cut', 'decreased', 'delivered', 'deployed', 'designed', 'doubled', 'drove',
    'eliminated', 'enabled', 'engineered', 'enhanced', 'established', 'expanded',
    'exposed', 'extended', 'halved', 'hardened', 'improved', 'increased',
    'instrumented', 'integrated', 'launched', 'migrated', 'minimized',
    'optimized', 'parallelized', 'prevented', 'rearchitected', 'rebuilt',
    'reduced', 'refactored', 'removed', 'replaced', 'resolved', 'restructured',
    'scaled', 'secured', 'shipped', 'slashed', 'streamlined', 'strengthened',
    'surfaced', 'tripled', 'tuned', 'unblocked', 'unified',
}


def extract_keywords(text: str, min_length: int = 3) -> Set[str]:
    """Extract meaningful keywords from text (lowercase, alphanumeric only)."""
    words = re.findall(r'\b[a-zA-Z][a-zA-Z0-9+#.-]{2,}\b', text.lower())
    stopwords = {
        'the', 'and', 'for', 'are', 'but', 'not', 'you', 'all', 'any', 'can',
        'her', 'was', 'one', 'our', 'out', 'day', 'get', 'has', 'him', 'his',
        'how', 'its', 'may', 'new', 'now', 'old', 'see', 'two', 'way', 'who',
        'boy', 'did', 'she', 'use', 'with', 'from', 'they', 'have', 'this',
        'that', 'will', 'your', 'when', 'make', 'like', 'time', 'just', 'him',
        'into', 'over', 'think', 'also', 'back', 'after', 'first', 'well',
        'year', 'work', 'such', 'take', 'give', 'most', 'very', 'what',
        'must', 'because', 'through', 'before', 'between', 'under', 'while',
        'where', 'should', 'each', 'about', 'more', 'these', 'been', 'other',
        'than', 'then', 'there', 'some', 'could', 'would', 'their', 'into',
        'only', 'than', 'been', 'many', 'more', 'very', 'after', 'most',
        'such', 'even', 'well', 'were', 'said', 'does', 'part', 'come',
        'made', 'may', 'over', 'did', 'down', 'much', 'take', 'know', 'see',
    }
    return {w for w in words if w not in stopwords and len(w) >= min_length}


def extract_tech_keywords(text: str) -> Set[str]:
    """Extract technology-specific keywords (case-sensitive for acronyms)."""
    # Known tech terms to look for (avoid generic acronym matching)
    known_tech_terms = {
        # Cloud & Platforms
        'aws', 'gcp', 'azure', 'cloud', 'heroku', 'vercel', 'netlify', 'firebase',
        # Languages
        'java', 'python', 'go', 'golang', 'typescript', 'javascript', 'c++', 'c#', 'rust', 'kotlin', 'swift', 'scala',
        # Frameworks
        'spring boot', 'spring webflux', 'spring cloud', 'spring security', 'node.js', 'fastapi', 'django', 'flask', 'express',
        # Databases
        'kafka', 'rabbitmq', 'redis', 'mongodb', 'postgresql', 'postgres', 'mysql', 'dynamodb', 'cassandra', 'sqlite', 'typesense',
        # DevOps & Infrastructure
        'kubernetes', 'k8s', 'docker', 'github actions', 'gitlab ci', 'jenkins', 'ci/cd', 'linux', 'git', 'terraform', 'ansible', 'helm', 'argocd', 'flux',
        # Observability
        'prometheus', 'grafana', 'loki', 'zipkin', 'jaeger', 'elk', 'datadog',
        # Architecture & Protocols
        'microservices', 'rest', 'graphql', 'grpc', 'websocket', 'mqtt', 'oauth', 'jwt', 'iam', 'sso', 'openid',
        # Frontend
        'react', 'vue', 'angular', 'next.js', 'html', 'css', 'tailwind', 'sass',
        # Other Tech
        'ffmpeg', 'hls', 's3', 'minio', 'lua', 'bcrypt',
        # Concepts
        'distributed systems', 'observability', 'sre', 'devops', 'backend', 'frontend', 'full stack', 'fullstack',
        'rate limiting', 'load balancing', 'autoscaling', 'auto scaling', 'container orchestration',
        'event driven', 'async', 'reactive', 'stream processing', 'data pipeline',
        # Specific tools from resume
        'spring', 'webflux', 'cloud gateway', 'sse', 'async', 'loki', 'prometheus', 'grafana', 'zipkin',
        'oauth 2.0', 'azure ad', 'global exception handler', 'ffmpeg', 'hls', 'api key', 'bcrypt', 'minio',
        'cloud run', 'gcp cloud run', 'redis lua', 'lua script', 'atomic', 'concurrency', 'benchmark',
        'webflux', 's3asyncclient', 'reactive', 'zipkin trace', 'trace-id', 'structured log', 'loki',
        'api gateway', 'scope-grained', 'multi-tenant', 'personalization', 're-ranker', 'weighted',
        'autocomplete', 'geolocation', 'typesense', 'parallel worker', 'data import', 'async event',
    }
    keywords = set()
    text_lower = text.lower()
    for term in known_tech_terms:
        if re.search(r'\b' + re.escape(term) + r'\b', text_lower):
            keywords.add(term)
    return keywords


def _resume_data_to_text(resume_data: dict) -> str:
    parts = []
    for exp in resume_data.get("experience", []):
        parts.append(exp.get("company", ""))
        parts.append(exp.get("role", ""))
        parts.extend(exp.get("bullets", []))
    for proj in resume_data.get("projects", []) + resume_data.get("open_source", []):
        parts.append(proj.get("title", ""))
        parts.append(proj.get("tech", ""))
        parts.extend(proj.get("bullets", []))
    for row in resume_data.get("skills", []):
        if len(row) >= 2:
            parts.append(row[1])
    summary = resume_data.get("summary")
    if summary:
        parts.append(summary)
    return " ".join(parts)


def analyze_keyword_coverage(jd_text: str, resume_text, cover_letter_text: str, use_corpus: bool = True) -> Dict:
    """Analyze how well JD keywords are covered in resume and cover letter."""
    if isinstance(resume_text, dict):
        resume_text = _resume_data_to_text(resume_text)

    jd_keywords = extract_keywords(jd_text)
    jd_tech_keywords = extract_tech_keywords(jd_text)
    jd_all_keywords = jd_keywords | jd_tech_keywords

    resume_words = extract_keywords(resume_text) | extract_tech_keywords(resume_text)
    cl_words = extract_keywords(cover_letter_text) | extract_tech_keywords(cover_letter_text)

    # Overall coverage
    covered_in_resume = jd_all_keywords & resume_words
    covered_in_cl = jd_all_keywords & cl_words
    covered_anywhere = covered_in_resume | covered_in_cl

    missing = jd_all_keywords - covered_anywhere

    # Tech keyword specific coverage
    missing_tech = jd_tech_keywords - covered_anywhere

    # Corpus-based insights
    corpus_insights = {}
    if use_corpus:
        corpus = build_keyword_corpus_from_db(min_freq=2)
        # High-value keywords from corpus that are in this JD but missing from resume
        high_value_missing = (jd_tech_keywords & corpus) - covered_anywhere
        # High-value keywords from corpus that ARE covered (good signal)
        high_value_covered = (jd_tech_keywords & corpus) & covered_anywhere
        # Corpus keywords NOT in this JD but in resume (bonus)
        bonus_keywords = (corpus - jd_all_keywords) & resume_words
        
        corpus_insights = {
            "high_value_missing": sorted(high_value_missing),
            "high_value_covered": sorted(high_value_covered),
            "bonus_keywords_in_resume": sorted(bonus_keywords)[:20],
            "corpus_size": len(corpus),
        }

    return {
        "jd_total_keywords": len(jd_all_keywords),
        "jd_tech_keywords": len(jd_tech_keywords),
        "covered_in_resume": len(covered_in_resume),
        "covered_in_cover_letter": len(covered_in_cl),
        "covered_anywhere": len(covered_anywhere),
        "coverage_pct": round(len(covered_anywhere) / len(jd_all_keywords) * 100, 1) if jd_all_keywords else 100,
        "missing_keywords": sorted(missing),
        "missing_tech_keywords": sorted(missing_tech),
        "top_missing": sorted(missing, key=lambda x: -len(x))[:20],
        **corpus_insights,
    }


def analyze_resume_bullets(resume_data: dict) -> Dict:
    """Analyze resume bullet quality metrics."""
    all_bullets = []
    for exp in resume_data.get("experience", []):
        all_bullets.extend(exp.get("bullets", []))
    for proj in resume_data.get("projects", []) + resume_data.get("open_source", []):
        all_bullets.extend(proj.get("bullets", []))

    strong_verbs = STRONG_OPENERS
    weak_verbs = WEAK_OPENERS

    metrics = {
        "total_bullets": len(all_bullets),
        "bullets_with_metrics": 0,
        "bullets_starting_strong": 0,
        "bullets_starting_weak": 0,
        "avg_bullet_length": 0,
        "strong_verb_bullets": [],
        "weak_verb_bullets": [],
    }

    total_len = 0
    for bullet in all_bullets:
        total_len += len(bullet)
        first_word = bullet.split()[0].lower().rstrip('.,;:') if bullet.split() else ""
        
        if any(char.isdigit() for char in bullet):
            metrics["bullets_with_metrics"] += 1
        if first_word in strong_verbs:
            metrics["bullets_starting_strong"] += 1
            metrics["strong_verb_bullets"].append(bullet)
        elif first_word in weak_verbs:
            metrics["bullets_starting_weak"] += 1
            metrics["weak_verb_bullets"].append(bullet)

    metrics["avg_bullet_length"] = round(total_len / len(all_bullets), 1) if all_bullets else 0
    metrics["metric_coverage_pct"] = round(metrics["bullets_with_metrics"] / len(all_bullets) * 100, 1) if all_bullets else 0
    metrics["strong_verb_pct"] = round(metrics["bullets_starting_strong"] / len(all_bullets) * 100, 1) if all_bullets else 0

    return metrics


def build_keyword_corpus_from_db(min_freq: int = 2) -> Set[str]:
    """Build a set of high-frequency tech keywords from all JDs in the database."""
    conn = sqlite3.connect(DB_PATH)
    rows = conn.execute("SELECT jd FROM jobs WHERE jd IS NOT NULL AND jd != ''").fetchall()
    conn.close()
    
    if not rows:
        return set()
    
    all_text = " ".join(row[0] for row in rows)
    tech_keywords = extract_tech_keywords(all_text)
    
    # Count frequency across all JDs
    freq = Counter()
    for row in rows:
        jd_text = row[0]
        found = extract_tech_keywords(jd_text)
        freq.update(found)
    
    # Keep keywords that appear in at least min_freq job descriptions
    return {kw for kw, count in freq.items() if count >= min_freq}


def get_corpus_stats() -> Dict:
    """Get statistics about the keyword corpus."""
    conn = sqlite3.connect(DB_PATH)
    total_jobs = conn.execute("SELECT COUNT(*) FROM jobs WHERE jd IS NOT NULL AND jd != ''").fetchone()[0]
    conn.close()
    
    corpus = build_keyword_corpus_from_db(min_freq=1)
    freq_all = Counter()
    conn = sqlite3.connect(DB_PATH)
    rows = conn.execute("SELECT jd FROM jobs WHERE jd IS NOT NULL AND jd != ''").fetchall()
    conn.close()
    
    for row in rows:
        freq_all.update(extract_tech_keywords(row[0]))
    
    return {
        "total_jobs_in_db": total_jobs,
        "unique_tech_keywords": len(corpus),
        "top_keywords": freq_all.most_common(30),
    }


def print_analysis_report(keyword_results: Dict, bullet_results: Dict):
    """Print formatted analysis report."""
    print("\n" + "=" * 60)
    print("KEYWORD COVERAGE ANALYSIS")
    print("=" * 60)
    print(f"JD Total Keywords:       {keyword_results['jd_total_keywords']}")
    print(f"JD Tech Keywords:        {keyword_results['jd_tech_keywords']}")
    print(f"Covered in Resume:       {keyword_results['covered_in_resume']}")
    print(f"Covered in Cover Letter: {keyword_results['covered_in_cover_letter']}")
    print(f"Covered Anywhere:        {keyword_results['covered_anywhere']}")
    print(f"Overall Coverage:        {keyword_results['coverage_pct']}%")
    
    if keyword_results['missing_tech_keywords']:
        print(f"\nMISSING TECH KEYWORDS ({len(keyword_results['missing_tech_keywords'])}):")
        for kw in keyword_results['missing_tech_keywords'][:15]:
            print(f"  - {kw}")
        if len(keyword_results['missing_tech_keywords']) > 15:
            print(f"  ... and {len(keyword_results['missing_tech_keywords']) - 15} more")

    # Corpus-based insights
    if 'high_value_missing' in keyword_results and keyword_results['high_value_missing']:
        print(f"\nHIGH-VALUE MISSING (from {keyword_results.get('corpus_size', 0)}-keyword corpus, freq>=2):")
        for kw in keyword_results['high_value_missing'][:15]:
            print(f"  - {kw}")
        if len(keyword_results['high_value_missing']) > 15:
            print(f"  ... and {len(keyword_results['high_value_missing']) - 15} more")

    if 'high_value_covered' in keyword_results and keyword_results['high_value_covered']:
        print(f"\nHIGH-VALUE COVERED:")
        for kw in keyword_results['high_value_covered'][:15]:
            print(f"  + {kw}")

    if 'bonus_keywords_in_resume' in keyword_results and keyword_results['bonus_keywords_in_resume']:
        print(f"\nBONUS KEYWORDS (in resume, not in JD but common in corpus):")
        for kw in keyword_results['bonus_keywords_in_resume'][:15]:
            print(f"  + {kw}")

    print("\n" + "=" * 60)
    print("BULLET QUALITY ANALYSIS")
    print("=" * 60)
    print(f"Total Bullets:           {bullet_results['total_bullets']}")
    print(f"Bullets with Metrics:    {bullet_results['bullets_with_metrics']} ({bullet_results['metric_coverage_pct']}%)")
    print(f"Bullets Starting Strong: {bullet_results['bullets_starting_strong']} ({bullet_results['strong_verb_pct']}%)")
    print(f"Bullets Starting Weak:   {bullet_results['bullets_starting_weak']}")
    print(f"Avg Bullet Length:       {bullet_results['avg_bullet_length']} chars")

    if bullet_results['weak_verb_bullets']:
        print(f"\nWEAK VERB BULLETS ({len(bullet_results['weak_verb_bullets'])}):")
        for b in bullet_results['weak_verb_bullets'][:5]:
            print(f"  - {b[:100]}...")

    print("=" * 60)