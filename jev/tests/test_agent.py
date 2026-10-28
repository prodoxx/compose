"""Offline contracts for a dynamic operation/target policy. No paid APIs."""

import json
import time
from copy import deepcopy
from unittest.mock import Mock

import pytest

from jev_ultrafast import agent as loop
from jev_ultrafast import model
from jev_ultrafast.browser import StalePage, browser_operation, fingerprint


def page():
    state = {
        "url": "https://example.test/",
        "title": "Search",
        "text": "Search",
        "scroll": {"y": 0},
        "actions": [
            {"id": "e1", "kind": "fill", "label": "Search", "role": "textbox", "value": "", "node": 10},
            {"id": "e2", "kind": "click", "label": "Open Search", "role": "textbox", "value": "", "node": 10},
            {"id": "e3", "kind": "click", "label": "Go", "role": "button", "value": "", "node": 20},
            {"id": "wait", "kind": "wait", "label": "Wait"},
        ],
    }
    state["fingerprint"] = fingerprint(state)
    return state


def choice(ids, selected):
    return {"choice": selected, "confidence": 1.0, "probabilities": {i: float(i == selected) for i in ids}}


def decision(action="e1"):
    return {
        "choice": action,
        "operation": "TYPE_TEXT",
        "target": "1",
        "confidence": 1.0,
        "probabilities": {action: 1.0},
        "latency_ms": 10,
        "usage": {},
    }


@pytest.mark.parametrize("mutation", ["unknown", "nan", "missing", "negative", "non_max", "confidence"])
def test_invalid_choice_is_rejected(mutation):
    a = choice(["a", "b"], "a")
    if mutation == "unknown":
        a["choice"] = "invented"
    elif mutation == "nan":
        a["probabilities"]["a"] = float("nan")
    elif mutation == "missing":
        del a["probabilities"]["b"]
    elif mutation == "negative":
        a["probabilities"]["b"] = -1
    elif mutation == "non_max":
        a["choice"] = "b"
    else:
        a["confidence"] = 5
    with pytest.raises(ValueError, match="Invalid TypeSafe"):
        model.validate_choice(a, {"a", "b"})


def test_one_index_per_node_with_operation_specific_targets():
    elements, targets, controls = model.action_space(page()["actions"])
    assert len(elements) == 2
    assert elements[0]["operations"] == ["TYPE_TEXT", "CLICK"]
    assert targets["TYPE_TEXT"]["1"]["id"] == "e1"
    assert targets["CLICK"]["1"]["id"] == "e2"
    assert targets["CLICK"]["2"]["id"] == "e3"
    assert "WAIT" in controls


def test_all_heads_are_one_request_and_only_matching_head_executes(monkeypatch):
    calls = []

    def post(_url, _key, body):
        calls.append(body)
        return {
            "model": "test",
            "answers": {
                "operation": choice(body["questions"]["operation"]["criteria"], "TYPE_TEXT"),
                "type_text_target": choice(["1"], "1"),
                "click_target": {"choice": "invented"},
            },
        }

    monkeypatch.setenv("TYPESAFE_API_KEY", "test")
    monkeypatch.setattr(model, "post_json", post)
    d = model.choose(page(), "Find a book", [])
    assert len(calls) == 1
    assert d["operation"] == "TYPE_TEXT" and d["target"] == "1" and d["choice"] == "e1"
    assert set(calls[0]["questions"]) == {"operation", "click_target", "type_text_target"}


def test_click_cannot_consume_a_text_target(monkeypatch):
    def post(_url, _key, body):
        return {
            "model": "test",
            "answers": {
                "operation": choice(body["questions"]["operation"]["criteria"], "CLICK"),
                "type_text_target": choice(["1"], "1"),
                "click_target": choice(["1", "2", "999"], "999"),
            },
        }

    monkeypatch.setenv("TYPESAFE_API_KEY", "test")
    monkeypatch.setattr(model, "post_json", post)
    with pytest.raises(ValueError, match="Invalid TypeSafe"):
        model.choose(page(), "Find a book", [])


