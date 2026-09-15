#!/usr/bin/env python3
"""sub2api 校验判据的解析测试（用户 2026-09-14 要求 5：动态引用上游）。

守的是 `parse_sub2api_validator` —— 它从 sub2api 的
`backend/internal/service/claude_code_validator.go` 抽出 claude_code_only
分组的四项校验判据，供 plan.py 在「客户端」类判死时写进警告文案。

为什么这些判据必须从上游解析而不是抄下来：用户第 1 条问的那个 503
（`only allows Claude Code clients`）就发生在那个 validator 里。阈值或
UA 正则一改，抄在本项目里的知识立刻变成错的，而错的表现是操作员照着
警告去补身份、补完仍然 503。

本测试用**内联的源码片段**，不打网络 —— CI 与离线环境都要能跑。
片段照抄上游真实形态（2026-09-14 实测拉取），改动时请同步。
"""
import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from cpa_probe.cpa_source_probe import parse_sub2api_validator

# 上游真实片段（2026-09-14 从 Wei-Shaw/sub2api@main 拉取后节选）。
# 保留原注释与结构，让「上游改了形态」这件事在 diff 里看得见。
_REAL = r'''
package service

var (
	// User-Agent 匹配: claude-cli/x.x.x (仅支持官方 CLI，大小写不敏感)
	claudeCodeUAPattern = regexp.MustCompile(`(?i)^claude-cli/\d+\.\d+\.\d+`)

	// 带捕获组的版本提取正则
	claudeCodeUAVersionPattern = regexp.MustCompile(`(?i)^claude-cli/(\d+\.\d+\.\d+)`)

	// System prompt 相似度阈值（默认 0.5，和 claude-relay-service 一致）
	systemPromptThreshold = 0.5
)

var claudeCodeSystemPrompts = []string{
	"You are Claude Code, Anthropic's official CLI for Claude.",
	"You are a Claude agent, built on Anthropic's Claude Agent SDK.",
	"You are Claude Code, Anthropic's official CLI for Claude, running within the Claude Agent SDK.",
	"You are a file search specialist for Claude Code, Anthropic's official CLI for Claude.",
	"You are a helpful AI assistant tasked with summarizing conversations.",
	"You are an interactive CLI tool that helps users",
}

func (v *ClaudeCodeValidator) Validate(r *http.Request, body map[string]any) bool {
	ua := r.Header.Get("User-Agent")
	if !claudeCodeUAPattern.MatchString(ua) {
		return false
	}
	xApp := r.Header.Get("X-App")
	if xApp == "" {
		return false
	}
	anthropicBeta := r.Header.Get("anthropic-beta")
	if anthropicBeta == "" {
		return false
	}
	anthropicVersion := r.Header.Get("anthropic-version")
	if anthropicVersion == "" {
		return false
	}
	return true
}
'''


def test_parses_all_four_criteria() -> None:
    got = parse_sub2api_validator(_REAL)

    assert got.get("ua_pattern") == r"(?i)^claude-cli/\d+\.\d+\.\d+", \
        f"UA 正则解析错误：{got.get('ua_pattern')!r}"
    assert got.get("prompt_threshold") == 0.5, \
        f"相似度阈值解析错误：{got.get('prompt_threshold')!r}"

    heads = got.get("required_headers") or []
    for want in ("X-App", "anthropic-beta", "anthropic-version"):
        assert want in heads, f"必需头缺 {want}，实得 {heads}"
    assert "User-Agent" not in heads, \
        "UA 由正则单独校验，不该混进必需头清单"

    prompts = got.get("system_prompts") or []
    assert len(prompts) == 6, f"应解析出 6 条模板，实得 {len(prompts)}"
    assert prompts[0].startswith("You are Claude Code"), \
        f"首条模板不对：{prompts[0]!r}"
    print("[PASS] parses all four criteria")


def test_empty_source_yields_empty_dict() -> None:
    """拉不到源码时必须返回空 —— 调用方据此退回泛化文案，绝不猜阈值。"""
    assert parse_sub2api_validator("") == {}, "空源码必须返回空 dict"
    assert parse_sub2api_validator("package service\n") == {}, \
        "无关源码必须返回空 dict"
    print("[PASS] empty source -> empty dict")


def test_partial_source_degrades_per_field() -> None:
    """只认出一部分时，认出的照给、认不出的缺席 —— 不整体放弃。"""
    partial = 'systemPromptThreshold = 0.75\n'
    got = parse_sub2api_validator(partial)
    assert got.get("prompt_threshold") == 0.75, f"实得 {got}"
    assert "ua_pattern" not in got, "没有的字段不该凭空出现"
    print("[PASS] partial source degrades per field")


def test_threshold_change_is_picked_up() -> None:
    """上游改阈值时必须跟着变 —— 这正是不写死的意义。"""
    changed = _REAL.replace("systemPromptThreshold = 0.5",
                            "systemPromptThreshold = 0.8")
    got = parse_sub2api_validator(changed)
    assert got.get("prompt_threshold") == 0.8, \
        f"上游改成 0.8 后应跟随，实得 {got.get('prompt_threshold')}"
    print("[PASS] threshold change picked up")


def main() -> int:
    test_parses_all_four_criteria()
    test_empty_source_yields_empty_dict()
    test_partial_source_degrades_per_field()
    test_threshold_change_is_picked_up()
    print("\n全部通过 · 4 项")
    return 0


if __name__ == "__main__":
    sys.exit(main())
