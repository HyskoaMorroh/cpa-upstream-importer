"""CPA 上游批量导入 —— 共享判定库。

从三个脚本（probe-fix.py / audit-upstreams.py / context-probe.py /
swap-watch.py）里抽出的纯函数与判定规则，供导入服务复用，不重写。

模块划分：
    parse        解析 url,key，按段规范化 base-url
    classify     响应定性：余额/封号/限流/门禁/IP封/反测活/死路/临时/注入
    request      按段构造请求，路径与 CPA executor 对齐
    client       统一 HTTP 传输（原三脚本用了三种底层）
    fingerprint  后端 id 指纹、静默换模判定、截断校验
    pipeline     四阶段探测编排
    plan         去重、priority 定档、影响面枚举
    writeback    行级 YAML 编辑（保注释）、备份、diff、校验
"""

from .parse import (
    SECTIONS,
    ParsedRow,
    ParseResult,
    base_for_section,
    host_of,
    mask_key,
    parse_lines,
    strip_v1,
)
from .classify import classify, advice, is_usable, should_downrank, body_excerpt
from .fingerprint import (
    backend_of,
    input_tokens,
    model_matches,
    resp_id,
    resp_model,
    swap_rate,
    truncated,
)
from .pipeline import Prober, CandidateResult, SectionVerdict, Attempt
from .plan import (
    Band,
    Impact,
    ImportPlan,
    SectionPlan,
    assign_priorities,
    build_band,
    build_plan,
    compute_impact,
    dedup_key,
    credential_pair,
    dominant_prefix,
    existing_fingerprints,
    existing_pairs,
    extract_existing_entries,
    entry_all_zero_weight,
    entry_weights,
    weight_zero_excludes,
    host_matches_note,
    name_alias_map,
    priority_collisions,
    score_verdict,
    suggest_priority,
)
from .writeback import (
    Diff,
    owned_sections,
    apply_diffs,
    backup,
    build_diffs,
    push_to_cpa,
    rebuild_config_full,
    validate,
    verify_upstream,
    write_local,
)
from .batch import BatchProber
from .resources import Resources, detect as detect_resources
from .cpa_source_probe import check as check_profile_drift

__all__ = [
    "SECTIONS",
    "ParsedRow",
    "ParseResult",
    "parse_lines",
    "base_for_section",
    "strip_v1",
    "host_of",
    "mask_key",
    "classify",
    "advice",
    "is_usable",
    "should_downrank",
    "body_excerpt",
    "resp_model",
    "resp_id",
    "backend_of",
    "input_tokens",
    "model_matches",
    "truncated",
    "swap_rate",
    "Prober",
    "CandidateResult",
    "SectionVerdict",
    "Attempt",
    "Band",
    "Impact",
    "build_band",
    "compute_impact",
    "score_verdict",
    "dedup_key",
    "credential_pair",
    "dominant_prefix",
    "existing_fingerprints",
    "existing_pairs",
    "extract_existing_entries",
    "entry_all_zero_weight",
    "entry_weights",
    "weight_zero_excludes",
    "host_matches_note",
    "name_alias_map",
    "suggest_priority",
    "SectionPlan",
    "ImportPlan",
    "build_plan",
    "assign_priorities",
    "priority_collisions",
    "owned_sections",
    "Diff",
    "backup",
    "build_diffs",
    "apply_diffs",
    "push_to_cpa",
    "rebuild_config_full",
    "validate",
    "verify_upstream",
    "write_local",
    "BatchProber",
    "Resources",
    "detect_resources",
    "check_profile_drift",
]
