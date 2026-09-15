"""Offline source/request contracts; run directly with the pinned Python."""
import io
import os
import sys
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from cpa_probe import cpa_source_probe as csp
from cpa_probe import request as rq
from cpa_probe import parse


COMPAT = '''
package executor
func (e *Executor) Execute() {
    translated = helps.SetBoolIfDifferent(translated, "store", false)
}
func (e *Executor) ExecuteStream() {
    translated = helps.SetBoolIfDifferent(translated, "stream_options.include_usage", true)
}
func (e *Executor) cache() {
    if e.cfg.EnablePromptCacheKey {
        translated = helps.SetStringIfDifferent(translated, "prompt_cache_key", promptCacheKey)
    }
}
'''
CODEX = '''
package executor
func (e *Executor) Execute() {
    body = helps.SetBoolIfDifferent(body, "stream", true)
    body, _ = sjson.DeleteBytes(body, "previous_response_id")
}
func (e *Executor) compact() {
    body, _ = sjson.DeleteBytes(body, "stream")
}
'''


def fixtures():
    return {
        csp._CLAUDE_REQ: '''package executor
const (
claudeCodeBeta = "claude-code-fixture"
claudeOAuthBeta = "oauth-fixture"
)
var claudeCodeCLIConstantBetas = []string{
"thinking-fixture",
}
''',
        csp._CODEX_REQ: '''package executor
const (
codexUserAgent = "codex-fixture/1"
codexOriginator = "fixture"
)
''',
        csp._CODEX_EXEC: CODEX,
        csp._BODY_SHAPE_SOURCES["claude-api-key"][0]: '''package executor
func (e *Executor) Execute() {
body, _ = sjson.DeleteBytes(body, "diagnostics")
}''',
        csp._BODY_SHAPE_SOURCES["gemini-api-key"][0]: '''package executor
func (e *Executor) Execute() {
body, _ = sjson.DeleteBytes(body, "session_id")
}''',
        csp._BODY_SHAPE_SOURCES["openai-compatibility"][0]: COMPAT,
        csp._CLAUDE_FP: 'const ClaudeFingerprintProfileCLI = "claude-code-cli"',
        csp._CFG_TYPES: '// Mode controls cloaking behavior: "auto", "always", "never".',
        csp._CPAMP_UTILS: "export const DISABLE_ALL_MODELS_RULE = '*';",
        csp._CPAMP_TYPES: 'interface OpenAIProviderConfig {\n disabled?: boolean;\n}',
    }


def offline_read(root, rel):
    return fixtures().get(rel, "")


def test_conditional_not_forced():
    force, _ = csp.parse_body_shape(COMPAT, "translated")
    assert "stream_options.include_usage" not in force
    assert "prompt_cache_key" not in force
    assert force["store"] == "false"


def test_unknown_guard_not_forced():
    src = '''func (e *E) Execute() {
if mysterious() {
body = helps.SetBoolIfDifferent(body, "unsafe", true)
}
body = helps.SetBoolIfDifferent(body, "store", false)
}'''
    force, _ = csp.parse_body_shape(src)
    assert "unsafe" not in force
    assert force["store"] == "false"


def test_remote_local_parity():
    def get(url, **kw):
        for rel, text in fixtures().items():
            if url.endswith("/" + rel):
                return 200, text
        return 404, "fixture absent"
    with patch.object(csp, "_read", side_effect=offline_read), \
            patch.object(csp.os.path, "isdir", return_value=True), \
            patch.dict(os.environ, {"CPAMP_SOURCE_ROOT": "panel"}), \
            patch.object(csp, "_http_get", side_effect=get):
        local = csp.extract("local")
        remote = csp.extract_remote(ref="a" * 40, use_cache=False)
    for field in ("body_shape", "claude_fingerprint_profiles",
                  "claude_cloak_modes", "cpamp_disable_all_rule",
                  "cpamp_disabled_field"):
        assert getattr(remote, field) == getattr(local, field), field
    assert remote.body_shape


def test_partial_not_ok():
    with patch.object(csp, "_http_get", return_value=(
            200, fixtures()[csp._CLAUDE_REQ])):
        ident = csp.extract_remote(use_cache=False)
    assert not ident.ok, "beta-only snapshot must not claim complete coverage"


def test_cache_root_isolation():
    with patch.object(csp, "_read", side_effect=offline_read), \
            patch.object(csp.os.path, "isdir", return_value=True), \
            patch.object(csp, "_http_get", return_value=(404, "")):
        a = csp.cached_identity(source_root="source-a")
        b = csp.cached_identity(source_root="source-b")
    assert a.source_root != b.source_root


