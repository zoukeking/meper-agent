"""ExpressionEngine unit tests — type preservation, resolve, resolve_bool, resolve_str, resolve_dict."""
from __future__ import annotations

from app.engine.workflow.expression import ExpressionEngine

# ── resolve ──


class TestResolve:
    """Tests for ExpressionEngine.resolve()."""

    def test_single_expression_returns_raw_value(self) -> None:
        engine = ExpressionEngine({"node1": {"status": "ok"}})
        assert engine.resolve("{{ node1.status }}") == "ok"

    def test_multiple_expressions_string_substitution(self) -> None:
        engine = ExpressionEngine({"a": "hello", "b": "world"})
        result = engine.resolve("{{ a }} plus {{ b }}")
        assert "hello" in result
        assert "world" in result

    def test_no_expression_returns_template(self) -> None:
        engine = ExpressionEngine({})
        assert engine.resolve("plain text") == "plain text"

    def test_mixed_text_expression(self) -> None:
        engine = ExpressionEngine({"name": "Alice"})
        assert engine.resolve("Hello {{ name }}!") == "Hello Alice!"

    def test_empty_string_returns_empty(self) -> None:
        engine = ExpressionEngine({})
        assert engine.resolve("") == ""

    def test_none_template_returns_none(self) -> None:
        engine = ExpressionEngine({})
        assert engine.resolve(None) is None  # type: ignore[arg-type]


# ── type preservation ──


class TestTypePreservation:
    """Tests that _eval_expression_typed preserves Python types."""

    def test_int_preserved(self) -> None:
        engine = ExpressionEngine({"count": 42})
        result = engine.resolve("{{ count }}")
        assert result == 42
        assert isinstance(result, int)

    def test_float_preserved(self) -> None:
        engine = ExpressionEngine({"score": 3.14})
        result = engine.resolve("{{ score }}")
        assert result == 3.14
        assert isinstance(result, float)

    def test_bool_true_preserved(self) -> None:
        engine = ExpressionEngine({"flag": True})
        result = engine.resolve("{{ flag }}")
        assert result is True

    def test_bool_false_preserved(self) -> None:
        engine = ExpressionEngine({"flag": False})
        result = engine.resolve("{{ flag }}")
        assert result is False

    def test_none_preserved(self) -> None:
        engine = ExpressionEngine({"value": None})
        # Jinja2 renders None as "None" string, then ast.literal_eval restores it
        result = engine.resolve("{{ value }}")
        assert result is None

    def test_list_preserved(self) -> None:
        engine = ExpressionEngine({"items": [1, 2, 3]})
        result = engine.resolve("{{ items }}")
        assert result == [1, 2, 3]

    def test_dict_preserved(self) -> None:
        engine = ExpressionEngine({"data": {"key": "val"}})
        result = engine.resolve("{{ data }}")
        assert result == {"key": "val"}

    def test_string_stays_string(self) -> None:
        engine = ExpressionEngine({"name": "hello world"})
        result = engine.resolve("{{ name }}")
        assert result == "hello world"
        assert isinstance(result, str)


# ── resolve_bool ──


class TestResolveBool:
    """Tests for ExpressionEngine.resolve_bool()."""

    def test_python_true(self) -> None:
        engine = ExpressionEngine({"flag": True})
        assert engine.resolve_bool("{{ flag }}") is True

    def test_python_false(self) -> None:
        engine = ExpressionEngine({"flag": False})
        assert engine.resolve_bool("{{ flag }}") is False

    def test_string_true(self) -> None:
        engine = ExpressionEngine({"v": "true"})
        assert engine.resolve_bool("{{ v }}") is True

    def test_string_false(self) -> None:
        engine = ExpressionEngine({"v": "false"})
        assert engine.resolve_bool("{{ v }}") is False

    def test_nonexistent_expression_returns_false(self) -> None:
        engine = ExpressionEngine({})
        # Undefined variable renders to empty string in ChainableUndefined
        assert engine.resolve_bool("{{ nonexistent_var }}") is False

    def test_nonzero_int(self) -> None:
        engine = ExpressionEngine({"n": 1})
        assert engine.resolve_bool("{{ n }}") is True

    def test_zero_int(self) -> None:
        engine = ExpressionEngine({"n": 0})
        assert engine.resolve_bool("{{ n }}") is False

    def test_nonzero_float(self) -> None:
        engine = ExpressionEngine({"n": 0.5})
        assert engine.resolve_bool("{{ n }}") is True

    def test_empty_string_resolved(self) -> None:
        engine = ExpressionEngine({"v": ""})
        assert engine.resolve_bool("{{ v }}") is False