def test_target_head_receives_control_state_and_full_next_step_rules(monkeypatch):
    p = page()
    p["actions"].insert(0, {
        "id": "toggle", "kind": "click", "label": "Free cancellation", "node": 30,
        "role": "checkbox", "checked": "true", "selected": False,
    })

    def post(_url, _key, body):
        questions = body["questions"]
        target = questions["click_target"]
        assert target["criteria"]["1"]["checked"] == "true"
        assert target["criteria"]["1"]["selected"] is False
        assert questions["operation"]["instructions"]["rules"] in target["instructions"]["rules"]
        return {
            "model": "test",
            "answers": {
                "operation": choice(questions["operation"]["criteria"], "CLICK"),
                "click_target": choice(target["criteria"], "3"),
            },
        }

    monkeypatch.setenv("TYPESAFE_API_KEY", "test")
    monkeypatch.setattr(model, "post_json", post)
    d = model.choose(p, "Search with free cancellation", [])
    assert d["choice"] == "e3"


def test_quoted_task_text_still_uses_the_llm(monkeypatch):
    monkeypatch.setenv("TEXT_MODEL_API_KEY", "test")
    post = Mock(return_value={"choices": [{"message": {"content": '{"text":"Zurich"}'}}]})
    monkeypatch.setattr(model, "post_json", post)
    context = model.field_context('Fly from "Zurich" to London', page()["actions"][0], page(), [])
    assert model.field_text(context)[0] == "Zurich"
    assert post.call_count == 1
    sent = json.loads(post.call_args.args[2]["messages"][1]["content"])
    assert sent["goal"] == 'Fly from "Zurich" to London'


def test_missing_text_credential_stops_before_guessing(monkeypatch):
    monkeypatch.delenv("TEXT_MODEL_API_KEY", raising=False)
    with pytest.raises(ValueError, match="TEXT_MODEL_API_KEY"):
        model.field_text({"goal": 'Enter "Zurich"'})


@pytest.fixture
def runner():
    a = loop.Agent.__new__(loop.Agent)
    a.screenshots = False
    a.pending_text = None
    a.secrets = {}
    p = page()
    a.state = {
        "browser": Mock(fresh=Mock(return_value=True), observe=Mock(return_value=p)),
        "page": p,
        "decision": decision(),
        "goal": "Find a book",
        "history": [],
        "decisions": [],
        "status": "predicted",
        "stop_reason": None,
        "skipped": [],
        "started_at": time.perf_counter(),
        "record": False,
        "text_calls": [],
    }
    return a


def test_stale_decision_is_consumed_before_any_mutation(runner):
    runner.state["browser"].fresh.return_value = False
    with pytest.raises(StalePage):
        runner.command("act", {"fingerprint": runner.state["page"]["fingerprint"]})
    runner.state["browser"].act.assert_not_called()
    assert runner.state["decision"] is None


def test_generated_text_reused_only_for_identical_retry_context(runner, monkeypatch):
    helper = Mock(return_value=("book", {"model": "test", "latency_ms": 10}))
    monkeypatch.setattr(loop, "field_text", helper)
    runner.state["browser"].act.side_effect = [StalePage("Changed before input"), None]
    with pytest.raises(StalePage):
        runner.command("act", {"fingerprint": runner.state["page"]["fingerprint"]})
    runner.state["decision"] = decision()
    runner.command("act", {"fingerprint": runner.state["page"]["fingerprint"]})
    assert helper.call_count == 1
    assert runner.state["browser"].act.call_count == 2  # The first call rejects before any browser input.
    assert runner.pending_text is None


def test_changed_field_context_does_not_reuse_generated_text(runner, monkeypatch):
    helper = Mock(return_value=("book", {"model": "test", "latency_ms": 10}))
    monkeypatch.setattr(loop, "field_text", helper)
    runner.state["browser"].act.side_effect = [StalePage("Changed before input"), None]
    with pytest.raises(StalePage):
        runner.command("act", {"fingerprint": runner.state["page"]["fingerprint"]})
    runner.state["page"]["text"] = "Different page context"
    runner.state["decision"] = decision()
    runner.command("act", {"fingerprint": runner.state["page"]["fingerprint"]})
    assert helper.call_count == 2


def test_loading_waits_do_not_trigger_no_progress_stop(runner):
    for _ in range(5):
        runner.state["decision"] = decision("wait")
        runner.command("act", {"fingerprint": runner.state["page"]["fingerprint"]})
    assert len(runner.state["history"]) == 5 and runner.state["status"] == "ready"


def test_stale_observation_preserves_executed_action(runner):
    runner.state["decision"] = decision("e3")
    runner.state["browser"].observe.side_effect = StalePage("changed")
    with pytest.raises(StalePage):
        runner.command("act", {"fingerprint": runner.state["page"]["fingerprint"]})
    assert runner.state["history"][-1]["action"] == "Go"
    runner.state["browser"].act.assert_called_once()


