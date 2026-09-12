"""
CPA 运行时健康状态查询模块

支持两种数据源：
1. HTTP API - 从 CPA 管理接口实时获取（优先）
2. Detection Fallback - 基于检测结果预测（回退）
"""

import logging
import requests
from typing import Dict, Optional, List, Tuple
from dataclasses import dataclass

logger = logging.getLogger(__name__)


@dataclass
class AuthHealth:
    """单个 auth 的健康状态"""
    success: int
    failed: int
    recent_buckets: List[Dict[str, int]]
    health_score: float = 0.0


def fetch_cpa_runtime_health(
    base_url: str = "http://localhost:8317",
    timeout: int = 5,
    management_token: Optional[str] = None
) -> Optional[Dict[str, Dict[str, AuthHealth]]]:
    """
    从 CPA 管理接口获取运行时健康数据

    Args:
        base_url: CPA 服务地址（例如 http://localhost:8317）
        timeout: 请求超时（秒）
        management_token: CPA 管理接口令牌（可选，优先从环境变量 CPA_MANAGEMENT_TOKEN 读取）

    Returns:
        按 provider 和 "base_url|api_key" 分组的健康数据
        格式: {
            "gemini": {
                "https://example.com|sk-xxx": AuthHealth(...)
            }
        }
        失败时返回 None
    """
    if not base_url:
        logger.debug("未配置 CPA 管理接口 URL，跳过运行时状态查询")
        return None

    # 构造完整 URL（CPA 路由: /v0/management/api-key-usage）
    url = f"{base_url.rstrip('/')}/v0/management/api-key-usage"

    # 从环境变量读取令牌（优先于参数）
    token = os.environ.get("CPA_MANAGEMENT_TOKEN") or management_token

    try:
        logger.info(f"查询 CPA 运行时健康状态: {url}")

        # 构造请求头
        headers = {}
        if token:
            headers["Authorization"] = f"Bearer {token}"
            logger.debug("使用管理令牌鉴权")

        resp = requests.get(url, timeout=timeout, headers=headers)
        resp.raise_for_status()

        raw_data = resp.json()
        logger.debug(f"收到 CPA 运行时数据，providers: {list(raw_data.keys())}")

        # 解析为 AuthHealth 对象
        result = {}
        for provider, auths in raw_data.items():
            result[provider] = {}
            for auth_key, stats in auths.items():
                health = AuthHealth(
                    success=stats.get("success", 0),
                    failed=stats.get("failed", 0),
                    recent_buckets=stats.get("recent_requests", [])
                )
                health.health_score = calculate_health_score(
                    health.success,
                    health.failed,
                    health.recent_buckets
                )
                result[provider][auth_key] = health

        return result

    except requests.Timeout:
        logger.warning(f"CPA 管理接口请求超时: {url}")
        return None
    except requests.ConnectionError as e:
        logger.warning(f"无法连接到 CPA 管理接口: {url} - {e}")
        return None
    except requests.HTTPError as e:
        logger.warning(f"CPA 管理接口返回错误: {e.response.status_code} - {e}")
        return None
    except Exception as e:
        logger.error(f"获取 CPA 运行时状态失败: {e}", exc_info=True)
        return None


def calculate_health_score(
    success: int,
    failed: int,
    recent_buckets: List[Dict[str, int]]
) -> float:
    """
    计算健康分数 = 可调度比例×60% + 活跃比例×40%

    Args:
        success: 成功请求数
        failed: 失败请求数
        recent_buckets: 最近请求桶列表（每桶包含 success 和 failed）

    Returns:
        健康分数 [0.0, 1.0]
    """
    total = success + failed

    # 可调度比例 = success / (success + failed)
    if total == 0:
        schedulable_ratio = 0.0
    else:
        schedulable_ratio = success / total

    # 活跃比例 = 最近有请求的桶数 / 总桶数
    if not recent_buckets:
        active_ratio = 0.0
    else:
        active_count = sum(
            1 for bucket in recent_buckets
            if bucket.get("success", 0) + bucket.get("failed", 0) > 0
        )
        active_ratio = active_count / len(recent_buckets)

    # 加权计算
    health_score = schedulable_ratio * 0.6 + active_ratio * 0.4

    logger.debug(
        f"健康分数计算: success={success}, failed={failed}, "
        f"schedulable={schedulable_ratio:.2f}, active={active_ratio:.2f}, "
        f"score={health_score:.3f}"
    )

    return health_score


