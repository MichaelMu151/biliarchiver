# 799 analytic sample · 来源说明书（逐条可复现，2026-10-16 冻结）

> 定义：S = {v : labor-hit(v) ∧ transcribed(v)}，799 条。
> `labor-hit` = `match_labor(标题+标签+简介)` 命中 strong/phrase（moderate 需第二信号），
> 且不在 `excluded_family`（育儿/生育/婚姻一票否决）。
> 以下数字全部来自 `corpus.db`：`videos.run_id/discovery/parent_bvid/seed_bvid` +
> `collection_runs.config_json`。复现脚本见 §6。

## 总表：799 按任务（run_id）归属

| n | run_id | kind / label | 启动 | 说明 |
|---:|---|---|---|---|
| 206 | `e9f81b4b80fd` | academic / 学术滚雪球 | 10-14 | 本轮滚雪球 pass 的 206（门槛见 §3） |
| 187 | `1b54ee358b71` | transcribe / 按视频号采集 | 10-06 | 指定 391 条 BV 清单强制转写（`skip_gate=True`），命中 187 |
| 123 | `07f0fa77c98f` | keyword / 关键词采样 | 10-05 | 关键词搜（词见 §4），标题约束 8小时/八小时/双休/单休 |
| 87 | `f414741f3ef8` | scout / 选题预览·劳工词表筛选 | 09-30 | 130 账号纯词表扫（金标准），`discovery='space'` |
| 74 | `277ca9b4aa63` | transcribe / 按视频号采集 | 10-15 | 回炉 74（56 rescued + 34 scout + 去重），全过 |
| 68 | `e0d37090530d` | keyword / 关键词采样 | 10-05 | 同上，时间窗自 08-01，`min_views=300` |
| 43 | `53c6a9380810` | academic / 滚雪球采样 | 09-25 | 上一轮滚雪球（门槛见 §3） |
| 3 | `825e720bcf7a` | archive / UP 主归档·2账号 | 10-13 | `381297519`（懒澡的杰基）+ `448327317` 全量归档的副产品 |
| 3+3+1+1 | 零散 transcribe | 按 BV 转写 | 09-24~10-07 | 早期单条强制转写，共 8 |

按 `discovery` 字段：seed 287 · snowball 231 · keyword 191 · space 90。

## §1 UP 主来源（C1 归档 + 各任务沉淀）

799 共涉及约 120 个 UP，头部高度集中（长尾不一一列，库里可查）：

| n | mid | UP | 主要贡献任务 |
|---:|---|---|---|
| 55 | 3493117728656046 | 摩的司机徐师傅 | 归档沉淀 + 滚雪球种子（三和/日结/挂壁系列） |
| 11 | 570064 | 章北海official | 滚雪球 + 关键词（896/996/劳动法） |
| 11 | 229762896 | 汤师傅ヽ | 滚雪球 + 关键词（社保/打工人） |
| 10 | 35847683 | 峰哥亡命天涯 | 归档沉淀（三和大神） |
| 9 | 1690570029 | 超来-双休八小时 | 关键词（双休/八小时标题约束） |
| 7 | 3493261484231462 | 上天入地扬扬哥 | 滚雪球 |
| 7 | 453013730 | 劳动法猫头鹰 | scout + 滚雪球 |
| 6+5 | 35466224473746 / 3546827792058566 | 浪-整活的神（直播号+主号） | 关键词（8小时/躺平） |

归档任务本身（`825e720bcf7a`，2 账号全量：`懒澡的杰基`、`448327317`）
只直接贡献 3 条——归档的价值是提供种子池和评论/弹幕底座，不是转写数。

## §2 关键词来源（C3：123 + 68 = 191）

两轮共用标题约束 `title_must_terms = 8小时,八小时,双休,单休`
（标题至少命中其一，由 `keyword_sample.py` 在搜索后二次过滤）：