def test_observation_is_one_atomic_browser_read(monkeypatch):
    import jev_ultrafast.browser as browser

    p = page()
    cdp = Mock(return_value={"result": {"value": p}})
    monkeypatch.setattr(browser, "cdp", cdp)
    actual = browser_operation({"operation": "observe", "session": "test", "screenshot": False})
    assert actual["actions"] == p["actions"]
    assert cdp.call_count == 1
    assert cdp.call_args.args[0] == "Runtime.evaluate"


def test_executor_rejects_a_stale_page_before_browser_input(monkeypatch):
    import jev_ultrafast.browser as browser

    b = browser.Browser.__new__(browser.Browser)
    b.fresh = Mock(return_value=False)
    operation = Mock()
    monkeypatch.setattr(browser, "browser_operation", operation)
    with pytest.raises(StalePage):
        b.act(page()["actions"][0], page(), "book")
    operation.assert_not_called()


@pytest.mark.parametrize("response", [{"exceptionDetails": {}}, {"result": {}}])
def test_interrupted_dropdown_mutation_cannot_be_retried_as_stale(monkeypatch, response):
    import jev_ultrafast.browser as browser

    # A navigation can destroy the evaluation result after the change event already fired.
    if "exceptionDetails" in response:
        response["exceptionDetails"] = {"text": "Execution context destroyed"}
    cdp = Mock(return_value=response)
    monkeypatch.setattr(browser, "cdp", cdp)
    with pytest.raises(RuntimeError, match="Dropdown execution"):
        browser_operation({"operation": "act", "session": "test", "action": {
            "id": "e1", "kind": "select", "node": 1, "value": "Design",
        }})
    assert cdp.call_count == 1


def test_hover_is_its_own_operation_and_moves_the_pointer_without_pressing(monkeypatch):
    import jev_ultrafast.browser as browser

    menu = {"id": "e1", "kind": "hover", "label": "Hover Menu", "role": "button", "value": "", "node": 5}
    _, targets, _ = model.action_space([{**menu, "id": "e0", "kind": "click", "label": "Menu"}, menu])
    assert targets["HOVER"]["1"]["id"] == "e1" and targets["CLICK"]["1"]["id"] == "e0"
    # The target script finds the element, then only a mouseMoved reaches the page.
    cdp = Mock(side_effect=[{"result": {"value": {"x": 40, "y": 50}}}, {}])
    monkeypatch.setattr(browser, "cdp", cdp)
    browser_operation({"operation": "act", "session": "s", "action": menu})
    assert [c.args[0] for c in cdp.call_args_list] == ["Runtime.evaluate", "Input.dispatchMouseEvent"]
    assert cdp.call_args.kwargs["type"] == "mouseMoved"


def test_fingerprint_tracks_values_and_identity_not_screenshots():
    p = page()
    other = deepcopy(p)
    other["screenshot"] = "changed"
    assert fingerprint(p) == fingerprint(other)
    other["actions"][0]["node"] = 99
    assert fingerprint(p) != fingerprint(other)


def reply(content, tokens=5):
    return {"choices": [{"message": {"content": content}}], "usage": {"total_tokens": tokens}}


@pytest.mark.parametrize(
    "content",
    ["Thinking: Zurich", '{"text":null}', '{"text":"  "}', '{"text":"Zurich","extra":true}', '{"text":123}', None],
)
def test_text_helper_rejects_invalid_values_after_one_retry(monkeypatch, content):
    monkeypatch.setenv("TEXT_MODEL_API_KEY", "test")
    post = Mock(return_value=reply(content))
    monkeypatch.setattr(model, "post_json", post)
    with pytest.raises(ValueError, match="nothing typed"):
        model.field_text({"goal": "Find a flight"})
    assert post.call_count == 2


def quiz_text_context():
    field = {"label": "What is your favourite food?", "role": "textbox", "value": ""}
    return model.field_context("Answer every question and submit", field, page(), [])