# ── resolve_dict ──


class TestResolveDict:
    """Tests for ExpressionEngine.resolve_dict()."""

    def test_nested_dict_resolution(self) -> None:
        engine = ExpressionEngine({"node1": {"result": "ok"}})
        config = {"outer": {"inner": "{{ node1.result }}"}}
        result = engine.resolve_dict(config)
        assert result == {"outer": {"inner": "ok"}}

    def test_list_expression_resolution(self) -> None:
        engine = ExpressionEngine({"a": "x", "b": "y"})
        config = {"items": ["{{ a }}", "{{ b }}", "plain"]}
        result = engine.resolve_dict(config)
        assert result == {"items": ["x", "y", "plain"]}

    def test_non_string_values_preserved(self) -> None:
        engine = ExpressionEngine({})
        config = {"count": 42, "flag": True, "data": [1, 2]}
        result = engine.resolve_dict(config)
        assert result == {"count": 42, "flag": True, "data": [1, 2]}

    def test_empty_dict(self) -> None:
        engine = ExpressionEngine({})
        assert engine.resolve_dict({}) == {}

    def test_mixed_types_in_dict(self) -> None:
        engine = ExpressionEngine({"name": "Alice", "age": 30})
        config = {
            "greeting": "Hello {{ name }}",
            "raw_number": 42,
            "nested": {"key": "{{ name }}"},
        }
        result = engine.resolve_dict(config)
        assert result["greeting"] == "Hello Alice"
        assert result["raw_number"] == 42
        assert result["nested"]["key"] == "Alice"


# ── resolve_str ──


