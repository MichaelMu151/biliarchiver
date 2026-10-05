"""Labor / workplace lexicon for UP screening and macro dimension A.

Single source of truth — keep in sync with css_pipeline/preprocess.py _RE_A where
terms overlap, and with bili/analysis_hygiene.WORK_TERMS for pilot audits.

Matching is intentionally strict: broad single tokens only count when paired with
another labor signal (see WEAK_ALONE).
"""

from __future__ import annotations

import re
from dataclasses import dataclass

# --- Tier 1: direct workplace / labor exploitation signals ---
STRONG_TERMS: tuple[str, ...] = (
    "加班", "996", "007", "大小周", "单休", "双休", "8小时", "八小时", "5天8小时",
    "欠薪", "讨薪", "裁员", "被裁", "下岗", "N+1", "辞退", "开除", "离职", "裸辞", "辞职",
    "劳动法", "劳动仲裁", "劳动监察", "劳动成果", "仲裁", "工伤", "过劳", "猝死",
    "劳动对话", "劳动文化",
    "打工人", "上班族", "社畜", "牛马", "班味", "耗材", "螺丝钉", "工具人",
    "人肉电池", "电池人", "职场", "工位", "早会", "开早会", "考勤", "打卡",
    "老板", "领导", "资本家", "剥削", "压榨", "福报", "狼性", "奋斗者",
    "劳资", "资方", "外包", "派遣", "劳务派遣", "劳务", "合同工", "派遣工",
    "小时工", "两班倒", "三班倒", "夜班", "晚班", "流水线", "工厂", "富士康",
    "骑手", "外卖", "快递", "快递员", "网约车", "滴滴", "美团", "饿了么",
    "农民工", "蓝领", "白领", "一线", "基层",
    "送餐员", "仓管", "保洁", "保安", "服务员", "流水线工人",
    "降本增效", "优化", "毕业", "提桶跑路", "251", "关系户", "黑心企业",
    "自愿加班", "主动加班", "无偿加班", "加班费",
    "PUA", "团建", "内卷", "躺平", "摆烂", "孔乙己",
    "灵活就业", "三和大神", "挂壁", "日结", "零工",
)

# --- Tier 2: employment market / job quality ---
MODERATE_TERMS: tuple[str, ...] = (
    "就业", "失业", "就业率", "找工作", "求职", "招聘", "用人", "校招", "秋招",
    "春招", "内推", "实习", "转正", "试用期", "面试", "简历", "HR", "人事",
    "薪资", "工资", "薪酬", "底薪", "时薪", "月薪", "年薪", "年终奖", "13薪",
    "绩效", "KPI", "kpi", "晋升", "升职", "35岁", "中年危机",
    "大厂", "中小厂", "体制内", "铁饭碗", "考公", "编制", "事业编",
    "公积金", "五险一金", "社保", "医保",
    "工作", "上班", "下班", "通勤", "打工", "劳动者", "劳工", "劳动力",
    "人口红利", "工程师红利", "用工", "用人成本", "离职率",
    "调休", "补休", "年假", "病假", "事假", "竞业", "保密协议",
    "全职", "兼职", "待业", "啃老", "家里蹲", "gap", "Gap",
    "毕业即失业", "慢就业", "结构性失业", "摩擦性失业",
    "工时", "劳动时间", "劳动强度", "劳动条件", "劳动权益",
    "工会", "罢工", "抗议", "维权", "员工", "工人", "技术工人",
)

# --- Tier 3: multi-character phrases (high precision) ---
PHRASE_PATTERNS: tuple[str, ...] = (
    r"职场.{0,6}关系",
    r"关系.{0,4}户",
    r"黑心.{0,4}企业",
    r"黑心.{0,4}工厂",
    r"不给.{0,6}工资",
    r"拖欠.{0,6}工资",
    r"拖欠.{0,6}薪",
    r"恶意.{0,4}裁员",
    r"违法.{0,4}加班",
    r"强制.{0,4}加班",
    r"没有.{0,6}双休",
    r"取消.{0,4}双休",
    r"单休.{0,6}违法",
    r"大小周",
    r"996\.?ICU",
    r"毕业.{0,8}优化",
    r"毕业.{0,8}裁员",
    r"被.{0,2}优化",
    r"被.{0,2}毕业",
    r"提桶.{0,2}跑路",
    r"劳动.{0,4}合同",
    r"劳务.{0,4}合同",
    r"派遣.{0,4}工",
    r"外卖.{0,4}骑手",
    r"快递.{0,4}员",
    r"工厂.{0,6}打工",
    r"流水线.{0,6}工人",
    r"两班.{0,2}倒",
    r"三班.{0,2}倒",
    r"日结.{0,4}工",
    r"小时.{0,2}工",
    r"灵活.{0,4}就业",
    r"找.{0,2}工作",
    r"没.{0,4}工作",
    r"失去.{0,4}工作",
    r"丢.{0,2}工作",
    r"就业.{0,4}难",
    r"失业.{0,4}率",
    r"失业潮",
    r"下岗潮",
    r"工资.{0,6}低",
    r"薪资.{0,6}低",
    r"工资.{0,10}崩",
    r"薪资.{0,10}崩",
    r"工资.{0,10}塌",
    r"薪资.{0,10}塌",
    r"劳动成果",
    r"剩余.?劳动",
    r"剩余.?价值",
    r"按劳分配",
    r"按需分配",
    r"老板.{0,8}PUA",
    r"职场.{0,6}PUA",
    r"上班.{0,6}丑",
    r"上班.{0,6}累",
    r"不想.{0,4}上班",
    r"讨厌.{0,4}上班",
    r"厌恶.{0,4}上班",
    r"日本.{0,6}职场",
    r"中国.{0,6}职场",
    r"还原.{0,8}离职",
    r"不同.{0,4}员工.{0,4}离职",
    r"深度还原.{0,12}职场",
    r"深度还原.{0,12}打工",
    r"还原.{0,12}打工人",
)

