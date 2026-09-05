#!/usr/bin/env python3
"""Cluster SEO keywords in-process with TF-IDF and scikit-learn estimators.

The shared handler layer calls :func:`run_clusterer` directly; the module has no
subprocess or transport protocol. It supports K-Means, DBSCAN, and agglomerative
clustering, with optional Snowball stemming and stop-word removal.

Heavy optional imports are guarded so the base SEOHEAD package remains importable
without clustering dependencies. A clear result error is returned only when the
clusterer is invoked without the required libraries.
"""

from __future__ import annotations

from typing import Any

# --- Guarded heavy dependencies -------------------------------------------
# Importing this module must never fail when scikit-learn / numpy are absent.
try:
    from sklearn.cluster import DBSCAN, AgglomerativeClustering, KMeans
    from sklearn.feature_extraction.text import TfidfVectorizer
    from sklearn.preprocessing import normalize

    _SKLEARN_OK = True
    _SKLEARN_ERR: str | None = None
except ImportError as exc:  # pragma: no cover - exercised only without deps
    _SKLEARN_OK = False
    _SKLEARN_ERR = str(exc)

# Optional stemming. ``snowballstemmer`` is the reference Snowball
# implementation: pure Python, no data corpora, and no network access.
try:
    import snowballstemmer

    _STEMMER_OK = True
except ImportError:
    _STEMMER_OK = False


# --- Pure helpers ----------------------------------------------------------
def preprocess(
    keywords: list[str],
    language: str = "russian",
    do_stem: bool = True,
    do_stopwords: bool = True,
) -> list[str]:
    """Lower-case, then optionally Snowball-stem each keyword.

    ``do_stopwords`` is kept in the signature for parity with the original
    (stop-word removal happens later, inside the TF-IDF vectorizer).
    """
    processed = [kw.lower() for kw in keywords]

    if do_stem and _STEMMER_OK:
        lang_map = {"russian": "russian", "english": "english", "auto": "russian"}
        stem_lang = lang_map.get(language, "russian")
        try:
            stemmer = snowballstemmer.stemmer(stem_lang)
            processed = [" ".join(stemmer.stemWords(kw.split())) for kw in processed]
        except Exception:
            # Stemming is best-effort; fall back to the lower-cased forms.
            pass

    return processed


def get_stop_words(language: str) -> list[str]:
    """Return the built-in stop-word list for ``language``.

    The list is bundled rather than downloaded: the clusterer must stay usable
    offline and must not fetch corpora over the network at call time.
    """
    ru_stops = [
        "и",
        "в",
        "не",
        "на",
        "с",  # noqa: RUF001 - intentional Russian stop word
        "по",
        "для",
        "это",
        "а",  # noqa: RUF001 - intentional Russian stop word
        "что",
        "как",
        "то",
        "все",
        "он",
        "она",
        "они",
        "мы",
        "вы",
        "я",
        "к",
        "из",
        "за",
    ]
    en_stops = [
        "the",
        "a",
        "an",
        "and",
        "or",
        "but",
        "in",
        "on",
        "at",
        "to",
        "for",
        "of",
        "with",
        "by",
        "from",
        "is",
        "are",
        "was",
        "were",
    ]
    if language == "english":
        return en_stops
    return ru_stops


def detect_language(keywords: list[str]) -> str:
    """Guess 'russian' vs 'english' by comparing Latin/Cyrillic letter counts."""
    sample = " ".join(keywords[:500]).lower()
    if not sample:
        return "russian"
    latin = sum(1 for ch in sample if "a" <= ch <= "z")
    cyrillic = sum(1 for ch in sample if "а" <= ch <= "я" or ch == "ё")  # noqa: RUF001
    return "english" if latin > cyrillic else "russian"


def cluster_name(keywords_in_cluster: list[str]) -> str:
    """Name a cluster after its most frequent word longer than 3 characters."""
    freq: dict[str, int] = {}
    for kw in keywords_in_cluster:
        for word in kw.lower().split():
            if len(word) > 3:
                freq[word] = freq.get(word, 0) + 1
    if not freq:
        return "Cluster"
    return max(freq, key=freq.get)