class TestResolveStr:
    """Tests for ExpressionEngine.resolve_str().

    resolve_str must ALWAYS return a str — this is the contract that lets
    callers (e.g. AgentNodeExecutor) do ``.strip()`` / f-string formatting
    without an AttributeError when the resolved value is a bool/int/dict
    (which happens when an agent dispatches a workflow with JSON-native
    params like ``{"flag": true}``).
    """

    def test_none_template_returns_empty(self) -> None:
        """None template → '' (fail-safe, mirrors resolve_bool)."""
        engine = ExpressionEngine({})
        assert engine.resolve_str(None) == ""  # type: ignore[arg-type]

    def test_empty_string_returns_empty(self) -> None:
        """Empty template → ''."""
        engine = ExpressionEngine({})
        assert engine.resolve_str("") == ""

    def test_bool_true_returns_str_true(self) -> None:
        """Single {{var}} resolving to Python True → 'True' (not bool)."""
        engine = ExpressionEngine({"input": {"flag": True}})
        result = engine.resolve_str("{{ input.flag }}")
        assert result == "True"
        assert isinstance(result, str)

    def test_bool_false_returns_str_false(self) -> None:
        """Single {{var}} resolving to Python False → 'False' (not bool)."""
        engine = ExpressionEngine({"input": {"flag": False}})
        result = engine.resolve_str("{{ input.flag }}")
        assert result == "False"
        assert isinstance(result, str)

    def test_int_returns_str(self) -> None:
        """Single {{var}} resolving to int → '5'."""
        engine = ExpressionEngine({"input": {"count": 5}})
        result = engine.resolve_str("{{ input.count }}")
        assert result == "5"
        assert isinstance(result, str)

    def test_dict_returns_json(self) -> None:
        """Single {{var}} resolving to dict → JSON text."""
        engine = ExpressionEngine({"input": {"obj": {"k": "v"}}})
        result = engine.resolve_str("{{ input.obj }}")
        assert result == '{"k": "v"}'
        assert isinstance(result, str)

    def test_list_returns_json(self) -> None:
        """Single {{var}} resolving to list → JSON text."""
        # Note: avoid key name "items" — Jinja2 would resolve input.items as
        # the built-in dict.items method instead of the key lookup.
        engine = ExpressionEngine({"input": {"tags": [1, 2]}})
        result = engine.resolve_str("{{ input.tags }}")
        assert result == "[1, 2]"
        assert isinstance(result, str)

    def test_comparison_operator_in_text_not_evaluated(self) -> None:
        """Templates containing ``==``/``>`` etc. are NOT evaluated as comparisons.

        Regression: previously ``_try_eval_comparison`` ran on every rendered
        string, so any text containing ``>`` / ``<`` (markdown blockquotes,
        SQL, code, math) was mis-classified as a comparison and returned a
        bool. Comparison evaluation now lives only in the Gateway node's
        explicit ``operator``+``expected`` fields.
        """
        engine = ExpressionEngine({"input": {"x": "done"}})
        # The whole template is returned as-is (no implicit comparison).
        result = engine.resolve_str('{{ input.x }} == "done"')
        assert result == 'done == "done"'
        assert isinstance(result, str)

    def test_markdown_blockquote_in_resolved_value(self) -> None:
        """A resolved value containing markdown ``>`` must not become bool.

        Real-world regression: an upstream Agent node produced a response with
        a markdown blockquote (``> ⚠️ ...``). The downstream Agent node's
        ``input_query`` template ``...{{prev.response}}...`` was rendered to a
        string containing ``>``, which ``_try_eval_comparison`` split on ``>``
        and compared as ``"text before" > "text after"`` → ``True``. The Agent
        then received ``"True"`` as its user message and asked the user for
        input instead of acting on the real context.
        """
        md_response = "查询完成。结果如下：\n\n> ⚠️ 以上信息基于工具返回\n\n如需详情请联系"
        engine = ExpressionEngine({"prev": {"response": md_response}})
        template = "上一步结果：\n{{ prev.response }}\n请继续。"
        result = engine.resolve_str(template)
        # Must contain the full markdown text, NOT collapse to "True".
        assert result != "True"
        assert "⚠️ 以上信息基于工具返回" in result
        assert "请继续。" in result
        assert isinstance(result, str)

    def test_plain_string_passthrough(self) -> None:
        """String with no expressions is returned as-is."""
        engine = ExpressionEngine({})
        result = engine.resolve_str("just plain text")
        assert result == "just plain text"

    def test_mixed_text_substitution(self) -> None:
        """General path (text + {{var}}) substitutes and stays str."""
        engine = ExpressionEngine({"input": {"n": 5}})
        result = engine.resolve_str("count is {{ input.n }}")
        assert result == "count is 5"
        assert isinstance(result, str)

    def test_none_variable_returns_empty(self) -> None:
        """Single {{var}} resolving to None → '' (not the string 'None')."""
        engine = ExpressionEngine({"input": {"missing": None}})
        result = engine.resolve_str("{{ input.missing }}")
        assert result == ""


# ── _eval_expression_typed ──


class TestEvalExpressionTyped:
    """Tests for ExpressionEngine._eval_expression_typed()."""

    def test_int_type_restore(self) -> None:
        engine = ExpressionEngine({"n": 42})
        assert engine._eval_expression_typed("n") == 42

    def test_float_type_restore(self) -> None:
        engine = ExpressionEngine({"n": 3.14})
        assert engine._eval_expression_typed("n") == 3.14

    def test_bool_true_restore(self) -> None:
        engine = ExpressionEngine({"f": True})
        assert engine._eval_expression_typed("f") is True

    def test_bool_false_restore(self) -> None:
        engine = ExpressionEngine({"f": False})
        assert engine._eval_expression_typed("f") is False

    def test_none_restore(self) -> None:
        engine = ExpressionEngine({"v": None})
        assert engine._eval_expression_typed("v") is None

    def test_string_no_conversion(self) -> None:
        engine = ExpressionEngine({"s": "hello world"})
        result = engine._eval_expression_typed("s")
        assert result == "hello world"
        assert isinstance(result, str)

    def test_undefined_returns_empty_string(self) -> None:
        engine = ExpressionEngine({})
        # Undefined variable → ChainableUndefined renders to ""
        result = engine._eval_expression_typed("nonexistent")
        assert result == ""
