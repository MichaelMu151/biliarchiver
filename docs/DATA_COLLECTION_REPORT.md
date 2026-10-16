# Data Collection Appendix · Bilibili Labor-Discourse Corpus (v1.0, 2026-10-16)

> All figures below are computed from `data/corpus.db` (SQLite, `SCHEMA_VERSION=4`).
> Lexicon implementation: `bili/labor_lexicon.py::match_labor`. Gate: `bili/academic.py::evaluate_gate`.
> Reproduce: the Python one-liners in §7 against the frozen DB dump.

## 1. Corpus at a glance

| Layer | N |
|---|---:|
| Authors (UP) archived | 133 |
| Videos | 59,945 |
| Comments | 970,093 |
| Danmaku | 1,584,525 |
| Dynamics (posts) | 73,105 |
| Transcripts (video-level) | 1,294 |
| Snowball edges (recommendation links) | 4,560 |
| Gate decisions (logged accept/reject) | 13,303 |
| Collection runs (`collection_runs`) | 32 |

Time window: 2026-09-22 → 2026-10-16. Analysis was frozen on 2026‑10‑16;
`--apply` runs after that date are post-freeze backfills and are excluded.

## 2. The construct: what counts as "labor discourse"

A single dictionary (`match_labor`) is applied to **title + tags + description**
for every video. Four mutually exclusive outcomes:

| Tier | Content (examples) | Rule |
|---|---|---|
| `strong` (n=5,350 videos) | 996 / 007 / 大小周 / 欠薪 / 裁员 / N+1 / 劳动仲裁 / 过劳 / 打工人 / 社畜 / 牛马 / 资本家 / 剥削 / 流水线 / 富士康 / 骑手 / 外卖 / 三和大神 / 挂壁 / 日结 / 内卷 / 躺平 | single hit ⇒ include |
| `phrase` (n=2,896) | 拖欠工资 / 强制加班 / 没有双休 / 提桶跑路 / 日结工 / 找工作… (~50 regexes) | single hit ⇒ include (high precision) |
| `moderate` (n=1,975) | 就业 / 失业 / 薪资 / 35岁 / 大厂 / 体制内 / 考公 / 社保 / 调休 / 工会… | hit alone insufficient (`WEAK_ALONE`): needs a second labor signal |
| `excluded_family` (n=2,936) | 育儿 / 生育 / 婚姻 / 催婚 / 备孕… | **hard veto**, evaluated before inclusion |
| `none` (n=46,788) | — | excluded |

**Labor-hit corpus = strong + phrase (+ moderate with corroboration) = 10,221 videos.**
Family veto removes a further 2,936 even when labor terms co-occur
(e.g. "双休带娃" is excluded: construct is *workplace* discourse, not care work).

## 3. Five collection channels (exhaustive, non-overlapping by design)

| # | Channel (DB `kind`) | Runs | What it does | Gate? | Yields |
|---|---|---|---|---|---|
| C1 | UP archive (`archive`) | 4 (2 done) | full crawl of 130 curated labor-adjacent accounts: videos + comments + danmaku + dynamics | **none** (post-hoc labeling only) | bulk of the 59,945; seed pool |
| C2 | Academic snowball (`academic`) | 7 (4 done) | BFS over Bilibili recommendation graph from seeds; `evaluate_gate` per candidate | **yes** — ① allow/deny partitions ② `tag_terms` (19 words) ③ labor lexicon ④ family veto ⑤ `min_views` ⑥ `min_engagement` | 206 pass in latest run `e9f81b4b80fd` (see §4); 333 of the labor-hit corpus carry `discovery='snowball'` |
| C3 | Keyword sampling (`keyword`) | 9 (8 done) | site-wide search on 19 title terms; title must match ≥1 | title-match only, lexicon applied post-hoc | 280 labor-hit corpus videos |
| C4 | Scout preview (`scout`) | 1 (done `f414741f3ef8`) | **lexicon-only** sweep of the 130 accounts' new uploads (no `tag_terms`) | lexicon only | methodological gold standard (see §6) |
| C5 | Forced BV transcribe (`transcribe`) | 11 (9 done) | explicit BV lists, `skip_gate=True`, Whisper on GPU | **none** | all 1,294 transcripts; `forced_transcribe` rows in `gate_decisions` |

Collection ethics/footprint: public API only, logged-in session, request
interval 1.8–3.2 s with jitter, ≤6 retries, per-stage checkpointing
(`.pipeline.json`), no private messages, no de-anonymization (sender ids stored
as hashes in `danmaku.sender_hash`).