def test_auth_matches_cpa():
    with patch.object(csp, "cached_identity", return_value=csp.CpaIdentity()):
        _, third, _ = rq.build_request("claude-api-key", "https://relay.example",
                                      "m", "fixture")
        _, official, _ = rq.build_request("claude-api-key", "https://api.anthropic.com",
                                         "m", "fixture")
    assert third.get("Authorization") == "Bearer fixture"
    assert "x-api-key" not in third
    assert official.get("x-api-key") == "fixture"
    assert "Authorization" not in official


def test_dynamic_headers_not_literal():
    with patch.object(csp, "cached_identity", return_value=csp.CpaIdentity()):
        _, headers, _ = rq.build_request("claude-api-key", "https://relay.example",
                                        "m", "fixture",
                                        extra_headers={"X-Session": "$CPA-SESSION-ID"})
    assert "$CPA-SESSION-ID" not in headers.values()


def test_custom_channel_case():
    # 带路径前缀的 codex / compat base 要补 `/v1`（2026-09-12 改，docx 第 1/2 条）
    # --------------------------------------------------------------------
    # 这一行原来断言 `/ChannelA` 原样保留。那正是用户报的
    # 「同一个网址填进 cc-switch 能用、填进 CPA 不能用」的成因：
    # cc-switch 直接把整串 `.../ChannelA/v1` 当 base，而 CPA 按
    # `TrimSuffix(baseURL,"/") + "/responses"` 拼，base 少了 `/v1` 就拼成
    # `/ChannelA/responses` —— 站方真正的端点是 `/ChannelA/v1/responses`，404。
    # 段规则是「codex / compat 的 base 一律以 /v1 结尾」，与前面有没有路径
    # 前缀无关。
    #
    # 影响面有限且安全：`declared_base=True`（config.yaml 里已有的条目）
    # 完全不走这条补齐分支，那些值逐字保留；只有**新粘进来**的地址会被补。
    # 实测生产 config.yaml 的 45 个 codex + 16 个 compat 条目全部已以 /v1
    # 结尾，本次改动对它们零影响。
    assert parse.base_for_section("https://relay.example/ChannelA",
                                  "codex-api-key") == "https://relay.example/ChannelA/v1"
    # 已经带 /v1 的不再重复补
    assert parse.base_for_section("https://relay.example/ChannelA/v1",
                                  "codex-api-key") == "https://relay.example/ChannelA/v1"
    # config.yaml 里的既有值逐字保留，不补
    assert parse.base_for_section("https://relay.example/ChannelA",
                                  "codex-api-key",
                                  declared_base=True) == "https://relay.example/ChannelA"
    # 不需要 /v1 的两段不受影响
    assert parse.base_for_section("https://relay.example/ChannelA",
                                  "claude-api-key") == "https://relay.example/ChannelA"
    assert parse.base_for_section("https://relay.example/a",
                                  "codex-api-key") != parse.base_for_section(
                                      "https://relay.example/b", "codex-api-key")
    assert parse.base_for_section("https://relay.example/v1/responses",
                                  "codex-api-key") == "https://relay.example/v1"


def test_shape_refresh():
    one = csp.CpaIdentity(body_shape={"openai-compatibility": ({"store": "false"}, [])})
    two = csp.CpaIdentity(body_shape={"openai-compatibility": ({"store": "true"}, [])})
    with patch.object(csp, "cached_identity", side_effect=[one, two]):
        first = rq._body_shape_for("openai-compatibility")
        second = rq._body_shape_for("openai-compatibility")
    assert first != second


def test_stream_and_cache_conditions():
    ident = csp.CpaIdentity(body_rules={"openai-compatibility":
        csp.parse_body_rules(COMPAT, "translated")})
    for stream in (False, True):
        for enabled in (False, True):
            _, _, body = rq.build_request(
                "openai-compatibility", "https://relay.example/Channel", "m", "fixture",
                stream=stream, entry_config={"support-prompt-cache-key": enabled},
                source_identity=ident)
            assert ("stream_options" in body) == stream
            assert "prompt_cache_key" not in body, "unknown helper must not invent a session"


def test_codex_final_headers_and_stream():
    ident = csp.CpaIdentity(codex_user_agent="source-UA", codex_originator="source-origin")
    for disabled in (False, True):
        _, headers, body = rq.build_request(
            "codex-api-key", "https://relay.example/a", "m", "fixture",
            source_identity=ident, cfg={"codex": {"disable-codex-cloaking": disabled}},
            extra_headers={"User-Agent": "custom-UA", "Originator": "custom-origin"})
        assert body["stream"] is True
        assert headers["User-Agent"] == ("custom-UA" if disabled else "source-UA")
        assert headers["Originator"] == ("custom-origin" if disabled else "source-origin")


