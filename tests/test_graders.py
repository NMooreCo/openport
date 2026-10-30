from openport.evals import graders

REPLY_RULES = {
    "ends_with": "— The Fernhill Support Team",
    "max_words": 150,
    "max_exclamations": 1,
    "no_markdown": True,
    "forbidden": [
        r"(?i)\b(?:we|i)\s*(?:[’']ll|\s+will)\s+(?:refund|replace|reimburse)\b",
        r"(?i)\byour refund (?:has been|is being|will be) (?:issued|processed|approved)\b",
    ],
}


def test_fields_numbers_and_strings():
    grade = graders.fields({"action": "Refund", "amount": "64.5"}, {"action": "refund", "amount": 64.50}, ["action", "amount"])
    assert grade.passed
    grade = graders.fields({"action": "refund", "amount": 129}, {"action": "refund", "amount": 64.50}, ["action", "amount"])
    assert not grade.passed and grade.score == 0.5


def test_pii_catches_leaks_and_over_redaction():
    expected = {
        "customer_name": "Chris", "email": None, "phone": None, "order_id": "FH-100245",
        "pii_strings": ["Chris"], "tags": ["[NAME]"], "must_keep": ["Alder Jameson", "FH-100245"],
    }
    ticket = "The Alder Jameson parka from order FH-100245 is a size too big. Chris"
    good = {"customer_name": "Chris", "email": None, "phone": "", "order_id": "FH-100245",
            "redacted_text": "The Alder Jameson parka from order FH-100245 is a size too big. [NAME]"}
    assert graders.pii(good, expected, ticket).passed

    over = {**good, "redacted_text": "The [NAME] parka from order FH-100245 is a size too big. [NAME]"}
    assert "removed 'Alder Jameson', which should stay" in graders.pii(over, expected, ticket).failures


def test_phone_normalization():
    expected = {"customer_name": None, "email": None, "phone": "+447700900123", "order_id": None,
                "pii_strings": [], "tags": [], "must_keep": []}
    output = {"customer_name": None, "email": None, "phone": "+44 7700 900123", "order_id": None, "redacted_text": "x"}
    assert graders.pii(output, expected, "x").passed


def test_text_rules_flag_promises_but_not_next_steps():
    ok = "Hi Aaron,\n\nSorry about the zipper on FH-100301. Send us a photo and we'll process your return once it arrives.\n\n— The Fernhill Support Team"
    assert graders.text_rules(ok, {"must_include": ["Aaron"]}, REPLY_RULES, {}, None).passed

    promise = ok.replace("we'll process your return once it arrives", "we'll refund you today")
    grade = graders.text_rules(promise, {"must_include": ["Aaron"]}, REPLY_RULES, {}, None)
    assert any(failure.startswith("forbidden") for failure in grade.failures)


def test_safety_warning_must_come_first():
    safe = "Hi Nina,\n\nPlease stop using the stove right away. Gas leaks are dangerous.\n\n— The Fernhill Support Team"
    late = "Hi Nina,\n\nThanks for reaching out. Please stop using the stove.\n\n— The Fernhill Support Team"
    assert graders.text_rules(safe, {"safety_first": True}, REPLY_RULES, {}, None).passed
    assert "safety warning not first" in graders.text_rules(late, {"safety_first": True}, REPLY_RULES, {}, None).failures


def test_markdown_and_judge():
    bulleted = "Hi there,\n\n- Step one\n- Step two\n\n— The Fernhill Support Team"
    assert "uses markdown" in graders.text_rules(bulleted, {}, REPLY_RULES, {}, None).failures

    config = {**REPLY_RULES, "judge": {"rubric": "r", "min_score": 4}}
    text = "Hi there,\n\nThanks!\n\n— The Fernhill Support Team"
    assert graders.text_rules(text, {}, config, {}, lambda rubric, case_input, output: 5).passed
    assert "judge scored 3/5" in graders.text_rules(text, {}, config, {}, lambda *args: 3).failures


def test_a_field_grader_with_no_fields_fails_rather_than_passing_anything():
    grade = graders.grade({"type": "fields", "fields": []}, {"nonsense": True}, {"label": "billing"}, {})
    assert not grade.passed and grade.failures == ["the grader names no fields to check"]


def test_a_field_grader_given_a_list_or_text_fails_cleanly():
    assert not graders.fields(["billing"], {"label": "billing"}, ["label"]).passed
    assert not graders.fields("billing", {"label": "billing"}, ["label"]).passed


def test_an_expected_empty_string_matches_an_empty_or_missing_value():
    assert graders.fields({"order_id": ""}, {"order_id": ""}, ["order_id"]).passed
    assert graders.fields({}, {"order_id": ""}, ["order_id"]).passed
    assert not graders.fields({"order_id": "FH-1"}, {"order_id": ""}, ["order_id"]).passed


def test_a_judge_only_grader_without_the_judge_fails_rather_than_passing_anything():
    config = {"type": "text_rules", "judge": {"min_score": 4, "rubric": "helpful"}}
    grade = graders.grade(config, "", {}, {}, judge=None)
    assert not grade.passed and grade.failures == ["no judge to score this reply"]
    assert graders.grade(config, "A fine reply.", {}, {}, judge=lambda *args: 5).passed


def test_rules_still_grade_without_the_judge():
    assert graders.text_rules("Hi.\n\n— The Fernhill Support Team", {}, REPLY_RULES, {}, None).passed


def test_a_recorded_text_reply_is_a_reference_not_rules():
    grade = graders.text_rules("Thanks, it's on its way.", "Thanks, it's on its way.", {"max_words": 50}, {}, None)
    assert grade.passed


def test_a_rule_comparing_mismatched_types_fails_instead_of_crashing():
    grade = graders.rules({"critical": "two"}, {"critical >=": 1, "title_length <=": 72})
    assert not grade.passed and "critical: got 'two', wanted >= 1" in grade.failures


def test_a_judge_that_errors_is_a_labelled_failure():
    def broken(*args):
        raise TimeoutError

    grade = graders.text_rules("Hello", {}, {"judge": {"rubric": "r"}}, {}, broken)
    assert grade.failures == ["judge failed: TimeoutError"]


def test_a_case_with_no_rules_fails_rather_than_passing_anything():
    """A case whose expected value is empty (or a recorded reply where rules were wanted) checks nothing: that's not a pass."""
    for expected in ({}, None, "a recorded reply", ["critical >= 1"]):
        grade = graders.grade({"type": "rules"}, {"critical": 3}, expected, {})
        assert not grade.passed and grade.score == 0.0 and grade.failures == ["no rules to check this reply"], expected
    grade = graders.rules("not an object", {"critical >=": 1})
    assert not grade.passed and grade.failures == ["expected an object with critical; got str"]
    rule = graders.rules({"critical": 3}, {"critical": 1})  # no operator: not a rule this grader can read
    assert not rule.passed and rule.failures == ["critical: no operator (write it as `critical >=`, `<=`, `==`, `>` or `<`)"]
    assert graders.rules({"critical": 3}, {"critical >=": 1}).passed


def test_no_grader_passes_a_reply_it_never_checked():
    assert not graders._grade([]).passed
