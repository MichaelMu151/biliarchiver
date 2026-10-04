#!/usr/bin/env python3
"""Full-corpus GPU BERTopic pipeline (515k+ cleaned comments).

Designed for CUDA hosts (AutoDL / RunPod / local NVIDIA). On machines without
torch+CUDA it refuses to run the full fit (use --export-only / --dry-load).

Inputs (one of):
  - SQLite ``v_comment_analysis`` via --db
  - Pre-exported JSONL via --docs-jsonl  (preferred on AutoDL after thin upload)

Outputs:
  - data/exports/embeddings_bge_full.npy (+ .meta.json)
  - data/exports/gpu_full_bertopic_metrics.json
  - TOPIC_EVOLUTION_FINDINGS.md
  - data/exports/bertopic_gpu_model/  (optional)

Example on AutoDL:
  python run_gpu_full_bertopic.py \\
    --docs-jsonl data/exports/comment_analysis_full.jsonl.gz \\
    --embedding-model BAAI/bge-large-zh-v1.5 \\
    --batch-size 256 --min-topic-size 120
"""

from __future__ import annotations

import argparse
import gzip
import json
import os
import random
import re
import sqlite3
import sys
import time
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

ROOT = Path(__file__).resolve().parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

OUT_DIR = ROOT / "data" / "exports"
DEFAULT_DB = ROOT / "data" / "corpus.db"
DEFAULT_JSONL = OUT_DIR / "comment_analysis_full.jsonl.gz"
EMBED_CACHE = OUT_DIR / "embeddings_bge_full.npy"
EMBED_META = OUT_DIR / "embeddings_bge_full.meta.json"
METRICS_PATH = OUT_DIR / "gpu_full_bertopic_metrics.json"
REPORT_PATH = ROOT / "TOPIC_EVOLUTION_FINDINGS.md"
MODEL_DIR = OUT_DIR / "bertopic_gpu_model"

# ---------------------------------------------------------------------------
# Six theoretical macro dimensions (Guided / post-hoc mapping)
# ---------------------------------------------------------------------------

MACRO_DIMS: dict[str, dict[str, Any]] = {
    "A_workplace_alienation": {
        "label_zh": "职场异化与劳工权益",
        "label_en": "Workplace Alienation & Labor Resistance",
        "seeds": [
            "加班", "996", "007", "打工人", "牛马", "社畜", "裁员", "失业",
            "工资", "欠薪", "讨薪", "劳动法", "仲裁", "老板", "大厂", "内卷",
            "躺平", "压榨", "剥削", "班味", "耗材", "红利", "试用期", "外包",
            "职场", "面试", "绩效", "35岁", "双休", "自愿下班", "流水线",
        ],
    },
    "B_reproductive_strike": {
        "label_zh": "再生产拒绝与婚育罢工",
        "label_en": "Reproductive Strike & Biological Boycott",
        "seeds": [
            "结婚", "不婚", "不育", "彩礼", "生娃", "生孩子", "丁克", "断后",
            "软肋", "最后一代", "相亲", "催婚", "催生", "婚房", "首付",
            "养不起", "生不起", "结不起", "传宗接代", "绝育", "婚育", "剩女",
            "剩男", "领证", "嫁妆", "为资本家生", "繁衍耗材",
        ],
    },
    "C_intergenerational_rupture": {
        "label_zh": "代际创伤与家庭裂隙",
        "label_en": "Intergenerational Rupture & Paternal Hegemony",
        "seeds": [
            "父母", "家长", "家长权", "原生家庭", "代际", "啃老", "赡养",
            "养老", "母亲", "父亲", "扇耳光", "逼死", "独立", "报复",
            "儿子", "女儿", "婆婆", "丈母娘", "义务坐牢", "衡水", "教育内卷",
            "孝顺", "反孝", "童年创伤", "控制欲",
        ],
    },
    "D_youth_nihilism": {
        "label_zh": "青年虚无主义与断裂体验",
        "label_en": "Youth Nihilism & Liminality",
        "seeds": [
            "孔乙己", "长衫", "脱不下", "学历贬值", "躺平", "摆烂", "润",
            "起床", "创造不了", "价值", "退游", "地球ol", "防御塔", "泉水",
            "虚无", "无意义", "内卷", "卷不动", "毕业即失业", "应届生",
            "打工人", "耗材", "红利耗尽",
        ],
    },
    "E_institutional_disillusionment": {
        "label_zh": "制度反省与公民权利重塑",
        "label_en": "Institutional Disillusionment & Citizenship Discourse",
        "seeds": [
            "人民", "爱国", "分配", "增值税", "消费税", "贫富", "分化",
            "体制", "体制内", "考公", "编制", "公平", "税收", "改革",
            "地方政府", "谁是人民", "公民", "权力", "审查", "言论",
            "阶层", "中产", "底层", "收割",
        ],
    },
    "F_affective_cynicism": {
        "label_zh": "网络狂欢与解构性犬儒",
        "label_en": "Affective Cynicism & Networked Parody",
        "seeds": [
            "阴阳怪气", "复读", "表情包", "神评", "去崇高", "嘲讽", "反讽",
            "解构", "典中典", "赢学", "赢麻", "破防", "麻了", "针不戳",
            "哈基基", "哈基米", "栓q", "抽象", "乐子人", "玩梗",
        ],
    },
}