# Alone these are too noisy — only count when another labor signal co-occurs.
WEAK_ALONE: frozenset[str] = frozenset({
    # generic / lifestyle
    "工作", "产业", "商业", "优化", "毕业", "红利", "内卷", "躺平", "摆烂",
    "Gap", "gap", "PUA", "兼职", "全职", "孔乙己", "251", "团建",
    # role / hierarchy without workplace framing
    "老板", "领导", "打卡", "考勤", "工位", "早会", "开早会",
    "开除", "离职", "辞职", "辞退", "裸辞",
    "一线", "基层", "保安", "服务员", "保洁", "仓管",
    "班味", "耗材", "螺丝钉", "工具人", "电池人", "人肉电池",
    # platforms / gig (often appear in ads / puns)
    "滴滴", "美团", "饿了么", "外卖", "快递", "网约车", "骑手",
    # everyday employment vocabulary
    "上班", "下班", "通勤", "打工", "工资", "薪资", "薪酬", "月薪", "年薪",
    "就业", "失业", "面试", "招聘", "用人", "实习", "简历", "HR", "人事",
    "医保", "社保", "公积金", "编制", "事业编", "考公", "铁饭碗", "体制内",
    "员工", "大厂", "夜班", "晚班", "绩效",
    "KPI", "kpi", "晋升", "升职", "抗议", "维权", "仲裁",
    "35岁", "中年危机",
})

# Strip these before matching short platform tokens (substring false positives).
_FP_STRIP: tuple[tuple[str, str], ...] = (
    ("娇滴滴", ""),  # else "滴滴" matches
)


def _compile() -> tuple[re.Pattern[str], re.Pattern[str], list[re.Pattern[str]]]:
    strong = re.compile("|".join(re.escape(t) for t in STRONG_TERMS), re.I)
    moderate = re.compile("|".join(re.escape(t) for t in MODERATE_TERMS), re.I)
    phrases = [re.compile(p, re.I) for p in PHRASE_PATTERNS]
    return strong, moderate, phrases


_STRONG_RE, _MODERATE_RE, _PHRASE_RES = _compile()


@dataclass(frozen=True)
class LaborMatch:
    hit: bool
    tier: str  # strong | moderate | phrase | none
    terms: tuple[str, ...]


def _normalize_for_terms(text: str) -> str:
    out = text or ""
    for src, dst in _FP_STRIP:
        out = out.replace(src, dst)
    return out


def _find_terms(text: str, pool: tuple[str, ...]) -> list[str]:
    if not text:
        return []
    low = text.lower()
    # Longer terms first so "打工人" wins over bare stems when both listed.
    ordered = sorted(pool, key=len, reverse=True)
    found: list[str] = []
    for t in ordered:
        needle = t.lower()
        if needle in low:
            found.append(t)
    return found


def match_labor(text: str) -> LaborMatch:
    """Return whether text is labor-relevant and which terms fired.

    Rules (tight):
    - Phrase patterns → keep (high precision).
    - Any non-WEAK_ALONE term → keep (tier strong if from STRONG else moderate).
    - Only WEAK_ALONE terms → keep only if ≥2 distinct weak signals.
    - Otherwise → drop.
    """
    raw = text or ""
    if not raw.strip():
        return LaborMatch(False, "none", ())

    phrase_hits: list[str] = []
    for pat in _PHRASE_RES:
        m = pat.search(raw)
        if m:
            phrase_hits.append(m.group(0))
    if phrase_hits:
        return LaborMatch(True, "phrase", tuple(phrase_hits[:8]))

    norm = _normalize_for_terms(raw)
    strong_hits = _find_terms(norm, STRONG_TERMS)
    moderate_hits = _find_terms(norm, MODERATE_TERMS)
    combined = list(dict.fromkeys(strong_hits + moderate_hits))
    if not combined:
        return LaborMatch(False, "none", ())

    non_weak = [t for t in combined if t not in WEAK_ALONE]
    if non_weak:
        if any(t in strong_hits for t in non_weak):
            return LaborMatch(True, "strong", tuple(non_weak[:12]))
        return LaborMatch(True, "moderate", tuple(non_weak[:12]))

    # Weak-only: require at least two distinct signals.
    if len(combined) >= 2:
        return LaborMatch(True, "moderate", tuple(combined[:12]))
    return LaborMatch(False, "none", ())


def labor_regex() -> re.Pattern[str]:
    """Combined regex for quick scans (strong + moderate non-weak + phrase stems)."""
    parts = list(STRONG_TERMS) + [t for t in MODERATE_TERMS if t not in WEAK_ALONE]
    return re.compile("|".join(re.escape(t) for t in parts), re.I)


ALL_TERMS_FLAT: tuple[str, ...] = tuple(dict.fromkeys(STRONG_TERMS + MODERATE_TERMS))