@pytest.mark.parametrize("first", ['{"text": null}', "```json\n{}\n```", "Pizza"])
def test_text_helper_retries_a_refused_free_text_answer(monkeypatch, first):
    # {"text": null} was seen live from mercury-2.5 on a quiz textarea; it used to end the whole run.
    monkeypatch.setenv("TEXT_MODEL_API_KEY", "test")
    post = Mock(side_effect=[reply(first), reply('{"text": "Pizza"}')])
    monkeypatch.setattr(model, "post_json", post)
    value, helper = model.field_text(quiz_text_context())
    assert (value, post.call_count, helper["usage"]) == ("Pizza", 2, {"total_tokens": 10})
    retry = post.call_args.args[2]["messages"]
    assert retry[-2] == {"role": "assistant", "content": first}
    assert retry[-1]["content"] == model.TEXT_RETRY


@pytest.mark.parametrize("content", ['{"text": "Pizza"}\n```', '```json\n{"text": "Pizza"}\n```'])
def test_text_helper_accepts_a_fenced_value_without_retry(monkeypatch, content):
    # A trailing code fence was also seen live; the value itself was valid.
    monkeypatch.setenv("TEXT_MODEL_API_KEY", "test")
    post = Mock(return_value=reply(content))
    monkeypatch.setattr(model, "post_json", post)
    assert model.field_text(quiz_text_context())[0] == "Pizza"
    assert post.call_count == 1


@pytest.mark.parametrize("scrolls, note", [
    ((), None),
    (("scroll_down",), "below"),
    (("scroll_up", "scroll_down"), "above and below"),
])
def test_model_is_told_where_the_page_continues_offscreen(monkeypatch, scrolls, note):
    # A form's Submit below the fold is not an observed control. Without this note the model stopped BLOCKED.
    p = page()
    p["actions"][-1:-1] = [{"id": s, "kind": "scroll", "label": s, "delta": 560} for s in scrolls]
    sent = []

    def post(_url, _key, body):
        sent.append(body["state"]["page"])
        return {"model": "test", "answers": {"operation": choice(body["questions"]["operation"]["criteria"], "DONE")}}

    monkeypatch.setenv("TYPESAFE_API_KEY", "test")
    monkeypatch.setattr(model, "post_json", post)
    model.choose(p, "Answer every question and submit", [])
    if note:
        assert f"continues {note} the visible area" in sent[0]["offscreen"]
    else:
        assert "offscreen" not in sent[0]


@pytest.mark.parametrize("reported, total", [
    ({"input_tokens": 2413, "output_tokens": 166}, 2579),
    ({"input_tokens": 5, "output_tokens": 1, "total_tokens": 9}, 9),
    ({}, None),
])
def test_typesafe_usage_carries_a_total(monkeypatch, reported, total):
    def post(_url, _key, body):
        answers = {"operation": choice(body["questions"]["operation"]["criteria"], "DONE")}
        return {"model": "test", "answers": answers, "usage": reported}

    monkeypatch.setenv("TYPESAFE_API_KEY", "test")
    monkeypatch.setattr(model, "post_json", post)
    assert model.choose(page(), "Find a book", [])["usage"].get("total_tokens") == total


def fake_harness(monkeypatch, browser, *, endpoint, alive, fail=None):
    stopped = []
    monkeypatch.delenv("BU_CDP_WS", raising=False)
    if endpoint:
        monkeypatch.setenv("BU_CDP_URL", "http://127.0.0.1:9555")
    else:
        monkeypatch.delenv("BU_CDP_URL", raising=False)

    def cdp(method, **_params):
        if method == fail:
            raise RuntimeError("CDP failed")
        return {"targetId": "t", "sessionId": "s", "result": {"value": "complete"}}

    monkeypatch.setattr(browser, "daemon_alive", lambda: alive)
    monkeypatch.setattr(browser, "ensure_daemon", lambda: None)
    monkeypatch.setattr(browser, "restart_daemon", lambda: stopped.append(True))
    monkeypatch.setattr(browser, "cdp", cdp)
    return stopped


@pytest.mark.parametrize("endpoint, alive, stops", [(True, False, True), (True, True, False), (False, False, False)])
def test_run_stops_only_a_daemon_it_started_for_a_cdp_browser(monkeypatch, endpoint, alive, stops):
    import jev_ultrafast.browser as browser

    stopped = fake_harness(monkeypatch, browser, endpoint=endpoint, alive=alive)
    b = browser.Browser("http://localhost/")
    assert not stopped
    b.close()
    b.close()
    assert stopped == ([True] if stops else [])