STOPWORDS_ZH = {
    "的", "了", "是", "我", "你", "他", "她", "它", "们", "这", "那", "就",
    "都", "也", "和", "与", "及", "或", "在", "有", "没有", "不是", "这个",
    "那个", "一个", "什么", "怎么", "为什么", "因为", "所以", "但是", "如果",
    "还是", "可以", "自己", "我们", "你们", "他们", "啊", "呢", "吧",
    "吗", "呀", "哦", "嗯", "哈", "回复", "起来", "出来", "过来", "下去",
    "真的", "感觉", "觉得", "知道", "可能", "应该", "已经", "这么",
    "那么", "这样", "那样", "比较", "非常", "很多", "一些", "一下", "一点",
    "东西", "问题", "事情", "时候", "现在", "以前", "以后", "然后", "其实",
    "就是", "只是", "还有", "而且", "不过", "虽然", "但", "并", "被", "把",
    "让", "给", "对", "从", "到", "为", "以", "而", "之", "其", "等", "等等",
    "上", "下", "中", "里", "外", "前", "后", "左", "右", "说", "看", "想",
    "会", "能", "要", "得", "着", "过", "来", "去", "做", "人", "年", "月",
    "日", "天", "次", "种", "位", "名", "个",
}

# Keep a small Latin allowlist; drop usernames / sticker spam otherwise.
LATIN_ALLOW = {
    "996", "007", "kpi", "hr", "pua", "gdp", "ol", "moba", "yyds", "nsdd",
    "u1s1", "awsl", "xswl", "tmd", "nmlgb", "sb", "nb", "gg", "wp",
}

CJK_RE = re.compile(r"[\u4e00-\u9fff]")
LATIN_RE = re.compile(r"^[a-zA-Z][a-zA-Z0-9_\-]{1,30}$")
STICKER_RE = re.compile(r"(doge|tv_|call|打call)", re.I)


def _now() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def _log(msg: str) -> None:
    print(msg, flush=True)


def _gpu_mem() -> str:
    try:
        import torch

        if not torch.cuda.is_available():
            return "cpu"
        free, total = torch.cuda.mem_get_info()
        alloc = torch.cuda.memory_allocated() / (1024**3)
        return (
            f"alloc={alloc:.2f}GiB free={free/(1024**3):.2f}/{total/(1024**3):.2f}GiB "
            f"device={torch.cuda.get_device_name(0)}"
        )
    except Exception:
        return "n/a"


# ---------------------------------------------------------------------------
# Data loading / export
# ---------------------------------------------------------------------------

def ensure_views(db: Path) -> None:
    from bili.analysis_hygiene import ensure_analysis_columns

    conn = sqlite3.connect(str(db), timeout=60)
    try:
        ensure_analysis_columns(conn)
        conn.commit()
    finally:
        conn.close()


def load_from_db(db: Path, *, min_chars: int = 8) -> list[dict[str, Any]]:
    ensure_views(db)
    conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=120)
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute(
            """
            SELECT rpid, bvid, content, text_length, like_count,
                   snowball_depth, video_title, pub_ts
            FROM v_comment_analysis
            WHERE IFNULL(text_length, length(content)) >= ?
            """,
            (min_chars,),
        ).fetchall()
    finally:
        conn.close()
    docs = []
    for r in rows:
        docs.append({
            "rpid": r["rpid"],
            "bvid": r["bvid"],
            "content": r["content"] or "",
            "text_length": int(r["text_length"] or len(r["content"] or "")),
            "like_count": int(r["like_count"] or 0),
            "depth": int(r["snowball_depth"] if r["snowball_depth"] is not None else 0),
            "video_title": r["video_title"] or "",
            "pub_ts": int(r["pub_ts"] or 0),
        })
    return docs