## 4. Worked example: latest snowball gate (`e9f81b4b80fd`, 2026-10-14)

| Decision | n | Meaning |
|---|---|---|
| `pass` | 206 | admitted to archive |
| `tags` | 209 | title missed the 19-word `tag_terms` list |
| `labor_lexicon` | 77 | passed tags but failed the lexicon |
| `excluded_family` | 31 | family veto |
| `engagement` | 59 | below interaction threshold |
| `min_views` | 36 | below view threshold |
| **total adjudicated** | **618** | |

Post-hoc audit (lexicon re-run over the 209 `tags` rejects) rescued **56 videos**
that are lexicon-positive (e.g. 896/996, 三和大神, 社保新规) — a documented
recall loss from placing a narrow 19-word list *before* the lexicon
(see `data/exports/recrawl_shortlist.csv`). The pipeline was subsequently
reordered (lexicon first).

## 5. From 10,221 to the analytic sample: the N=799 rule

The analytic sample is defined **conjunctively**:

> **S = {v : labor-hit(v) ∧ transcribed(v)}**, transcribed = a video-level
> `transcripts` row with usable text. No further cherry-picking.

| Step | n |
|---|---:|
| labor-hit videos (strong/phrase/moderate+) | 10,221 |
| ∩ has transcript | **799** |
| — of which large-v3 GPU Whisper (zh, p≥0.97 typical) | 747 |
| — of which small/cpu or OCR-frame fallback | 52 |
| 799 with comments | 799 (342,089 comments) |
| 799 with danmaku (after `tools/backfill_danmaku.py --apply`) | 751 videos, 1,180,789 danmaku rows |

Composition by discovery channel (must be reported separately, **not pooled
as one random sample**): seed 287 · snowball 231 · keyword 191 · space 90.
“726 + 40 手筛 + 34 scout” = the same 799 (the 40 and 34 were drawn from
`recrawl_shortlist.csv` and `scout_labor_34.csv`, both lexicon-positive).

## 6. Strengths and limitations (for reviewers)

**Strengths.**
1. *Construct validity*: one versioned dictionary, applied identically to all
   59,945 videos; family veto pre-registered (`require_labor_lexicon`).
2. *Triangulation*: text (transcript) + comments (970k) + danmaku (1.58M) +
   recommendation edges (4,560) for the same videos.
3. *Auditability*: every accept/reject logged (`gate_decisions`, 13,303 rows);
   the 56 rescued cases are published, not hidden.
4. *Gold-standard check*: the lexicon-only scout sweep (C4) lets us estimate
   the recall loss of the tag-gated snowball (C2).

**Limitations (and what we did about them).**
1. *Not a probability sample.* C2 oversamples the recommendation graph of seed
   UP主; C3 oversamples searchable titles; C1 oversamples 130 curated accounts.
   → Report channel-specific results; any pooled estimate needs channel weights
   or explicit scope conditions.
2. *Transcription bottleneck.* Only 799/10,221 labor-hit videos are transcribed
   (GPU cost); transcription priority followed the labor queue, so S is
   labor-representative but not a random subset — state this.
3. *Platform censoring of danmaku.* Bilibili expires old danmaku pools; 48 of
   223 backfill targets returned `empty` (true zeros, kept as `done(0)`).
   Danmaku analyses should condition on pool availability.
4. *Whisper language-id errors.* 3 videos misclassified (en/nn); re-transcribed
   with forced `zh`. One silent video fell back to OCR frames (6 slides) —
   excluded from text analyses, kept for metadata.
5. *Temporal scope.* One month (2026-09-22 → 10-16); claims about change over
   time require a second wave.

## 7. Reproduction (one-liners, DB frozen at v1.0)

```bash
.venv/bin/python -c "
import sqlite3, json
c = sqlite3.connect('data/corpus.db')
from bili.labor_lexicon import match_labor  # tiers: strong/phrase/moderate/excluded_family/none
"
# gate audit: SELECT reason, COUNT(*) FROM gate_decisions WHERE run_id='e9f81b4b80fd' GROUP BY reason
# sample:     SELECT COUNT(DISTINCT bvid) FROM transcripts WHERE bvid IN (SELECT bvid FROM videos)
# danmaku:    tools/backfill_danmaku.py --list data/exports/recrawl_shortlist.csv  (dry-run default)
```