def test_failed_start_still_stops_the_daemon_it_started(monkeypatch):
    import jev_ultrafast.browser as browser

    stopped = fake_harness(monkeypatch, browser, endpoint=True, alive=False, fail="Page.navigate")
    with pytest.raises(RuntimeError, match="CDP failed"):
        browser.Browser("http://localhost/")
    assert stopped == [True]


def test_text_helper_prompt_answers_free_text_but_never_invents_identity():
    assert "plausible answer" in model.TEXT_VALUE
    assert "identity, contact, account, credential, or payment" in model.TEXT_VALUE


def test_navigation_during_prediction_reobserves_without_action(runner):
    runner.state["browser"].fresh.side_effect = StalePage("Document navigating")
    runner.command("tick")
    assert runner.state["status"] == "ready"
    assert runner.state["decision"] is None
    runner.state["browser"].act.assert_not_called()


def sent_state(monkeypatch, p, history=()):
    sent = []

    def post(_url, _key, body):
        sent.append(body)
        return {"model": "test", "answers": {"operation": choice(body["questions"]["operation"]["criteria"], "DONE")}}

    monkeypatch.setenv("TYPESAFE_API_KEY", "test")
    monkeypatch.setattr(model, "post_json", post)
    model.choose(p, "Answer every question and submit", list(history))
    return sent[0]


def test_model_sees_disabled_controls_and_unanswered_items_but_cannot_target_them(monkeypatch):
    # A quiz Submit stayed disabled behind a skipped radio question. Not listing it, the model never learned why.
    p = page()
    p["disabled"] = ["button Submit answers [disabled]"]
    p["unanswered"] = ["Where does the sun set? [no option selected, above the visible area]"]
    body = sent_state(monkeypatch, p)
    assert body["state"]["page"]["disabled"] == p["disabled"]
    assert body["state"]["page"]["unanswered"] == p["unanswered"]
    assert "Submit answers" not in json.dumps(body["questions"])
    assert "Submit answers" not in json.dumps(body["state"]["elements"])
    assert "Disabled controls are listed but cannot be used" in model.NEXT_ACTION
    assert fingerprint(p) != fingerprint({**p, "unanswered": []})
    assert "disabled" not in sent_state(monkeypatch, page())["state"]["page"]


def steps(*states, kind="scroll"):
    return [{"kind": kind, "before": a, "after": b} for a, b in zip(states, states[1:])]


@pytest.mark.parametrize("history, cycling", [
    (steps("A", "B", "A", "B", "A"), True),  # scroll down, up, down, up
    (steps("A", "A", "A", "A", "A"), True),
    (steps("W", "Y", "X", "X", "X"), False),  # answers that progressed, then no-ops: the 3-action stop covers it
    (steps("A", "B", "C", "B", "C", "B"), True),  # progress, then a cycle
    (steps("A", "B", "A", "B", "A", kind="wait"), False),  # loading waits are not a cycle
    (steps("A", "B", "A", "B"), False),  # too short
])
def test_cycle_means_no_new_page_state(history, cycling):
    assert model.cycling(history, 4) is cycling


def test_model_is_warned_about_a_cycle(monkeypatch):
    warned = sent_state(monkeypatch, page(), steps("A", "B", "A", "B", "A"))["state"]["page"]["no_progress"]
    assert "same page states" in warned and "disabled control" in warned
    assert "no_progress" not in sent_state(monkeypatch, page(), steps("A", "B", "C", "D", "E"))["state"]["page"]


def test_alternating_scrolls_stop_with_a_reason_before_the_budget(runner):
    # Each scroll changes the page, so the 3 no-change stop missed it and the run spent all 60 actions.
    pages = []
    for y, (scroll, delta) in enumerate([("scroll_down", 560), ("scroll_up", -560)]):
        p = page()
        p["scroll"] = {"y": y}
        p["actions"][-1:-1] = [{"id": scroll, "kind": "scroll", "label": scroll, "delta": delta}]
        p["fingerprint"] = fingerprint(p)
        pages.append(p)
    runner.state["page"] = pages[0]
    for i in range(model.CYCLE_STOP):
        runner.state["browser"].observe.return_value = pages[(i + 1) % 2]
        runner.state["decision"] = {**decision(runner.state["page"]["actions"][-2]["id"]), "probabilities": {
            "scroll_down": 1.0, "scroll_up": 1.0}}
        assert runner.state["status"] != "blocked"
        runner.command("act", {"fingerprint": runner.state["page"]["fingerprint"]})
    assert runner.state["status"] == "blocked"
    assert "cycled between the same page states" in runner.state["stop_reason"]
    assert len(runner.state["history"]) == model.CYCLE_STOP