| run | 搜索词 `keywords` | 时间窗 | 热度门 | n |
|---|---|---|---|---|
| `07f0fa77c98f` (10-05, cancelled 但数据保留) | 八小时, 8小时, 双休, 调休, 两班倒, 单休 | ≥2026-09-01 | 无（None） | 123 |
| `e0d37090530d` (10-05, done) | 八小时, 8小时, 双休, 调休, 两班倒 | ≥2026-08-01 | `min_views=300`，分区黑名单 18 类 | 68 |

191 条的标题命中分布（可重叠）：双休 113 · 八小时 62 · 8小时 51 · 单休 7。
注意 `07f0fa77c98f` 状态是 cancelled——任务被中断但已入库的 123 条保留，
`videos.run_id` 仍指向它，复现时不要按 status 过滤。

## §3 滚雪球来源（C2：206 + 43 = 249；其中 discovery='snowball' 231）

两轮都是“种子 BV → 推荐流 BFS”，`skip_gate=False`，`evaluate_gate` 六道门：

**本轮 `e9f81b4b80fd`（10-14，贡献 206）：**
种子 = 约 60 条 labor BV（含 `BV1gEGQzLErX` 日结、`BV1ei421e7jm` 网吧大神、
`BV1Fy4y1N7Hi` 调休、`BV1Z4421S7BZ` 896 等）；
`max_depth=2, max_nodes=700`；
`min_views=50000, min_replies=300, min_engagement=0.003`；
`category_deny` = 游戏/动画/…/搞笑 14 类；
`tag_terms` = 就业/学历/失业/薪资/找工作/文凭/体制内/内卷/大厂/职场/牛马/毕业/躺平/…（27 词）；
`time_range=all`。裁决 618：pass 206，其余见报告 §4。

**上轮 `53c6a9380810`（09-25，贡献 43）：**
种子约 60 条早期 BV；`max_depth=2, max_nodes=400`；
`min_views=50000, min_replies=300, min_engagement=0.003`；
`category_deny` 14 类；`tag_terms` 同系 27 词。

**231 条 snowball 的滚法（`parent_bvid/seed_bvid` 实证）：**
Top seeds：`BV1yT42167dQ` 学历贬值（45 条后代）· `BV1bz421k7yG` 断缴社保（35）·
`BV1YoLLzWEmV` 职场动作（33）· `BV19F411y7QU` 大专就业（22）·
`BV1gEGQzLErX` 日结（18）。
Top parents（直接推荐边）：`BV1pQDwYCEp7` 五险一金（12）· `BV1cx3NzcEGb` 毕业生就业（10）。
即：滚雪球的实际扩散围绕“学历贬值/社保断缴/就业”三个母题，`snowball_edges` 4,560 条为证。

## §4 强制转写清单（C5：187 + 74 + 8 = 269；其中 discovery='seed' 287）

- `1b54ee358b71`（10-06）：391 条指定 BV（`seed_bvids` 全列在 `config_json`），
  `max_depth=0, max_nodes=391`，三门全零（`min_views=0, min_replies=0, min_engagement=0`），
  `time_range=all`。命中 S 的 187 条（其余 204 或非劳工、或当时未转写）。
- `277ca9b4aa63`（10-15）：回炉 74（40 手筛 rescued + 34 scout），74/74 全过，
  `crawl_danmaku=True, comment_max_pages=0`（全量评论）。
- 零散 8 条（09-24~10-07）：早期单条 `按BV转写`。

## §5 scout 金标准（C4：87 条，`discovery='space'`）

`f414741f3ef8`（09-30）：130 账号新投稿**只过词表**（`tag_terms` 留空），
87 条全部 `space`。UP 分散（日本产牛马喵喵 3、章北海 3…），无头部垄断，
可作为 C2/C3 召回率的分母。

## §6 复现

```bash
# 总表：SELECT run_id, COUNT(*) FROM videos WHERE bvid IN (SELECT DISTINCT bvid FROM transcripts) GROUP BY run_id
# S 口径：match_labor(tags_json+title+description) 命中 strong/phrase 且非 excluded_family
# 任务配置：SELECT config_json FROM collection_runs WHERE run_id='<id>'
# 滚雪球边：SELECT parent_bvid, COUNT(*) FROM videos WHERE discovery='snowball' GROUP BY parent_bvid
```