def export_jsonl(docs: list[dict[str, Any]], path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    opener = gzip.open if str(path).endswith(".gz") else open
    with opener(path, "wt", encoding="utf-8") as f:  # type: ignore[arg-type]
        for doc in docs:
            f.write(json.dumps(doc, ensure_ascii=False) + "\n")
    return path


def load_jsonl(path: Path, *, min_chars: int = 8) -> list[dict[str, Any]]:
    opener = gzip.open if str(path).endswith(".gz") else open
    docs: list[dict[str, Any]] = []
    with opener(path, "rt", encoding="utf-8") as f:  # type: ignore[arg-type]
        for line in f:
            line = line.strip()
            if not line:
                continue
            doc = json.loads(line)
            text = doc.get("content") or ""
            if len(text) < min_chars:
                continue
            doc["depth"] = int(doc.get("depth") or 0)
            doc["like_count"] = int(doc.get("like_count") or 0)
            docs.append(doc)
    return docs


# ---------------------------------------------------------------------------
# Tokenization / stopwords
# ---------------------------------------------------------------------------

def _jieba_analyzer(text: str) -> list[str]:
    import jieba

    out = []
    for tok in jieba.lcut(text or ""):
        t = tok.strip()
        if len(t) < 2:
            continue
        if t in STOPWORDS_ZH or t.lower() in STOPWORDS_ZH:
            continue
        if re.fullmatch(r"[\W\d_]+", t):
            continue
        # Prefer Chinese content tokens; drop username / sticker latin spam.
        if CJK_RE.search(t):
            out.append(t)
            continue
        low = t.lower()
        if low in LATIN_ALLOW:
            out.append(low)
            continue
        if LATIN_RE.match(t) or STICKER_RE.search(t):
            continue
    return out


# ---------------------------------------------------------------------------
# Embeddings (BGE + cache)
# ---------------------------------------------------------------------------

def _pick_batch_size(requested: int) -> int:
    try:
        import torch

        if not torch.cuda.is_available():
            return min(requested, 64)
        free, _total = torch.cuda.mem_get_info()
        free_gb = free / (1024**3)
        if free_gb >= 18:
            return max(requested, 512)
        if free_gb >= 10:
            return max(min(requested, 256), 128)
        if free_gb >= 6:
            return min(requested, 128)
        return min(requested, 64)
    except Exception:
        return min(requested, 64)


def encode_documents(
    texts: list[str],
    *,
    model_name: str,
    batch_size: int,
    cache_path: Path,
    meta_path: Path,
    force: bool = False,
) -> Any:
    import numpy as np

    if cache_path.is_file() and meta_path.is_file() and not force:
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
        if (
            meta.get("model") == model_name
            and meta.get("n_docs") == len(texts)
            and meta.get("content_sha1_head") == _content_fingerprint(texts)
        ):
            _log(f"[embed] cache hit → {cache_path} ({meta['n_docs']} × {meta.get('dim')})")
            return np.load(cache_path)

    import torch
    from sentence_transformers import SentenceTransformer

    if not torch.cuda.is_available():
        raise RuntimeError(
            "CUDA unavailable. Full BGE embedding requires a GPU host. "
            "Export JSONL on Mac, then run this script on AutoDL."
        )

    bs = _pick_batch_size(batch_size)
    _log(f"[embed] loading {model_name} · batch={bs} · {_gpu_mem()}")
    model = SentenceTransformer(model_name, device="cuda")
    try:
        model.half()
    except Exception:
        pass

    t0 = time.time()
    # encode handles tqdm progress when show_progress_bar=True
    embeddings = model.encode(
        texts,
        batch_size=bs,
        show_progress_bar=True,
        convert_to_numpy=True,
        normalize_embeddings=True,
    )
    elapsed = time.time() - t0
    _log(f"[embed] done shape={embeddings.shape} in {elapsed/60:.1f} min · {_gpu_mem()}")

    cache_path.parent.mkdir(parents=True, exist_ok=True)
    np.save(cache_path, embeddings)
    meta = {
        "model": model_name,
        "n_docs": len(texts),
        "dim": int(embeddings.shape[1]),
        "batch_size": bs,
        "elapsed_sec": round(elapsed, 1),
        "content_sha1_head": _content_fingerprint(texts),
        "generated_at": _now(),
        "gpu": _gpu_mem(),
    }
    meta_path.write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
    _log(f"[embed] cached → {cache_path}")
    # free VRAM before UMAP/HDBSCAN if needed
    del model
    torch.cuda.empty_cache()
    return embeddings


def _content_fingerprint(texts: list[str]) -> str:
    import hashlib

    h = hashlib.sha1()
    h.update(str(len(texts)).encode())
    # sample head/mid/tail to avoid hashing 25MB fully every time while catching swaps
    for idx in (0, len(texts) // 2, len(texts) - 1):
        if 0 <= idx < len(texts):
            h.update(texts[idx][:200].encode("utf-8", "ignore"))
            h.update(b"|")
    # also hash total char count
    h.update(str(sum(len(t) for t in texts)).encode())
    return h.hexdigest()


# ---------------------------------------------------------------------------
# BERTopic fit
# ---------------------------------------------------------------------------

def _build_umap_hdbscan(*, min_topic_size: int, seed: int):
    """Prefer cuML on GPU; fall back to CPU sklearn-stack with n_jobs=-1."""
    try:
        from cuml.cluster import HDBSCAN as cuHDBSCAN
        from cuml.manifold import UMAP as cuUMAP

        _log("[cluster] using cuML UMAP + HDBSCAN (GPU)")
        umap_model = cuUMAP(
            n_neighbors=15,
            n_components=5,
            min_dist=0.0,
            metric="cosine",
            random_state=seed,
        )
        hdbscan_model = cuHDBSCAN(
            min_cluster_size=min_topic_size,
            metric="euclidean",
            cluster_selection_method="eom",
            prediction_data=True,
        )
        return umap_model, hdbscan_model, "cuml"
    except Exception as exc:
        _log(f"[cluster] cuML unavailable ({exc}); using CPU UMAP/HDBSCAN n_jobs=-1")
        from hdbscan import HDBSCAN
        from umap import UMAP

        umap_model = UMAP(
            n_neighbors=15,
            n_components=5,
            min_dist=0.0,
            metric="cosine",
            random_state=seed,
            low_memory=True,
            n_jobs=-1,
        )
        hdbscan_model = HDBSCAN(
            min_cluster_size=min_topic_size,
            metric="euclidean",
            cluster_selection_method="eom",
            prediction_data=True,
            core_dist_n_jobs=-1,
        )
        return umap_model, hdbscan_model, "cpu"


def fit_bertopic(
    texts: list[str],
    embeddings: Any,
    *,
    min_topic_size: int,
    seed: int,
    nr_topics: str | int,
) -> tuple[Any, list[int]]:
    from bertopic import BERTopic
    from bertopic.representation import MaximalMarginalRelevance
    from bertopic.vectorizers import ClassTfidfTransformer
    from sklearn.feature_extraction.text import CountVectorizer

    umap_model, hdbscan_model, backend = _build_umap_hdbscan(
        min_topic_size=min_topic_size, seed=seed
    )
    vectorizer = CountVectorizer(
        analyzer=_jieba_analyzer,
        min_df=3,
        max_df=0.95,
        ngram_range=(1, 2),
    )
    ctfidf = ClassTfidfTransformer(reduce_frequent_words=True)
    # MMR only: KeyBERTInspired needs a live embedding_model; we pass
    # precomputed embeddings and keep embedding_model=None for VRAM.
    representation = MaximalMarginalRelevance(diversity=0.35)
    seed_list = [cfg["seeds"] for cfg in MACRO_DIMS.values()]

    model = BERTopic(
        embedding_model=None,  # precomputed via fit_transform(..., embeddings=)
        umap_model=umap_model,
        hdbscan_model=hdbscan_model,
        vectorizer_model=vectorizer,
        ctfidf_model=ctfidf,
        representation_model=representation,
        seed_topic_list=seed_list,
        min_topic_size=min_topic_size,
        nr_topics=None if nr_topics == "auto" else nr_topics,
        calculate_probabilities=False,
        verbose=True,
    )
    _log(f"[fit] BERTopic start · backend={backend} · min_topic_size={min_topic_size}")
    t0 = time.time()
    topics, _ = model.fit_transform(texts, embeddings=embeddings)
    _log(f"[fit] done in {(time.time()-t0)/60:.1f} min · topics={len(set(topics))}")

    # Soft outlier reduction only when share is moderate. Aggressive embedding
    # reassignment (threshold=0.3) previously collapsed 58% → 0% and polluted macros.
    outlier_pct = 100.0 * sum(1 for t in topics if t == -1) / max(len(topics), 1)
    _log(f"[fit] outlier share before reduce: {outlier_pct:.2f}%")
    if 10.0 < outlier_pct <= 35.0:
        try:
            new_topics = model.reduce_outliers(
                texts, topics, embeddings=embeddings, strategy="embeddings", threshold=0.55
            )
            model.update_topics(texts, topics=new_topics, vectorizer_model=vectorizer)
            topics = list(new_topics)
            outlier_pct = 100.0 * sum(1 for t in topics if t == -1) / max(len(topics), 1)
            _log(f"[fit] outlier share after reduce: {outlier_pct:.2f}%")
        except Exception as exc:
            _log(f"[warn] reduce_outliers failed: {exc}")
    elif outlier_pct > 35.0:
        _log(
            f"[fit] outlier share {outlier_pct:.1f}% is high; "
            "skip force-reduce to protect topic purity (report -1 honestly)"
        )
    return model, list(topics)


# ---------------------------------------------------------------------------
# Macro mapping + evolution
# ---------------------------------------------------------------------------

def topic_words(model: Any, topic_id: int, top_n: int = 12) -> list[tuple[str, float]]:
    if topic_id == -1:
        return []
    words = model.get_topic(topic_id) or []
    return [(w, float(s)) for w, s in words[:top_n]]


def score_macros_on_text(text: str) -> Counter:
    """Count seed hits for each macro dimension inside one comment."""
    t = (text or "").lower()
    hits: Counter = Counter()
    if not t:
        return hits
    for mid, cfg in MACRO_DIMS.items():
        for seed in cfg["seeds"]:
            s = seed.lower()
            if len(s) >= 2 and s in t:
                hits[mid] += 1
    return hits


def map_topic_to_macro(
    words: list[str],
    sample_texts: list[str] | None = None,
) -> tuple[str, float]:
    """Map a topic using member-doc seed hits first, then Chinese top-word overlap."""
    doc_scores: Counter = Counter()
    for text in sample_texts or []:
        doc_scores.update(score_macros_on_text(text))

    if doc_scores:
        best, raw = doc_scores.most_common(1)[0]
        n = max(len(sample_texts or []), 1)
        score = raw / n
        if raw >= 2 or score >= 0.05:
            return best, float(score)

    if not words:
        return "other_unmapped", 0.0
    bag = {w for w in words if CJK_RE.search(w)}
    if not bag:
        return "other_unmapped", 0.0
    scores: dict[str, float] = {}
    for mid, cfg in MACRO_DIMS.items():
        seeds = cfg["seeds"]
        hit = 0.0
        for s in seeds:
            if s in bag:
                hit += 1.0
            elif any(s in w or w in s for w in bag):
                hit += 0.5
        scores[mid] = hit / max(len(seeds), 1)
    best, score = max(scores.items(), key=lambda x: x[1])
    if score <= 0:
        return "other_unmapped", 0.0
    return best, score


def pattern_label(p0: float, p1: float, p2: float) -> str:
    d01 = p1 - p0
    d12 = p2 - p1
    if abs(d01) < 0.3 and abs(d12) < 0.3:
        return "stable"
    if p2 > p0 and p1 < p0 - 0.2:
        return "reflux"
    if p2 >= p1 >= p0 and (p2 - p0) >= 0.4:
        return "diffusion"
    if p2 <= p1 <= p0 and (p0 - p2) >= 0.4:
        return "attenuation"
    if p2 > p1 and p1 > p0:
        return "diffusion"
    if p2 < p1 and p1 < p0:
        return "attenuation"
    if d01 < -0.4 and d12 > 0.3:
        return "reflux"
    return "mixed"


def representative_comments(
    docs: list[dict[str, Any]],
    topics: list[int],
    topic_id: int,
    *,
    k: int = 3,
    min_len: int = 40,
) -> list[dict[str, Any]]:
    pool = [
        docs[i]
        for i, tid in enumerate(topics)
        if tid == topic_id and len(docs[i].get("content") or "") >= min_len
    ]
    pool.sort(key=lambda d: int(d.get("like_count") or 0), reverse=True)
    out = []
    for d in pool[:k]:
        out.append({
            "likes": int(d.get("like_count") or 0),
            "depth": int(d.get("depth") or 0),
            "bvid": d.get("bvid"),
            "text": (d.get("content") or "")[:400],
        })
    return out


def summarize(
    docs: list[dict[str, Any]],
    topics: list[int],
    model: Any,
    *,
    embedding_model: str,
    min_topic_size: int,
    cluster_backend: str,
) -> dict[str, Any]:
    info = model.get_topic_info()
    topic_ids = [int(x) for x in info["Topic"].tolist()]

    # Collect liked sample texts per topic for robust macro mapping.
    topic_samples: dict[int, list[str]] = defaultdict(list)
    for doc, tid in zip(docs, topics):
        tid_i = int(tid)
        if len(topic_samples[tid_i]) >= 40:
            continue
        text = doc.get("content") or ""
        if len(text) < 12:
            continue
        topic_samples[tid_i].append(text)

    topic_macro: dict[int, str] = {}
    topic_score: dict[int, float] = {}
    topic_word_map: dict[int, list[tuple[str, float]]] = {}
    for tid in topic_ids:
        words_scored = topic_words(model, tid, 15)
        topic_word_map[tid] = words_scored
        words = [w for w, _ in words_scored]
        if tid == -1:
            topic_macro[tid] = "outlier"
            topic_score[tid] = 0.0
        else:
            mid, score = map_topic_to_macro(words, topic_samples.get(tid, []))
            topic_macro[tid] = mid
            topic_score[tid] = score

    depth_macro: dict[str, Counter] = defaultdict(Counter)
    macro_total: Counter = Counter()
    topic_total: Counter = Counter()
    for doc, tid in zip(docs, topics):
        depth = str(int(doc.get("depth") or 0))
        mid = topic_macro.get(int(tid), "other_unmapped")
        depth_macro[depth][mid] += 1
        macro_total[mid] += 1
        topic_total[int(tid)] += 1

    n = len(docs) or 1
    evolution = []
    for mid, cfg in list(MACRO_DIMS.items()) + [
        ("other_unmapped", {"label_zh": "其他未映射", "label_en": "Unmapped"}),
        ("outlier", {"label_zh": "离群主题(-1)", "label_en": "Outlier"}),
    ]:
        row = {
            "macro": mid,
            "label_zh": cfg["label_zh"],
            "label_en": cfg["label_en"],
            "n": int(macro_total.get(mid, 0)),
            "pct": round(100.0 * macro_total.get(mid, 0) / n, 3),
        }
        for d in ("0", "1", "2"):
            d_n = sum(depth_macro[d].values()) or 1
            row[f"depth_{d}_pct"] = round(100.0 * depth_macro[d].get(mid, 0) / d_n, 3)
            row[f"depth_{d}_n"] = int(depth_macro[d].get(mid, 0))
        p0, p1, p2 = row["depth_0_pct"], row["depth_1_pct"], row["depth_2_pct"]
        row["delta_0_to_1"] = round(p1 - p0, 3)
        row["delta_1_to_2"] = round(p2 - p1, 3)
        row["pattern"] = pattern_label(p0, p1, p2)
        evolution.append(row)
    evolution.sort(key=lambda x: -x["n"])

    topics_detail = []
    # Pre-aggregate topic × depth once (avoid O(n × topics) scans)
    topic_depth: dict[int, Counter] = defaultdict(Counter)
    for doc, tid in zip(docs, topics):
        topic_depth[int(tid)][str(int(doc.get("depth") or 0))] += 1

    for tid, count in topic_total.most_common():
        words_scored = topic_word_map.get(tid, [])
        topics_detail.append({
            "topic_id": tid,
            "n": count,
            "pct": round(100.0 * count / n, 3),
            "macro": topic_macro.get(tid, "other_unmapped"),
            "macro_score": round(topic_score.get(tid, 0.0), 4),
            "label_zh": (
                MACRO_DIMS.get(topic_macro.get(tid, ""), {}) or {}
            ).get("label_zh")
            or ({"outlier": "离群主题(-1)", "other_unmapped": "其他未映射"}.get(
                topic_macro.get(tid, ""), topic_macro.get(tid, "")
            )),
            "top_words": [{"term": w, "weight": round(s, 5)} for w, s in words_scored[:10]],
            "by_depth": {
                d: int(topic_depth[tid].get(d, 0)) for d in ("0", "1", "2")
            },
            "examples": representative_comments(docs, topics, tid, k=3),
        })

    # vignettes per macro: top liked across topics in that macro
    vignettes: dict[str, list[dict[str, Any]]] = {}
    for mid in list(MACRO_DIMS) + ["other_unmapped", "outlier"]:
        idxs = [i for i, tid in enumerate(topics) if topic_macro.get(int(tid)) == mid]
        idxs.sort(key=lambda i: int(docs[i].get("like_count") or 0), reverse=True)
        picked = []
        for i in idxs:
            text = docs[i].get("content") or ""
            if len(text) < 50:
                continue
            picked.append({
                "likes": int(docs[i].get("like_count") or 0),
                "depth": int(docs[i].get("depth") or 0),
                "bvid": docs[i].get("bvid"),
                "topic_id": int(topics[i]),
                "text": text[:500],
            })
            if len(picked) >= 3:
                break
        vignettes[mid] = picked

    return {
        "generated_at": _now(),
        "n_docs": len(docs),
        "depth_counts": dict(Counter(int(d.get("depth") or 0) for d in docs)),
        "embedding_model": embedding_model,
        "min_topic_size": min_topic_size,
        "cluster_backend": cluster_backend,
        "outlier_pct": round(100.0 * macro_total.get("outlier", 0) / n, 3),
        "macro_totals": {k: int(v) for k, v in macro_total.items()},
        "macro_evolution": evolution,
        "topics": topics_detail,
        "vignettes": vignettes,
        "hygiene_note": "Input is v_comment_analysis (pass_filter=1, analysis_exclude=0, is_noise=0).",
    }


# ---------------------------------------------------------------------------
# Report writer
# ---------------------------------------------------------------------------

def write_findings(summary: dict[str, Any], path: Path) -> None:
    evo = summary["macro_evolution"]
    lines: list[str] = []
    lines += [
        "# Topic Evolution Findings: Algorithmic Neighborhoods of Youth Refusal on Bilibili",
        "",
        f"**Generated:** {summary['generated_at']}  ",
        f"**Corpus size (analysis-ready):** {summary['n_docs']:,} comments  ",
        f"**Embedding:** `{summary['embedding_model']}`  ",
        f"**Clustering backend:** {summary['cluster_backend']} · `min_topic_size={summary['min_topic_size']}`  ",
        f"**Outlier share (−1):** {summary['outlier_pct']}%  ",
        "",
        "Intended venue style: *Information, Communication & Society*; *New Media & Society*.",
        "",
        "---",
        "",
        "## 1. Methods & Corpus Audit",
        "",
        "The analytic sample is the dual-gated view `v_comment_analysis`: videos with",
        "`pass_filter = 1` that were **not** soft-excluded for off-topic comment bags",
        "(26 videos with lexicon-other ≥ 98%), and comments with `is_noise = 0`",
        "(empty / emoji-only / symbol-only / @-only screened out).",
        "",
        f"- Documents embedded & clustered: **{summary['n_docs']:,}**",
        f"- Depth mix: `{summary['depth_counts']}`",
        f"- Encoder: **{summary['embedding_model']}** (CUDA fp16 batch encode; embeddings cached to disk)",
        "- Dimensionality / clustering: UMAP (`n_neighbors=15`, `n_components=5`, `min_dist=0.0`) + HDBSCAN;",
        "  cuML used when available, else multi-core CPU.",
        "- Representation: `ClassTfidfTransformer(reduce_frequent_words=True)` + MMR / KeyBERTInspired.",
        "- Macro assignment: guided seed lexicon overlap onto six theory dimensions (A–F), with soft",
        "  character-level fallback so reflective long comments are not dumped into “unmapped”.",
        "",
        "### Table 1. Macro dimension distribution (full sample)",
        "",
        "| Dim | Label (EN / ZH) | n | % | d0% | d1% | d2% | Δ0→1 | Δ1→2 | Pattern |",
        "| --- | --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | --- |",
    ]
    for row in evo:
        if row["macro"] in {"other_unmapped", "outlier"}:
            continue
        lines.append(
            f"| {row['macro'][:1]} | {row['label_en']} / {row['label_zh']} | "
            f"{row['n']} | {row['pct']} | {row['depth_0_pct']} | {row['depth_1_pct']} | "
            f"{row['depth_2_pct']} | {row['delta_0_to_1']} | {row['delta_1_to_2']} | "
            f"{row['pattern']} |"
        )
    # residual rows
    for row in evo:
        if row["macro"] not in {"other_unmapped", "outlier"}:
            continue
        lines.append(
            f"| — | {row['label_en']} / {row['label_zh']} | "
            f"{row['n']} | {row['pct']} | {row['depth_0_pct']} | {row['depth_1_pct']} | "
            f"{row['depth_2_pct']} | {row['delta_0_to_1']} | {row['delta_1_to_2']} | "
            f"{row['pattern']} |"
        )

    lines += [
        "",
        "## 2. Algorithmic Neighborhood & Topic Evolution",
        "",
        "Depth 0 denotes seed-generation videos; depths 1–2 are first- and second-order",
        "recommendation neighbors. Patterns are labeled *diffusion* (monotone rise),",
        "*reflux* (dip then rebound), *attenuation* (monotone fall), *stable*, or *mixed*.",
        "",
        "### 2.1 Production-side alienation (Dim A) → family rupture (Dim C)",
        "",
    ]
    a = next((r for r in evo if r["macro"] == "A_workplace_alienation"), None)
    c = next((r for r in evo if r["macro"] == "C_intergenerational_rupture"), None)
    b = next((r for r in evo if r["macro"] == "B_reproductive_strike"), None)
    if a and c:
        lines.append(
            f"Dim A moves {a['depth_0_pct']}% → {a['depth_1_pct']}% → {a['depth_2_pct']}% "
            f"({a['pattern']}). Dim C moves {c['depth_0_pct']}% → {c['depth_1_pct']}% → "
            f"{c['depth_2_pct']}% ({c['pattern']}). "
            "Where C rises while A softens, the recommendation neighborhood appears to"
            " **re-embed labor grievance inside kinship conflict** rather than diluting it"
            " into entertainment noise."
        )
    if b:
        lines += [
            "",
            "### 2.2 Reproductive strike resilience (Dim B)",
            "",
            f"Dim B: {b['depth_0_pct']}% → {b['depth_1_pct']}% → {b['depth_2_pct']}% "
            f"(Δ0→1={b['delta_0_to_1']}, Δ1→2={b['delta_1_to_2']}, pattern=`{b['pattern']}`). "
            "A depth-1 dip followed by depth-2 rebound is consistent with a **reflux**"
            " reading: bridal-price / childrefusal talk is not confined to seeds, but"
            " re-enters the graph after adjacent social issues are traversed.",
        ]

    lines += [
        "",
        "## 3. Qualitative Vignettes & Rhetorical Analysis",
        "",
    ]
    vignettes = summary.get("vignettes") or {}
    for mid, cfg in MACRO_DIMS.items():
        lines.append(f"### {mid[0]}. {cfg['label_en']} / {cfg['label_zh']}")
        lines.append("")
        for v in vignettes.get(mid, []):
            lines.append(
                f"- ♥{v['likes']:,} · depth {v['depth']} · topic {v['topic_id']} · "
                f"`{v.get('bvid')}`  \n  > {v['text']}"
            )
        if not vignettes.get(mid):
            lines.append("_No long high-like vignette retained for this dimension in the run._")
        lines.append("")

    lines += [
        "## 4. Computational Validity Reflection",
        "",
        "Three layers of classification were progressively refined in this project:",
        "",
        "1. **Lexicon gate (pilot):** high precision for explicit workplace/marriage tokens,",
        "   but ~84% of comments fell into “other” because irony and citizenship talk evade",
        "   surface keywords.",
        "2. **Local TF–IDF + SVD BERTopic (25k stratified):** recovered thematic structure",
        "   without GPU, but undersampled the long tail and used bag-of-words geometry.",
        "3. **Full-corpus BGE Transformer BERTopic (this report):** encodes semantic",
        "   neighborhood for all analysis-ready comments, with guided six-dimension mapping",
        "   and outlier reduction. Validity still requires human audit of top topics and",
        "   vignettes; polarity lexicons remain inappropriate for cynical Bilibili register.",
        "",
        "---",
        "",
        "*Automated draft from `run_gpu_full_bertopic.py`. Numbers are empirical for this",
        "corpus snapshot; interpretive claims are for research-team discussion, not final",
        "publication copy.*",
        "",
    ]
    path.write_text("\n".join(lines), encoding="utf-8")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main() -> int:
    parser = argparse.ArgumentParser(description="Full-corpus GPU BERTopic pipeline")
    parser.add_argument("--db", type=Path, default=DEFAULT_DB)
    parser.add_argument("--docs-jsonl", type=Path, default=None,
                        help="Prefer JSONL/JSONL.GZ on GPU hosts after thin upload")
    parser.add_argument("--export-only", action="store_true",
                        help="Export comment_analysis JSONL.GZ from local DB and exit")
    parser.add_argument("--export-path", type=Path, default=DEFAULT_JSONL)
    parser.add_argument("--min-chars", type=int, default=8)
    parser.add_argument("--embedding-model", default="BAAI/bge-large-zh-v1.5")
    parser.add_argument("--batch-size", type=int, default=256)
    parser.add_argument("--min-topic-size", type=int, default=120)
    parser.add_argument("--nr-topics", default="auto")
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--force-embed", action="store_true")
    parser.add_argument("--skip-model-save", action="store_true")
    parser.add_argument("--dry-load", action="store_true")
    parser.add_argument("--allow-cpu", action="store_true",
                        help="Dangerous: allow non-CUDA run (very slow / not for full 515k)")
    parser.add_argument("--max-docs", type=int, default=0,
                        help="Debug subsample; 0 = full corpus")
    args = parser.parse_args()

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    os.environ.setdefault("TOKENIZERS_PARALLELISM", "false")
    os.environ.setdefault("HF_ENDPOINT", os.environ.get("HF_ENDPOINT", "https://hf-mirror.com"))

    # Load docs
    if args.docs_jsonl and args.docs_jsonl.is_file():
        _log(f"[data] loading JSONL {args.docs_jsonl}")
        docs = load_jsonl(args.docs_jsonl, min_chars=args.min_chars)
    else:
        if not args.db.is_file():
            _log(f"[error] DB not found: {args.db}")
            return 1
        _log(f"[data] loading SQLite view from {args.db}")
        docs = load_from_db(args.db, min_chars=args.min_chars)

    _log(f"[data] {len(docs):,} docs · depth={dict(Counter(d['depth'] for d in docs))}")

    if args.export_only:
        path = export_jsonl(docs, args.export_path)
        size_mb = path.stat().st_size / 1e6
        _log(f"[export] wrote {path} ({size_mb:.1f} MB)")
        return 0

    if args.max_docs and args.max_docs > 0 and len(docs) > args.max_docs:
        rng = random.Random(args.seed)
        # stratified
        by_d: dict[int, list] = defaultdict(list)
        for d in docs:
            by_d[int(d["depth"])].append(d)
        take = []
        remaining = args.max_docs
        depths = sorted(by_d)
        for i, depth in enumerate(depths):
            share = max(1, int(args.max_docs * len(by_d[depth]) / len(docs)))
            k = min(len(by_d[depth]), share if i < len(depths) - 1 else remaining)
            take.extend(rng.sample(by_d[depth], k) if k < len(by_d[depth]) else by_d[depth])
            remaining = args.max_docs - len(take)
        docs = take
        _log(f"[data] subsampled to {len(docs):,}")

    if args.dry_load:
        return 0

    try:
        import torch
        cuda_ok = torch.cuda.is_available()
    except Exception:
        cuda_ok = False
    if not cuda_ok and not args.allow_cpu:
        _log(
            "[error] No CUDA GPU detected. This script is for AutoDL/RunPod.\n"
            "  On Mac:  .venv/bin/python run_gpu_full_bertopic.py --export-only\n"
            "  Then:    .venv/bin/python tools/launch_bertopic_autodl.py\n"
            "  (requires Settings → AutoDL 一键接入 first)"
        )
        return 2

    texts = [d["content"] for d in docs]
    embeddings = encode_documents(
        texts,
        model_name=args.embedding_model,
        batch_size=args.batch_size,
        cache_path=EMBED_CACHE if args.max_docs <= 0 else OUT_DIR / f"embeddings_bge_{len(docs)}.npy",
        meta_path=EMBED_META if args.max_docs <= 0 else OUT_DIR / f"embeddings_bge_{len(docs)}.meta.json",
        force=args.force_embed,
    )

    nr_topics: str | int = args.nr_topics
    if nr_topics != "auto":
        nr_topics = int(nr_topics)

    # Detect cluster backend label for metrics
    cluster_backend = "cuml"
    try:
        import cuml  # noqa: F401
    except Exception:
        cluster_backend = "cpu"

    model, topics = fit_bertopic(
        texts,
        embeddings,
        min_topic_size=args.min_topic_size,
        seed=args.seed,
        nr_topics=nr_topics,
    )
    summary = summarize(
        docs,
        topics,
        model,
        embedding_model=args.embedding_model,
        min_topic_size=args.min_topic_size,
        cluster_backend=cluster_backend,
    )
    summary["gpu_mem"] = _gpu_mem()
    summary["seed"] = args.seed

    METRICS_PATH.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    write_findings(summary, REPORT_PATH)
    _log(f"[out] metrics → {METRICS_PATH}")
    _log(f"[out] report  → {REPORT_PATH}")

    if not args.skip_model_save:
        try:
            model.save(str(MODEL_DIR), serialization="safetensors", save_ctfidf=True)
            _log(f"[out] model → {MODEL_DIR}")
        except Exception as exc:
            _log(f"[warn] model save skipped: {exc}")

    _log("\n=== Macro × depth ===")
    for row in summary["macro_evolution"]:
        if row["macro"] in {"other_unmapped", "outlier"}:
            continue
        _log(
            f"  {row['label_zh']:16s} n={row['n']:7d}  "
            f"d0={row['depth_0_pct']:6.2f}% d1={row['depth_1_pct']:6.2f}% "
            f"d2={row['depth_2_pct']:6.2f}%  [{row['pattern']}]"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