def extract_health_from_detection(
    plan,
    historical_priority: int = 0
) -> float:
    """
    从检测结果预测健康分数（回退方案）

    Args:
        plan: SectionPlan 对象
        historical_priority: 历史优先级值（从 config.yaml 提取）

    Returns:
        预测健康分数 [0.0, 1.0]

    指标权重：
    - 检测成功 40% (能否成功调用)
    - 响应时间 20% (延迟越低越好)
    - 模型覆盖度 20% (支持的高级模型数量)
    - 上下文窗口 10% (max-context-length)
    - 历史优先级 10% (config.yaml 中的 priority)
    """
    scores = []

    # 1. 检测成功率 (40%)
    # Bug 修复 (2026-09-13): SectionPlan 无 has_base_models/models_final 属性，
    # 改用 models 列表(总是存在)与 highest_models(可选回退)判定探测成功。
    detected_models = getattr(plan, 'models', []) or getattr(plan, 'highest_models', [])
    if detected_models:
        success_score = 1.0  # 检测成功
    else:
        success_score = 0.0  # 检测失败
    scores.append(("detection_success", 0.4, success_score))

    # 2. 响应时间 (20%)
    # 假设良好延迟 < 2s, 可接受 < 5s, 超过 5s 视为慢
    if hasattr(plan, 'avg_latency_ms') and plan.avg_latency_ms:
        latency_s = plan.avg_latency_ms / 1000.0
        if latency_s < 2.0:
            latency_score = 1.0
        elif latency_s < 5.0:
            latency_score = 0.5
        else:
            latency_score = 0.2
    else:
        latency_score = 0.5  # 未知延迟，给中等分
    scores.append(("latency", 0.2, latency_score))

    # 3. 模型覆盖度 (20%)
    model_count = len(detected_models)
    if model_count >= 5:
        model_score = 1.0
    elif model_count >= 3:
        model_score = 0.7
    elif model_count >= 1:
        model_score = 0.4
    else:
        model_score = 0.0
    scores.append(("model_coverage", 0.2, model_score))

    # 4. 上下文窗口 (10%)
    if plan.max_context_length and plan.max_context_length > 0:
        # 归一化: 100k → 1.0, 50k → 0.5
        context_score = min(1.0, plan.max_context_length / 100000.0)
    else:
        context_score = 0.5  # 未知窗口，给中等分
    scores.append(("context_window", 0.1, context_score))

    # 5. 历史优先级 (10%)
    # 归一化: priority 100 → 1.0, priority 50 → 0.5, priority 0 → 0.0
    if historical_priority > 0:
        priority_score = min(1.0, historical_priority / 100.0)
    else:
        priority_score = 0.0
    scores.append(("historical_priority", 0.1, priority_score))

    # 加权求和
    total_score = sum(weight * score for _, weight, score in scores)

    logger.debug(
        f"检测预测分数: {' | '.join(f'{name}={score:.2f}' for name, _, score in scores)} "
        f"→ total={total_score:.3f}"
    )

    return total_score


def match_auth_to_plan(
    plan,
    runtime_health: Dict[str, Dict[str, AuthHealth]],
    section: str
) -> Optional[AuthHealth]:
    """
    将 SectionPlan 匹配到 CPA 运行时健康数据

    Args:
        plan: SectionPlan 对象
        runtime_health: 从 fetch_cpa_runtime_health() 获取的数据
        section: config.yaml 段名 (claude-api-key, codex-api-key, 等)

    Returns:
        匹配的 AuthHealth 对象，未找到时返回 None
    """
    if not runtime_health:
        return None

    # 段名到 provider 的映射
    # Bug 修复 (2026-09-13): CPA 的 provider 值与 parse.SECTIONS 键对齐。
    # 原先 "gemini" → "gemini-api-key", "openai-api-key" → "openai-compatibility"
    section_to_provider = {
        "claude-api-key": "claude",
        "codex-api-key": "codex",
        "gemini-api-key": "gemini",
        "openai-compatibility": "openai-compatible",
    }

    provider = section_to_provider.get(section)
    if not provider or provider not in runtime_health:
        return None

    # 构造匹配键: "base_url|api_key"
    # 注意: plan 中的 base_url 可能需要规范化（去除尾部斜杠等）
    base_url = plan.base_url.rstrip('/')
    api_key = plan.api_key

    auth_key = f"{base_url}|{api_key}"

    return runtime_health[provider].get(auth_key)


def get_domain_health_scores(
    plans: List,
    runtime_health: Optional[Dict[str, Dict[str, AuthHealth]]],
    cfg: dict,
    section: str
) -> Dict[str, float]:
    """
    计算每个域名的健康分数

    Args:
        plans: SectionPlan 列表
        runtime_health: CPA 运行时健康数据（可选）
        cfg: config.yaml 字典
        section: 段名

    Returns:
        {domain: health_score} 字典
    """
    from urllib.parse import urlparse

    domain_scores = {}
    domain_plans = {}

    # 按域名分组
    for plan in plans:
        parsed = urlparse(plan.base_url)
        domain = parsed.netloc or parsed.path

        if domain not in domain_plans:
            domain_plans[domain] = []
        domain_plans[domain].append(plan)

    # 计算每个域名的平均健康分数
    for domain, group_plans in domain_plans.items():
        scores = []

        for plan in group_plans:
            if runtime_health:
                # 优先: 从 CPA 运行时数据计算
                auth_health = match_auth_to_plan(plan, runtime_health, section)
                if auth_health:
                    score = auth_health.health_score
                    logger.debug(f"{domain} - {plan.api_key[:10]}...: 使用运行时分数 {score:.3f}")
                else:
                    # 回退: 从检测结果预测
                    historical_pri = _extract_historical_priority(cfg, section, plan)
                    score = extract_health_from_detection(plan, historical_pri)
                    logger.debug(f"{domain} - {plan.api_key[:10]}...: 运行时未匹配，使用检测预测 {score:.3f}")
            else:
                # 回退: 从检测结果预测
                historical_pri = _extract_historical_priority(cfg, section, plan)
                score = extract_health_from_detection(plan, historical_pri)
                logger.debug(f"{domain} - {plan.api_key[:10]}...: 无运行时数据，使用检测预测 {score:.3f}")

            scores.append(score)

        # 域名健康分数 = 所有 KEY 的平均分
        domain_scores[domain] = sum(scores) / len(scores) if scores else 0.0
        logger.info(f"域名 {domain} 健康分数: {domain_scores[domain]:.3f} (基于 {len(scores)} 个 KEY)")

    return domain_scores


def _extract_historical_priority(cfg: dict, section: str, plan) -> int:
    """从 config.yaml 提取历史优先级"""
    try:
        entries = cfg.get(section, [])
        for entry in entries:
            if (entry.get("base-url") == plan.base_url and
                entry.get("api-key") == plan.api_key):
                return int(entry.get("priority", 0))
    except Exception:
        pass
    return 0