def test_declared_origin_and_channel():
    import json
    # JSON-compatible YAML fixture. YAML decoding belongs to the writeback task;
    # this contract consumes the resulting mapping, without an optional library.
    config = json.loads('{"base-url":"https://relay.example","headers":{"X-Test":"fixture"}}')
    ident = csp.CpaIdentity()
    url, _, _ = rq.build_request("codex-api-key", config["base-url"], "m", "fixture",
                                 declared_base=True, entry_config=config,
                                 source_identity=ident)
    assert url == "https://relay.example/responses"
    assert parse.base_for_section("https://relay.example/API/v1", "claude-api-key",
                                  declared_base=True) == "https://relay.example/API/v1"
    assert parse.parse_lines("https://relay.example/API/v1,fixture").valid[0].bare.endswith("/API/v1")


def test_cache_failure_expires_and_ref_isolation():
    csp._remote_cache.clear()
    with patch.object(csp, "_http_get", return_value=(404, "not found")) as get, \
            patch.object(csp.time, "time", return_value=1000):
        a = csp.extract_remote(ref="first")
        count = get.call_count
        assert csp.extract_remote(ref="first") is a
        assert get.call_count == count
        assert csp.extract_remote(ref="second") is not a
    with patch.object(csp, "_http_get", return_value=(404, "not found")) as get, \
            patch.object(csp.time, "time", return_value=1701):
        assert csp.extract_remote(ref="first") is not a
        assert get.call_count > 0


def test_dirty_source_refresh():
    data = fixtures()
    def read(root, rel):
        return data.get(rel, "")
    with patch.object(csp, "_read", side_effect=read):
        a = csp.cached_identity(source_root="dirty-source")
        data[csp._CODEX_EXEC] = CODEX.replace('"stream", true', '"stream", false')
        b = csp.cached_identity(source_root="dirty-source")
    assert a.snapshot_id != b.snapshot_id
    assert b.codex_body_force["stream"] == "false"


def test_headers_override_and_resolve():
    _, h, _ = rq.build_request(
        "claude-api-key", "https://relay.example", "m", "fixture",
        source_identity=csp.CpaIdentity(),
        entry_config={"headers": {"x-api-key": "custom-fixture",
                                  "authorization": "Bearer custom-fixture",
                                  "X-Client": "$X-Incoming", "X-Session": "$CPA-SESSION-ID"}},
        client_headers={"x-incoming": "resolved"}, session_id="session-fixture")
    assert h["authorization"] == "Bearer custom-fixture" and "Authorization" not in h
    assert h["X-Client"] == "resolved" and h["X-Session"] == "session-fixture"
    _, h2 = rq.models_endpoint("claude-api-key", "https://api.anthropic.com", "fixture")
    assert "Authorization" not in h2


def test_source_defaults_updateable():
    from cpa_probe import profiles, betas
    ident = csp.CpaIdentity(codex_user_agent="codex-updated/2",
                            codex_originator="updated",
                            claude_betas_unconditional=["claude-updated", "thinking-updated"],
                            claude_betas_conditional={"claudeContext1MBeta": "context-updated"},
                            claude_header_defaults={"user-agent": "claude-cli/9.8.7",
                                                    "package-version": "8.7.6"})
    items = profiles.ladder("claude-api-key", source_identity=ident)
    full = next(p for p in items if p.name == "cc-full")
    assert full.headers["anthropic-beta"] == "claude-updated,thinking-updated"
    assert full.headers["x-stainless-package-version"] == "8.7.6"
    assert betas.wanted("enable 1m context", source_identity=ident) == ["context-updated"]
    codex = profiles.ladder("codex-api-key", source_identity=ident)
    assert next(p for p in codex if p.name == "codex-tui").headers["originator"] == "updated"


def test_cpamp_enable_disable_semantics():
    utils = """
export const DISABLE_ALL_MODELS_RULE = '*';
export const stripDisableAllModelsRule = (models) =>
 models.filter((model) => String(model ?? '').trim() !== DISABLE_ALL_MODELS_RULE);
export const withDisableAllModelsRule = (models) => {
 const base = stripDisableAllModelsRule(models);
 return [...base, DISABLE_ALL_MODELS_RULE];
};
export const withoutDisableAllModelsRule = (models) => {
 const base = stripDisableAllModelsRule(models);
 return base;
};
"""
    rows = """
enabled: !hasDisableAllModelsRule(config.excludedModels),
enabled: provider.disabled !== true,
"""
    traits = csp.parse_cpamp_disable_semantics(utils, rows)
    assert traits["native_field"] == "excluded-models"
    assert traits["compat_field"] == "disabled"
    assert traits["enable_preserves_exclusions"] is True
    assert not csp.parse_cpamp_disable_semantics("", "")