def elbow_k(X, max_k: int = 50) -> int:
    """Estimate an optimal K for K-Means via the elbow of the inertia curve."""
    inertias: list[float] = []
    k_range = range(2, min(max_k + 1, X.shape[0] // 2))
    for k in k_range:
        km = KMeans(n_clusters=k, random_state=42, n_init=5, max_iter=100)
        km.fit(X)
        inertias.append(km.inertia_)

    if len(inertias) < 3:
        return 2
    diffs = [inertias[i] - inertias[i + 1] for i in range(len(inertias) - 1)]
    diffs2 = [diffs[i] - diffs[i + 1] for i in range(len(diffs) - 1)]
    elbow_idx = diffs2.index(max(diffs2)) + 2
    return list(k_range)[elbow_idx]


# --- Main entry point ------------------------------------------------------
def run_clusterer(params: dict[str, Any]) -> dict[str, Any]:
    """Cluster a list of keywords in-process.

    Parameters (``params`` dict):
        keywords:    list[str] to cluster (required, non-empty).
        algorithm:   'kmeans' | 'dbscan' | 'agglomerative' (default 'kmeans').
                     Alias 'method' is also accepted for parity with the
                     original protocol.
        n_clusters:  target cluster count for kmeans/agglomerative
                     (default 20; alias 'k').
        auto_k:      bool — if true, pick K via the elbow method (kmeans).
        eps:         DBSCAN neighbourhood radius (cosine, default 0.3).
        min_samples: DBSCAN core-point threshold (default 2).
        threshold:   optional agglomerative distance_threshold; when given,
                     n_clusters is ignored and cut by distance instead.
        language:    'russian' | 'english' | 'auto' (default 'russian').
        stem:        bool — apply Snowball stemming (default True).
        stopwords:   bool — remove stop-words in TF-IDF (default True).
        max_features: TF-IDF vocabulary cap (default 10000).

    Returns on success::

        {"ok": True,
         "clusters": [{"label": str, "keywords": [str, ...]}, ...],
         "algorithm": str,
         "count": int}

    On any failure returns ``{"ok": False, "error": str}`` — never raises.
    """
    if not _SKLEARN_OK:
        return {
            "ok": False,
            "error": (
                f"Required library is not installed: {_SKLEARN_ERR}. "
                "Run: pip install scikit-learn numpy"
            ),
        }

    try:
        keywords = params.get("keywords")
        if not isinstance(keywords, list) or not keywords:
            return {"ok": False, "error": "The keyword list is missing or empty"}

        # Keep only non-empty string keywords, stripped.
        keywords = [str(kw).strip() for kw in keywords if str(kw).strip()]
        if not keywords:
            return {"ok": False, "error": "The keyword list is empty after normalization"}

        # 'algorithm' is the new contract name; 'method' the legacy one.
        algorithm = params.get("algorithm") or params.get("method") or "kmeans"
        algorithm = str(algorithm).lower()

        n_clusters = int(params.get("n_clusters", params.get("k", 20)))
        auto_k = bool(params.get("auto_k", False))
        eps = float(params.get("eps", 0.3))
        min_samples = int(params.get("min_samples", 2))
        threshold = params.get("threshold")
        if threshold is not None:
            threshold = float(threshold)

        language = params.get("language", "russian")
        do_stem = bool(params.get("stem", True))
        do_stopwords = bool(params.get("stopwords", True))
        max_features = int(params.get("max_features", 10000))

        if language == "auto":
            language = detect_language(keywords)

        # Preprocess + TF-IDF vectorize.
        processed = preprocess(keywords, language, do_stem, do_stopwords)
        stop_words = get_stop_words(language) if do_stopwords else None

        vectorizer = TfidfVectorizer(
            max_features=max_features,
            ngram_range=(1, 2),
            stop_words=stop_words,
            sublinear_tf=True,
        )
        try:
            X = vectorizer.fit_transform(processed)
        except Exception as exc:
            return {"ok": False, "error": f"Vectorization failed: {exc}"}

        labels = _cluster(
            X,
            algorithm=algorithm,
            n_clusters=n_clusters,
            auto_k=auto_k,
            eps=eps,
            min_samples=min_samples,
            threshold=threshold,
            n_keywords=len(keywords),
        )
        if labels is None:
            return {
                "ok": False,
                "error": f"Unknown clustering algorithm: {algorithm}",
            }

        # Group keywords by cluster label.
        clusters: dict[int, list[str]] = {}
        for idx, label in enumerate(labels):
            clusters.setdefault(int(label), []).append(keywords[idx])

        # Largest clusters first.
        sorted_clusters = sorted(clusters.items(), key=lambda x: -len(x[1]))

        output_clusters = []
        for label, kws in sorted_clusters:
            # DBSCAN noise (-1) becomes the "no cluster" bucket.
            name = "Unclustered" if label == -1 else cluster_name(kws)
            output_clusters.append({"label": name, "keywords": kws})

        return {
            "ok": True,
            "clusters": output_clusters,
            "algorithm": algorithm,
            "count": len(output_clusters),
        }
    except Exception as exc:  # last-resort guard: never crash the process
        return {"ok": False, "error": f"Clustering failed: {exc}"}


def _cluster(
    X,
    *,
    algorithm: str,
    n_clusters: int,
    auto_k: bool,
    eps: float,
    min_samples: int,
    threshold: float | None,
    n_keywords: int,
):
    """Dispatch to the requested sklearn estimator; return label array or None."""
    if algorithm == "kmeans":
        if auto_k:
            # elbow_k() falls back to 2 whenever it collects fewer than three
            # inertia points (always true below ~6 keywords), so its result is
            # not itself bounded by the corpus size and must be capped here,
            # the same way the explicit n_clusters path already is below.
            k = min(elbow_k(normalize(X), max_k=min(100, n_keywords // 5)), n_keywords)
        else:
            k = min(n_clusters, n_keywords)
        k = max(k, 1)
        km = KMeans(n_clusters=k, random_state=42, n_init=10, max_iter=300)
        return km.fit_predict(X)

    if algorithm == "dbscan":
        Xn = normalize(X)
        db = DBSCAN(
            eps=eps,
            min_samples=min_samples,
            metric="cosine",
            algorithm="brute",
            n_jobs=-1,
        )
        return db.fit_predict(Xn)

    if algorithm == "agglomerative":
        # Large datasets: fall back to K-Means (dense agglomerative is O(n^2)).
        if X.shape[0] > 10000:
            k = max(min(n_clusters, n_keywords), 1)
            km = KMeans(n_clusters=k, random_state=42, n_init=10)
            return km.fit_predict(X)

        Xn = normalize(X).toarray()
        if threshold is not None:
            # Cut by distance rather than a fixed cluster count.
            ag = AgglomerativeClustering(
                n_clusters=None,
                distance_threshold=threshold,
                metric="cosine",
                linkage="average",
            )
        else:
            k = max(min(n_clusters, n_keywords), 1)
            ag = AgglomerativeClustering(n_clusters=k, metric="cosine", linkage="average")
        return ag.fit_predict(Xn)

    return None


# --- Smoke test (no network, no heavy deps required) -----------------------
if __name__ == "__main__":
    # Exercise pure helpers only — safe to run without sklearn or network access.
    assert detect_language(["hello world", "quick brown fox"]) == "english"
    assert detect_language(["привет мир", "рыжая лиса"]) == "russian"
    assert detect_language([]) == "russian"

    name = cluster_name(["купить айфон", "айфон цена", "чехол айфон"])
    assert name == "айфон", f"unexpected cluster name: {name}"
    assert cluster_name(["a b c"]) == "Cluster"  # no word > 3 chars

    stops = get_stop_words("english")
    assert "the" in stops

    # run_clusterer must degrade gracefully when sklearn is missing.
    if not _SKLEARN_OK:
        res = run_clusterer({"keywords": ["a", "b"]})
        assert res["ok"] is False and "error" in res

    # Empty input is rejected without raising.
    res = run_clusterer({"keywords": []})
    assert res["ok"] is False

    print("clusterer.py self-check passed")
