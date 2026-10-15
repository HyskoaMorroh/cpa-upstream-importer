# 路径引导（2026-10-12 补）：本文件原来既没注册进 tests/run.py，也没有
# __main__ 入口，更没有这段 sys.path 引导 —— 三重隐形。单独跑会直接
# ModuleNotFoundError: No module named 'cpa_probe'，
# 这本身就是它一次都没被执行过的证据。
from pathlib import Path as _Path
import sys as _sys
_sys.path.insert(0, str(_Path(__file__).resolve().parents[1]))

"""Bounded source parsing must not mistake discovered syntax for wire coverage."""
from cpa_probe import cpa_source_probe as source


def parse(body):
    return source.parse_body_rules("func (e *Executor) Execute() {\n" + body + "\n}")


def test_full_expression_preserved_and_dynamic_path_counted():
    rules = parse('body, _ = sjson.SetBytes(body, "cache", cached.ID)\n'
                  'body, _ = sjson.SetBytes(body, fmt.Sprintf("input.%d.id", i), item.Get("call_id").String())')
    assert rules[0]["value"] == "cached.ID"
    assert rules[0]["unknown"]
    assert any("dynamic-sjson-path" in r["unknown"] for r in rules)
    assert all(r["line"] > 0 and r["function"] == "Execute" for r in rules)
    assert source.resolve_body_rules(rules) == ({}, [])


def test_whole_body_helpers_invalidate_earlier_final_value():
    rules = parse('body = helps.SetBoolIfDifferent(body, "stream", true)\n'
                  'body, state, err = externalTransform(\nbody, config, cached.ID)')
    assert any("unresolved-body-assignment" in r["unknown"] for r in rules)
    assert source.resolve_body_rules(rules) == ({}, [])


def test_later_proven_setter_after_opaque_helper_remains_usable():
    rules = parse('body = externalTransform(body)\n'
                  'body, _ = sjson.SetBytes(body, "stream", true)')
    assert source.resolve_body_rules(rules) == ({"stream": "true"}, [])
    assert any(r["unknown"] for r in rules)  # still partial overall


def test_setbytes_typed_string_is_not_decoded_as_raw_json():
    typed = parse('body, _ = sjson.SetBytes(body, "flag", "true")')
    assert source.resolve_body_rules(typed) == ({"flag": '"true"'}, [])
    raw = parse('body, _ = sjson.SetRawBytes(body, "flag", []byte("true"))')
    assert source.resolve_body_rules(raw) == ({}, [])
    assert raw[0]["unknown"]


def test_numeric_values_not_claimed_executable_by_string_bool_consumer():
    rules = parse('body, _ = sjson.SetBytes(body, "max_tokens", 1024)')
    assert rules[0]["unknown"]
    assert source.resolve_body_rules(rules) == ({}, [])


def test_unknown_specific_write_invalidates_only_that_field():
    rules = parse('body = helps.SetBoolIfDifferent(body, "stream", true)\n'
                  'body = helps.SetBoolIfDifferent(body, "store", false)\n'
                  'body, _ = sjson.SetBytes(body, "stream", getStream())')
    assert source.resolve_body_rules(rules) == ({"store": "false"}, [])


def test_error_guard_return_preserves_normal_path_but_else_is_unknown():
    rules = parse('if abort { return response, nil }\n'
                  'body = helps.SetBoolIfDifferent(body, "stream", true)')
    assert source.resolve_body_rules(rules) == ({"stream": "true"}, [])
    rules = parse('if stream {\nbody = helps.SetBoolIfDifferent(body, "stream", true)\n'
                  '} else {\nbody = helps.SetBoolIfDifferent(body, "stream", false)\n}')
    assert source.resolve_body_rules(rules) == ({}, [])


def test_other_entrypoint_and_uncalled_helpers_do_not_invalidate():
    rules = parse('body = helps.SetBoolIfDifferent(body, "stream", true)')
    rules += source.parse_body_rules('func helper() {\nbody = transform(body)\n}\n'
                                     'func (e *Executor) ExecuteStream() {\nbody = transform(body)\n}')
    assert source.resolve_body_rules(rules) == ({"stream": "true"}, [])


def test_unverified_receiver_not_treated_as_sjson():
    rules = parse('body = unknown.SetBytes(body, "stream", true)')
    assert rules[0]["unknown"]
    assert source.resolve_body_rules(rules) == ({}, [])


def test_nested_setter_does_not_escape_outer_unknown_transform():
    rules = parse('body = transform(helps.SetBoolIfDifferent(body, "stream", true))')
    assert source.resolve_body_rules(rules) == ({}, [])
    assert all(r["unknown"] for r in rules)


def test_unconditional_return_does_not_apply_unreachable_setter():
    rules = parse('return response, nil\n'
                  'body = helps.SetBoolIfDifferent(body, "stream", true)')
    assert source.resolve_body_rules(rules) == ({}, [])
    assert "preceding-return-control-flow" in rules[0]["unknown"]


def test_codex_normal_path_setters_survive_error_guard_until_later_transform():
    # Ordering taken from the pinned CPA Codex Execute path: thinking error
    # guard, model selection, stream/delete setters, then body transformations.
    normal = ('body, err = helps.ApplyRequestThinking(body, req.Model)\n'
              'if err != nil { return response, err }\n'
              'body, _ = sjson.SetBytes(body, "model", baseModel)\n'
              'body, _ = sjson.SetBytes(body, "stream", true)\n'
              'body, _ = sjson.DeleteBytes(body, "previous_response_id")')
    rules = parse(normal)
    assert source.resolve_body_rules(rules) == ({"stream": "true"}, ["previous_response_id"])
    assert any("nonliteral:baseModel" in r["unknown"] for r in rules)
    rules = parse(normal + '\nbody = transformForUpstream(body)')
    assert source.resolve_body_rules(rules) == ({}, [])
    assert any("unresolved-body-assignment" in r["unknown"] for r in rules)


# 入口（2026-10-12 补）。本文件原来只有 12 个模块级 `def test_*`，既没进
# `tests/run.py` 的清单，也没有 `__main__` 块 —— 两重隐形：注册了也会
# 「执行 0 个用例、退出码 0」，被 run.py 记成通过。这 12 条 CPA 源码 body
# 规则边界用例因此一次都没跑过。
#
# 不引 pytest：本仓库的测试一律是「python tests/xxx.py」直接跑，run.py 也按
# 这个方式 subprocess 调用。保持一致，并打印 run.py 认得的小结格式。
def _main() -> int:
    import traceback
    tests = [(name, obj) for name, obj in sorted(globals().items())
             if name.startswith("test_") and callable(obj)]
    failed = []
    for name, fn in tests:
        try:
            fn()
            print(f"  ok  {name}")
        except Exception:                      # noqa: BLE001 —— 逐条隔离
            failed.append(name)
            print(f"  ✗ {name}")
            traceback.print_exc()
    if failed:
        print()
        print(f"失败 {len(failed)} 项 / 通过 {len(tests) - len(failed)} 项")
        return 1
    print()
    print(f"全部通过 · {len(tests)} 项")
    return 0


if __name__ == "__main__":
    import sys
    sys.exit(_main())