def test_partial_check_exposes_coverage():
    with patch.object(csp, "_http_get", return_value=(200, fixtures()[csp._CLAUDE_REQ])):
        result = csp.check(allow_remote=True, remote_ref="partial-check")
    assert result.get("partial") is True
    assert result.get("coverage", {}).get("fingerprint") == "missing"
    assert result.get("snapshot_id")


def test_split_stream_file_and_unknown_values():
    src = CODEX + '''
func (e *Executor) ExecuteStream() {
body = helps.SetBoolIfDifferent(body, "stream", true)
if extraFeature() {
body = helps.SetStringIfDifferent(body, "unsafe", computedValue)
}
}
'''
    rules = csp.parse_body_rules(src)
    force, drop = csp.resolve_body_rules(rules, stream=True)
    assert force["stream"] == "true" and "stream" not in drop
    assert "unsafe" not in force
    assert any(r["unknown"] for r in rules)


def test_embedded_session_reference():
    result = rq.apply_custom_headers({}, {"X-Session": "prefix-$cpa-session-id"},
                                     session_id="session-fixture")
    assert result["X-Session"] == "prefix-session-fixture"
    assert rq.apply_custom_headers({}, {"X-Session": "prefix-$CPA-SESSION-ID"}) == {}


def test_remote_pins_and_success_cache():
    data = fixtures()
    data[csp._BODY_SHAPE_SOURCES["openai-compatibility"][0]] = COMPAT.split(
        "func (e *Executor) cache")[0]
    data["internal/runtime/executor/helps/claude_device_profile.go"] = '''
const (
defaultClaudeFingerprintUserAgent = "claude-cli/9.8.7"
defaultClaudeFingerprintPackageVersion = "8.7.6"
)'''
    data[csp._CPAMP_UTILS] += """
const stripDisableAllModelsRule = (models) =>
models.filter((m) => m !== DISABLE_ALL_MODELS_RULE);
const withDisableAllModelsRule = (models) => {
const base = stripDisableAllModelsRule(models);
return [...base, DISABLE_ALL_MODELS_RULE];
};
const withoutDisableAllModelsRule = (models) => {
const base = stripDisableAllModelsRule(models);
return base;
};
"""
    data["apps/web/src/components/providers/ProviderTable/rowData.ts"] = '''
enabled: !hasDisableAllModelsRule(config.excludedModels),
enabled: provider.disabled !== true,
'''
    data[csp._CODEX_EXEC] = CODEX.split("func (e *Executor) compact")[0]
    seen = []
    def get(url, **kw):
        seen.append(url)
        if "/commits/" in url:
            return 200, '{"sha":"' + "a" * 40 + '"}'
        for repo, paths in csp.SOURCE_MANIFEST.items():
            for rel in paths:
                if url.endswith("/" + rel):
                    return 200, data.get(rel, "package fixture")
        return 404, ""
    csp._remote_cache.clear()
    with patch.object(csp, "_http_get", side_effect=get), \
            patch.object(csp.time, "time", return_value=1000):
        first = csp.extract_remote(ref="release", cpamp_ref="panel")
    assert first.ok, first.coverage
    # 三个源都要被钉到 commit sha —— 2026-09-14 新增 sub2api 后这里曾漏掉它，
    # 断言写死 {cpa, cpamp} 而实现已返回三个 key，套件因此红。
    # 用 SOURCE_MANIFEST 推导而非再写死：以后加第四个源不必改这行。
    assert first.immutable
    assert first.revisions == {repo: "a" * 40 for repo in csp.SOURCE_MANIFEST}
    assert all("/" + "a" * 40 + "/" in url for url in seen if "/commits/" not in url)
    with patch.object(csp, "_http_get", side_effect=AssertionError("must use cache")), \
            patch.object(csp.time, "time", return_value=1701):
        assert csp.extract_remote(ref="release", cpamp_ref="panel") is first


def test_source_size_bound():
    class Response:
        status = 200
        def __enter__(self):
            return self
        def __exit__(self, *args):
            pass
        def read(self, limit):
            assert limit == csp._SOURCE_MAX_BYTES + 1
            return b"x" * limit
    with patch("urllib.request.build_opener") as build:
        build.return_value.open.return_value = Response()
        status, body = csp._http_get("https://raw.githubusercontent.com/o/r/ref/file.go")
    assert status == 0 and "limit" in body