def login_page(password="", email=""):
    """A login form as snapshot.js reports it: the password's value is only whether it is filled."""
    state = {
        "url": "https://example.test/login",
        "title": "Log in",
        "text": "Log in",
        "scroll": {"y": 0},
        "actions": [
            {"id": "e1", "kind": "fill", "label": "Email", "role": "textbox", "value": email, "node": 10},
            {"id": "e2", "kind": "click", "label": "Open Email", "role": "textbox", "value": email, "node": 10},
            {"id": "e3", "kind": "fill", "label": "Password", "role": "textbox", "value": password, "node": 20, "secret": True},
            {"id": "e4", "kind": "click", "label": "Open Password", "role": "textbox", "value": password, "node": 20,
             "secret": True},
            {"id": "e5", "kind": "click", "label": "Log in", "role": "button", "value": "", "node": 30},
            {"id": "wait", "kind": "wait", "label": "Wait"},
        ],
    }
    state["fingerprint"] = fingerprint(state)
    return state


def test_a_secret_field_is_typed_by_the_run_and_its_value_never_reaches_a_model(runner, monkeypatch):
    # A login form's password was never an action, so every case that logged in through a form ended blocked.
    runner.secrets = {loop.field_key("Password:"): "pw-Sekr3t!", loop.field_key("email"): "me@example.test"}
    runner.state["browser"].observe.return_value = login_page()
    runner.state["page"] = runner.observe()
    helper = Mock()
    monkeypatch.setattr(loop, "field_text", helper)
    for target in ("e1", "e3"):
        runner.state["decision"] = decision(target)
        runner.state["browser"].observe.return_value = login_page(password="[filled]", email="me@example.test")
        runner.command("act", {"fingerprint": runner.state["page"]["fingerprint"]})
    typed = [c.kwargs["text"] for c in runner.state["browser"].act.call_args_list]
    assert typed == ["me@example.test", "pw-Sekr3t!"]
    helper.assert_not_called()
    assert [h["text"] for h in runner.state["history"]] == ["[secret]", "[secret]"]
    body = sent_state(monkeypatch, runner.state["page"], runner.state["history"])
    for value in ("pw-Sekr3t!", "me@example.test"):
        assert value not in json.dumps(body) and value not in json.dumps(runner.snapshot()["history"])
    secret = {e["label"]: e["value"] for e in body["state"]["elements"] if e.get("secret")}
    assert secret == {"Email": "[filled]", "Password": "[filled]"}


def test_a_password_field_no_secret_names_is_left_out_and_named_when_the_run_stops(runner):
    runner.state["browser"].observe.return_value = login_page()
    runner.state["page"] = runner.observe()
    assert "Password" not in json.dumps(runner.state["page"]["actions"])
    runner.state["decision"] = {**decision("BLOCKED"), "choice": "BLOCKED"}
    runner.command("act", {"fingerprint": runner.state["page"]["fingerprint"]})
    assert runner.state["status"] == "blocked"
    assert runner.state["stop_reason"] == ("The model chose BLOCKED: no listed action could make progress; "
                                           "skipped the password field 'Password': no secret names it")


def test_the_action_budget_ends_the_run_blocked_with_its_reason_instead_of_raising(runner):
    # It raised, so verify printed no result and skipped the case's assert.
    runner.state["history"] = [{"step": i + 1, "kind": "click", "action": "Go", "page_changed": True} for i in range(60)]
    runner.state["decision"] = decision("e3")
    runner.command("act", {"fingerprint": runner.state["page"]["fingerprint"]})
    assert (runner.state["status"], runner.state["stop_reason"]) == ("blocked", "Stopped at the 60-action budget")
    runner.state["browser"].act.assert_not_called()


def test_the_model_call_budget_ends_the_run_blocked_before_another_call(runner, monkeypatch):
    runner.state["status"], runner.state["decisions"] = "ready", [{}] * 120
    choose = Mock()
    monkeypatch.setattr(loop, "choose", choose)
    snap = runner.command("tick")
    assert (snap["status"], snap["stop_reason"]) == ("blocked", "Stopped at the 120-call model budget")
    choose.assert_not_called()
    runner.state["browser"].act.assert_not_called()
