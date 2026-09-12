#!/usr/bin/env python3
"""
健康分数优先级分配集成测试 - 简化版本

直接测试健康分数计算和排序逻辑，不依赖完整的 ImportPlan/SectionPlan 创建
"""

import sys
sys.path.insert(0, '.')

from cpa_probe.runtime_health import calculate_health_score, extract_health_from_detection


def main():
    print("=" * 60)
    print("Health Score Priority Assignment Test")
    print("=" * 60)
    
    # 场景：3 个域名运行时健康数据
    domains = {
        "kilo.example.com": {
            "success": 200,
            "failed": 10,
            "recent_buckets": [{"success": 5, "failed": 0} for _ in range(15)]
        },
        "tango.example.com": {
            "success": 100,
            "failed": 50,
            "recent_buckets": [{"success": 3, "failed": 2} if i < 10 else {"success": 0, "failed": 0} for i in range(20)]
        },
        "hotel.example.com": {
            "success": 20,
            "failed": 80,
            "recent_buckets": [{"success": 1, "failed": 4} if i < 5 else {"success": 0, "failed": 0} for i in range(20)]
        }
    }
    
    print("\n1. Runtime Health Data:")
    print("-" * 60)
    
    health_scores = {}
    for domain, data in domains.items():
        score = calculate_health_score(
            success=data["success"],
            failed=data["failed"],
            recent_buckets=data["recent_buckets"]
        )
        health_scores[domain] = score
        
        schedulable = data["success"] / (data["success"] + data["failed"])
        active_count = sum(1 for b in data["recent_buckets"] if b["success"] > 0 or b["failed"] > 0)
        active_ratio = active_count / len(data["recent_buckets"])
        
        print(f"\n  Domain: {domain}")
        print(f"    Success/Failed: {data['success']}/{data['failed']}")
        print(f"    Schedulable Ratio: {schedulable:.3f} (weight 60%)")
        print(f"    Active Buckets: {active_count}/{len(data['recent_buckets'])}")
        print(f"    Active Ratio: {active_ratio:.3f} (weight 40%)")
        print(f"    Health Score: {score:.3f}")
    
    # 排序测试
    print("\n2. Sorting by Health Score:")
    print("-" * 60)
    
    sorted_domains = sorted(health_scores.items(), key=lambda x: -x[1])
    for i, (domain, score) in enumerate(sorted_domains, 1):
        print(f"  Rank {i}: {domain} (score={score:.3f})")
    
    # 验证测试
    print("\n3. Verification Tests:")
    print("-" * 60)
    
    kilo_score = health_scores["kilo.example.com"]
    tango_score = health_scores["tango.example.com"]
    hotel_score = health_scores["hotel.example.com"]
    
    test_results = []
    
    test_results.append((
        "High-health domain has highest score",
        kilo_score > tango_score and kilo_score > hotel_score
    ))
    
    test_results.append((
        "Medium-health domain has middle score",
        tango_score > hotel_score and tango_score < kilo_score
    ))
    
    test_results.append((
        "Low-health domain has lowest score",
        hotel_score < tango_score and hotel_score < kilo_score
    ))
    
    test_results.append((
        "Scores are monotonically decreasing",
        kilo_score > tango_score > hotel_score
    ))
    
    test_results.append((
        "Sorting order matches expected: kilo > tango > hotel",
        sorted_domains[0][0] == "kilo.example.com" and
        sorted_domains[1][0] == "tango.example.com" and
        sorted_domains[2][0] == "hotel.example.com"
    ))
    
    for desc, passed in test_results:
        status = "PASS" if passed else "FAIL"
        print(f"  [{status}] {desc}")
    
    # 检测预测回退测试
    print("\n4. Detection Fallback Prediction Test:")
    print("-" * 60)
    
    class MockPlan:
        def __init__(self, score, latency, models, context):
            self.has_base_models = True
            self.models_final = models
            self.avg_latency_ms = latency
            self.max_context_length = context
            self.score = score
    
    fallback_scenarios = [
        ("High-quality", MockPlan(0.95, 1000, ["claude-opus-5", "claude-sonnet-5"], 200000), 85),
        ("Medium-quality", MockPlan(0.70, 2000, ["claude-sonnet-5"], 150000), 60),
        ("Low-quality", MockPlan(0.40, 3500, ["claude-haiku-4-5"], 80000), 30),
    ]
    
    fallback_scores = {}
    for name, plan, historical_pri in fallback_scenarios:
        score = extract_health_from_detection(plan, historical_pri)
        fallback_scores[name] = score
        print(f"  {name}: score={score:.3f} (historical_pri={historical_pri})")
    
    test_results.append((
        "Fallback scores: High > Medium > Low",
        fallback_scores["High-quality"] > fallback_scores["Medium-quality"] > fallback_scores["Low-quality"]
    ))
    
    # 总结
    print("\n" + "=" * 60)
    passed_count = sum(1 for _, p in test_results if p)
    total_count = len(test_results)
    print(f"Tests Passed: {passed_count}/{total_count}")
    
    if passed_count == total_count:
        print("Status: All tests passed")
        return 0
    else:
        print("Status: Some tests failed")
        return 1


if __name__ == "__main__":
    sys.exit(main())