def test_entrypoint_guard_cannot_switch_executor():
    rules = csp.parse_body_rules('''
func (e *Executor) ExecuteStream() {
if !stream {
body = helps.SetBoolIfDifferent(body, "unsafe", true)
}
}''')
    assert "unsafe" not in csp.resolve_body_rules(rules, stream=False)[0]
    assert "unsafe" not in csp.resolve_body_rules(rules, stream=True)[0]


def test_declared_full_endpoint_not_doubled():
    try:
        parse.base_for_section("https://relay.example/v1/responses", "codex-api-key",
                               declared_base=True)
    except ValueError:
        return
    raise AssertionError("ambiguous declared endpoint must be rejected")


def test_supported_cache_flag_controls_literal():
    rules = csp.parse_body_rules('''
func (e *Executor) Execute() {
if e.cfg.SupportPromptCacheKey {
translated = helps.SetStringIfDifferent(translated, "prompt_cache_key", "fixture-session")
}
}''', "translated")
    assert not csp.resolve_body_rules(rules)[0]
    assert not csp.resolve_body_rules(rules, config={"support-prompt-cache-key": False})[0]
    assert csp.resolve_body_rules(rules, config={"support-prompt-cache-key": True})[0] == {
        "prompt_cache_key": '"fixture-session"'}


def test_stream_forcing_comes_from_source_not_a_hardcoded_section():
    """「这一段强不强制流式」必须从 CPA 源码读，不许写死段名。

    2026-09-12。探测要问的是「CPA 这样发通不通」，所以请求形态必须与 CPA
    实际转发的一致。而 pipeline 原来写的是 `section == "codex-api-key"`，
    与 cpa_source_probe 自己解析出来的表不一致（那张表里 gemini 段也强制
    stream=true）。后果是用户 2026-09-12 报的「空 HTTP 200 + 0 个 SSE
    事件」的探测侧盲点：非流式 JSON 探通了，而 CPA 走流式，站方流式路径
    一个事件都不吐 —— 工具照样判它可用。

    守三件事：
      ① 判据真的来自 body_rules（换一份 rules，结论跟着换）
      ② 带 unknown 条件的改写不算数 —— 静态解析给不出确定结论时不能硬套
      ③ 读不出结论时返回 None，让调用方保留自己的保守默认
    """
    from types import SimpleNamespace

    def ident(rules):
        return SimpleNamespace(body_rules=rules)

    force_rule = {"path": "stream", "op": "set", "value": "true",
                  "conditions": {}, "unknown": []}
    assert csp.forces_stream(ident({"s": [force_rule]}), "s") is True
    # 解析过这一段、但没有强制流式那条 → False（不是「不知道」）
    assert csp.forces_stream(ident({"s": []}), "s") is False
    # 整段没解析到 → None，调用方保留自己的默认
    assert csp.forces_stream(ident({}), "s") is None
    assert csp.forces_stream(SimpleNamespace(), "s") is None
    # 带未知条件的改写不算数
    unknown = dict(force_rule, unknown=["helper:executeCompact"])
    assert csp.forces_stream(ident({"s": [unknown]}), "s") is False
    # drop 也不算「强制流式」
    drop = {"path": "stream", "op": "drop", "value": "",
            "conditions": {}, "unknown": []}
    assert csp.forces_stream(ident({"s": [drop]}), "s") is False

    # 接线：pipeline 必须用它，且不得再按段名写死
    src = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                       "cpa_probe", "pipeline.py")
    with io.open(src, encoding="utf-8") as fh:
        text = fh.read()
    assert "forces_stream" in text, "pipeline 没用上源码判据"
    body = "\n".join(ln for ln in text.splitlines()
                      if not ln.lstrip().startswith("#"))
    assert 'stream = section == "codex-api-key"' not in body, (
        "pipeline 又按段名写死了强制流式 —— 那是 docx 第 6 条禁止的硬编码")


def main():
    failed = []
    tests = [(name, fn) for name, fn in globals().items() if name.startswith("test_")]
    for name, fn in tests:
        try:
            fn()
            print("[PASS]", name)
        except Exception as exc:
            failed.append(name)
            print("[FAIL]", name, type(exc).__name__, str(exc))
    print(f"{len(tests) - len(failed)} passed, {len(failed)} failed")
    return bool(failed)


if __name__ == "__main__":
    raise SystemExit(main())
